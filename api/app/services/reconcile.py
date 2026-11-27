"""Pending-to-posted reconcile (#50).

Spec: the SimpleFIN protocol doesn't say whether a pending row keeps its id once it
posts, so plan for it not to. A pending row that was in an earlier pull but is
missing from this one, on an account that ingested cleanly, either (a) was
re-issued as a brand-new posted row -- the pending row absorbs it (`matching.
absorb`, `keep=pending`) and so keeps its own id, which is the one the phone's
outbox and receipts already hold -- or (b) was cancelled, in which case it stops
counting as spending: deleted, unless the user touched it or it's still young
enough that the bank may yet post it.

The in-place case (the same id flips pending -> posted) is `app.services.sync`'s
upsert, not this module.
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.models.enums import Source
from app.services import rules
from app.services.matching import RECONCILE_WINDOW, absorb, has_user_links, lock_bank_row
from app.services.splits import has_split_children


@dataclass
class ReconcileResult:
    """`absorbed_ids` are the surviving pending rows (now posted): the caller
    hands them to the manual matcher as newly posted rows.
    """

    absorbed_ids: list[uuid.UUID] = field(default_factory=list)
    deleted_count: int = 0


def _vanished_pending(
    db: Session,
    user_id: uuid.UUID,
    seen_external_ids: dict[uuid.UUID, set[str]],
    window_start: date,
) -> list[Transaction]:
    """Pending bank rows on a cleanly-ingested account, dated after the window's
    first day, whose `external_id` this pull didn't return. A row dated before
    the window simply wasn't asked for, so it can't have vanished.
    """
    vanished: list[Transaction] = []
    for account_id, seen in seen_external_ids.items():
        stmt = (
            select(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.account_id == account_id,
                Transaction.source == Source.simplefin,
                Transaction.pending.is_(True),
                Transaction.external_id.is_not(None),
                Transaction.parent_id.is_(None),
                ~has_split_children(),
                Transaction.posted_date > window_start,
            )
            .order_by(Transaction.posted_date, Transaction.id)
        )
        vanished.extend(row for row in db.scalars(stmt) if row.external_id not in seen)
    return vanished


def _user_touched(
    db: Session,
    user_id: uuid.UUID,
    txn: Transaction,
    index: rules.RuleIndex,
    *,
    count_subscription: bool = True,
) -> bool:
    """Notes, an event or report link (`event_id`, `reimbursement_id`), any
    inbound reference in `REPOINTED_FKS`, or merchant/category/bucket values no rule explains
    (`rules.values_explained_by_rule`) -- on a row the user reviewed or one whose
    values differ from a fresh row's. A PATCH doesn't set `reviewed`, so an
    unreviewed row with a user-chosen category counts as touched. `count_subscription`
    is passed through to `has_user_links`.
    """
    return has_user_links(
        db, user_id, txn, count_subscription=count_subscription
    ) or rules.values_set_by_hand(txn, index)


def _candidate_map(
    db: Session, user_id: uuid.UUID, vanished: list[Transaction]
) -> dict[uuid.UUID, list[Transaction]]:
    """For each vanished pending row, the posted bank rows that may be its
    re-issue: same account, same amount, within `RECONCILE_WINDOW`, created at
    or after the pending row (so a long-posted daily coffee is no candidate),
    and not part of a split. Rows from earlier runs count: a bank may list the
    posted row alongside the pending one for a while before dropping the latter.
    """
    posted = list(
        db.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.account_id.in_({pending.account_id for pending in vanished}),
                Transaction.amount.in_({pending.amount for pending in vanished}),
                Transaction.source == Source.simplefin,
                Transaction.pending.is_(False),
                Transaction.parent_id.is_(None),
                ~has_split_children(),
                Transaction.created_at >= min(pending.created_at for pending in vanished),
            )
        )
    )
    return {
        pending.id: [
            row
            for row in posted
            if row.account_id == pending.account_id
            and row.amount == pending.amount
            and row.created_at >= pending.created_at
            and abs(row.posted_date - pending.posted_date) <= RECONCILE_WINDOW
        ]
        for pending in vanished
    }


def reconcile_pending(
    db: Session,
    user_id: uuid.UUID,
    now: datetime,
    *,
    seen_external_ids: dict[uuid.UUID, set[str]],
    window_start: date,
) -> ReconcileResult:
    """Reconcile every vanished pending row (see the module docstring).

    A pending row is skipped, and logged, when it has more than one candidate
    (`_candidate_map`), when its one candidate is also the only candidate of
    another vanished pending row, or when the pair can't be locked or both rows
    already carry a `transfer_pair_id`, or when the candidate is user-touched (see
    `_merge_posted`); a skipped row is never deleted.

    Each absorb or delete runs in its own savepoint. Rows are re-locked
    (`matching.lock_bank_row`) before being changed so a concurrent PATCH isn't
    lost.
    """
    result = ReconcileResult()
    vanished = _vanished_pending(db, user_id, seen_external_ids, window_start)
    if not vanished:
        return result

    candidates = _candidate_map(db, user_id, vanished)
    claims = Counter(row.id for rows in candidates.values() for row in rows)
    index = rules.load_rule_index(db, user_id)
    for pending in vanished:
        rows = candidates[pending.id]
        if len(rows) > 1 or (rows and claims[rows[0].id] > 1):
            logger.warning(
                "Skipping reconcile of pending {}: ambiguous match among {} posted rows",
                pending.id,
                len(rows),
            )
        elif rows:
            survivor_id = _merge_posted(db, user_id, pending, rows[0], index)
            if survivor_id is not None:
                result.absorbed_ids.append(survivor_id)
        elif _cancel_if_untouched(db, user_id, now, pending, index):
            result.deleted_count += 1
    return result


def _merge_posted(
    db: Session,
    user_id: uuid.UUID,
    pending: Transaction,
    posted: Transaction,
    index: rules.RuleIndex,
) -> uuid.UUID | None:
    """The id of the surviving pending row, or `None` when the pair was skipped.

    `absorb` deletes `posted`, so a `posted` the user touched is never merged
    (`_user_touched`): that includes a manual capture's surviving row (#52), whose
    id the phone still holds, and a row the user edited while the bank listed both.
    """
    locked_pending = lock_bank_row(db, pending.id)
    locked_posted = lock_bank_row(db, posted.id)
    if locked_pending is None or locked_posted is None:
        logger.info(
            "Skipping reconcile {} <- {}: a row is locked by another writer", pending.id, posted.id
        )
        return None
    if locked_pending.transfer_pair_id is not None and locked_posted.transfer_pair_id is not None:
        logger.warning(
            "Skipping reconcile {} <- {}: both already carry a transfer_pair_id",
            locked_pending.id,
            locked_posted.id,
        )
        return None
    if _user_touched(db, user_id, locked_posted, index, count_subscription=False):
        logger.warning(
            "Skipping reconcile {} <- {}: the posted row is user-touched",
            locked_pending.id,
            locked_posted.id,
        )
        return None
    with db.begin_nested():
        absorb(db, keep=locked_pending, drop=locked_posted)
    return locked_pending.id


def _cancel_if_untouched(
    db: Session, user_id: uuid.UUID, now: datetime, pending: Transaction, index: rules.RuleIndex
) -> bool:
    """No posted match: a hold younger than `RECONCILE_WINDOW` may still post, so
    it stays; an older one is a cancelled hold and is deleted unless touched.
    Whether the row was deleted.
    """
    if pending.posted_date >= now.date() - RECONCILE_WINDOW:
        logger.info(
            "Keeping vanished pending {}: no match yet, younger than {} days",
            pending.id,
            RECONCILE_WINDOW.days,
        )
        return False
    locked = lock_bank_row(db, pending.id)
    if locked is None:
        logger.info("Skipping vanished pending {}: row locked by another writer", pending.id)
        return False
    if _user_touched(db, user_id, locked, index):
        logger.info("Keeping vanished pending {}: the user has touched it", locked.id)
        return False
    with db.begin_nested():
        db.delete(locked)
        db.flush()
    logger.info("Deleted vanished pending {}: no posted match, untouched", pending.id)
    return True
