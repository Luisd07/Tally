"""Auto-categorization rules, applied in priority order; first match wins.

Two halves live here: ordinary CRUD for `GET/POST/PATCH/DELETE /api/rules` (the
pattern shared with accounts/categories/subscriptions, see
`.claude/rules/api.md`), and `apply_rules`, the engine `app.services.sync`'s
`_post_process` hook (#48) calls on every sync's newly-inserted rows.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import UnprocessableEntity
from app.models import Category, Rule, Transaction
from app.models.enums import Bucket, MatchType
from app.schemas.rules import RuleCreate, RuleUpdate
from app.scoping import get_owned, owned
from app.services.crud import apply_patch, check_owned_refs, create_idempotent, merged
from app.services.normalize import normalize_text


def _matches(rule: Rule, normalized_pattern: str, field_value: str) -> bool:
    """Case-insensitive, on the normalized value of `field_value`.

    `pattern` is always matched literally (never compiled as a regex: see
    `app.models.rule.Rule` for why), so this is plain string containment,
    equality or a prefix check.
    """
    normalized_value = normalize_text(field_value)
    if rule.match_type == MatchType.equals:
        return normalized_value == normalized_pattern
    if rule.match_type == MatchType.starts_with:
        return normalized_value.startswith(normalized_pattern)
    return normalized_pattern in normalized_value


def _has_a_set_field(rule: Rule) -> bool:
    return (
        rule.set_merchant is not None
        or rule.set_category_id is not None
        or rule.set_bucket is not None
    )


def _apply(txn: Transaction, rule: Rule) -> None:
    if rule.set_merchant is not None:
        txn.merchant = rule.set_merchant
    if rule.set_category_id is not None:
        txn.category_id = rule.set_category_id
    if rule.set_bucket is not None:
        txn.bucket = rule.set_bucket
    txn.reviewed = True


def _load_active_rules(db: Session, user_id: uuid.UUID) -> list[Rule]:
    """The user's active rules, ordered `(priority, created_at, id)` -- lower
    priority runs first, ties broken oldest-first then by id, so the order is
    stable across calls.
    """
    return list(
        db.scalars(
            select(Rule)
            .where(Rule.user_id == user_id, Rule.active.is_(True))
            .order_by(Rule.priority.asc(), Rule.created_at.asc(), Rule.id.asc())
        )
    )


@dataclass(frozen=True)
class RuleIndex:
    """The user's active rules in priority order, with each rule's normalized
    pattern keyed by rule id: everything `_first_match` needs, built once per
    pass by `load_rule_index`.
    """

    rules: list[Rule]
    normalized_patterns: dict[uuid.UUID, str]


def _first_match(txn: Transaction, index: RuleIndex) -> Rule | None:
    """The first of `index.rules` (already in priority order) that matches `txn`.

    Matching is always against `description_raw`: `MatchField` has one member
    today (`description`) since sync never sets `merchant` before rules run
    (#51 UB-1/MV-2; see `app.models.enums.MatchField`).

    A rule with every `set_*` field null is skipped, never matched (#51 UB-2/
    MV-6): that only happens when its one-and-only `set_category_id` was
    nulled by a category delete (`ON DELETE SET NULL`), and matching a
    now-no-op rule would wrongly stop a later, still-meaningful rule from
    being tried.
    """
    for rule in index.rules:
        if not _has_a_set_field(rule):
            continue
        if _matches(rule, index.normalized_patterns[rule.id], txn.description_raw):
            return rule
    return None


def load_rule_index(db: Session, user_id: uuid.UUID) -> RuleIndex:
    """The user's active rules plus their normalized patterns, in the shape
    `values_explained_by_rule` and `_first_match` take.
    """
    active_rules = _load_active_rules(db, user_id)
    return RuleIndex(
        rules=active_rules,
        normalized_patterns={rule.id: normalize_text(rule.pattern) for rule in active_rules},
    )


def values_explained_by_rule(txn: Transaction, index: RuleIndex) -> bool:
    """Whether `txn`'s merchant/category/bucket are what a rule would have set,
    re-derived rather than stored: the rule that matches `txn` now (the same
    first-match `apply_rules` uses) has every non-null `set_merchant`/
    `set_category_id`/`set_bucket` equal to the row's own value. A `merchant`,
    `bucket` or `category_id` the rule does not set must still equal its
    sync-insert default (`None` / `personal` / `None`), or the user changed it
    and no rule explains it.

    `False` when no rule matches, and also when the rule was edited after it
    ran, since its `set_*` no longer equals what's on the row. A `reviewed` row these
    values explain is the rule's doing, not the user's (see `values_set_by_hand`).
    """
    rule = _first_match(txn, index)
    if rule is None:
        return False
    return (
        txn.merchant == rule.set_merchant
        and txn.category_id == rule.set_category_id
        and txn.bucket == (Bucket.personal if rule.set_bucket is None else rule.set_bucket)
    )


def values_set_by_hand(txn: Transaction, index: RuleIndex) -> bool:
    """Whether `txn` carries merchant/category/bucket values the user chose: the
    row is reviewed or differs from a fresh sync-inserted row, and
    `values_explained_by_rule` can't account for that. A PATCH doesn't set
    `reviewed`, so an unreviewed row with a user-chosen category counts. Shared by
    the pending reconcile (#50) and the transfer matcher (#61).
    """
    has_values = (
        txn.reviewed
        or txn.merchant is not None
        or txn.category_id is not None
        or txn.bucket != Bucket.personal
    )
    return has_values and not values_explained_by_rule(txn, index)


def apply_rules(db: Session, user_id: uuid.UUID, transaction_ids: list[uuid.UUID]) -> None:
    """Apply the first matching active rule to each of `transaction_ids`.

    An unmatched row is left exactly as it was inserted (`bucket=personal`,
    `reviewed=False`): the spec's "Anything no rule catches lands in the Inbox
    as unreviewed and defaults to personal." A matched row IS marked
    `reviewed=True` (#51 MV-4): the same sentence implies the converse for a
    row a rule did catch, so it no longer needs the user's triage.

    Only ever called with `transaction_ids` from a sync's `IngestResult.new_ids`
    (`app.services.sync._post_process`): brand-new bank rows. Nothing is split
    yet at that point (splitting only ever happens from the Inbox or
    Transactions screen, after a row exists), so there's no split parent/child
    case to special-case here.
    """
    if not transaction_ids:
        return

    index = load_rule_index(db, user_id)
    if not index.rules:
        return

    transactions = db.scalars(
        select(Transaction).where(
            Transaction.id.in_(transaction_ids), Transaction.user_id == user_id
        )
    )
    for txn in transactions:
        rule = _first_match(txn, index)
        if rule is not None:
            _apply(txn, rule)


# --- CRUD: GET/POST/PATCH/DELETE /api/rules --------------------------------------------


def list_rules(db: Session, user_id: uuid.UUID) -> list[Rule]:
    stmt = owned(Rule, user_id).order_by(Rule.priority.asc(), Rule.created_at.asc(), Rule.id.asc())
    return list(db.scalars(stmt))


def create_rule(db: Session, user_id: uuid.UUID, data: RuleCreate) -> tuple[Rule, bool]:
    """Idempotent create: a repeated `id` returns the existing row, ignoring the new body.

    Raises `NotFound` if `set_category_id` isn't the caller's.
    """
    check_owned_refs(db, user_id, data, {"set_category_id": Category})
    return create_idempotent(db, Rule, user_id, data.model_dump())


def update_rule(db: Session, user_id: uuid.UUID, id: uuid.UUID, data: RuleUpdate) -> Rule:
    """Checks the at-least-one-`set_*` requirement against the merged row:
    whichever `set_*` fields the patch sets, falling back to what's already
    stored for the ones it doesn't -- so clearing the row's only `set_*` field
    is a 422 here. There's no DB CHECK backing this (#51 UB-2/MV-6): see
    `app.models.rule.Rule`.
    """
    rule = get_owned(db, Rule, id, user_id)
    check_owned_refs(db, user_id, data, {"set_category_id": Category})

    set_merchant = merged(rule, data, "set_merchant")
    set_category_id = merged(rule, data, "set_category_id")
    set_bucket = merged(rule, data, "set_bucket")
    if set_merchant is None and set_category_id is None and set_bucket is None:
        raise UnprocessableEntity.value_error(
            "set_merchant, set_category_id or set_bucket must be set"
        )
    return apply_patch(db, rule, data)


def delete_rule(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> None:
    rule = get_owned(db, Rule, id, user_id)
    db.delete(rule)
    db.flush()
