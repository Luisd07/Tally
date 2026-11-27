"""Manual-to-bank matcher (#52) and transfer matcher (#61, wired in after #52 in
`app.services.sync._post_process`); `absorb` is also the merge step of the pending
reconcile (#50).

Spec "Receipts": a manual capture (phone, no bank id yet) and the bank's own row for
the same charge are, for a few days, two rows describing one expense. When the bank's
version lands, this module folds it into the manual row and deletes the bank row --
the surviving row keeps the *manual* row's id, not the bank's, because the phone's
outbox and a late receipt upload still hold that id (spec "Captures without signal":
"a retry can never create a duplicate"). The bank's own data (its `external_id`,
`amount`, dates, ...) becomes the surviving row's data; see `absorb`.
"""

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from loguru import logger
from sqlalchemy import Select, and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.models import IncomeEntry, Insight, Receipt, ReviewFinding, TaxReviewItem, Transaction
from app.models.enums import Bucket, Source
from app.services import rules
from app.services.splits import has_split_children

# Spec "Receipts": "the matcher looks for a manual row on the same account with the
# same amount within 3 days."
MATCH_WINDOW = timedelta(days=3)

# Spec "Transfers": a negative row on one account pairs with a positive row of the
# same amount on another "within 5 days".
TRANSFER_WINDOW = timedelta(days=5)

# Spec: "look for a new posted row on the same account with the same amount within
# 5 days" (the pending reconcile, `app.services.reconcile`). Also how old an
# unmatched vanished hold must be before it's deleted, and how near a pending row
# must be for the transfer matcher to leave its possible posted twin alone.
RECONCILE_WINDOW = timedelta(days=5)

# Spec "Receipts": "No match after 7 days puts the manual row in the Inbox with a
# flag." There's no column for this -- a manual row this old with no match is just a
# manual row that's still there, since a match absorbs the bank row into it without
# changing its id (see `absorb`). Shared with `app.services.transactions`'s
# `unmatched_manual` list filter (#53's Inbox).
UNMATCHED_AFTER = timedelta(days=7)

# The bank-owned fields SimpleFIN writes (`app.services.sync._bank_owned_update`,
# plus `source`/`external_id`/`fingerprint`, which that function doesn't touch
# because it only ever runs on a row that already has them). Moved onto the
# surviving row unconditionally: once matched, the bank's data for all of these
# is the only copy that exists.
_BANK_OWNED_FIELDS = (
    "source",
    "external_id",
    "amount",
    "posted_date",
    "transacted_at",
    "pending",
    "description_raw",
    "fingerprint",
)

# Fields a manual capture may set that the bank's row doesn't get from SimpleFIN.
# Filled onto the surviving row only where it doesn't already have one -- the
# user's own entry always wins, including one made on the bank row by some other
# path before this match (not currently possible, but `absorb` doesn't assume
# `keep` is always the manual side forever -- see its docstring).
#
# Policy (deliberate, not an oversight): if the bank row already carries a value
# here -- a #51 rule's `set_category_id`, say -- and the manual row has its own
# non-null value, the manual row's wins. The user's deliberate capture outranks
# whatever a rule guessed at, even when the rule ran first in `_post_process`.
_FILL_IF_NULL_FIELDS = (
    "merchant",
    "category_id",
    "event_id",
    "notes",
    "reimbursement_id",
    "subscription_id",
)

# Every inbound FK elsewhere in the schema that can point at a `transactions.id`
# row, repointed from `drop` to `keep` by `absorb` below. `tests/test_matching.
# py::test_every_inbound_transaction_fk_is_handled` walks `Base.metadata` and
# fails if a new inbound FK (a future table referencing a transaction) shows up
# without being added here or to `FK_ALLOWLIST` -- this tuple is what both that
# test and `absorb` read, so there is exactly one place to update.
REPOINTED_FKS: tuple[tuple[type, str], ...] = (
    (Receipt, "transaction_id"),
    (IncomeEntry, "transaction_id"),
    (Transaction, "transfer_pair_id"),
    (ReviewFinding, "transaction_id"),
    (TaxReviewItem, "transaction_id"),
    (Insight, "transaction_id"),
)

# Inbound FKs into `transactions.id` that `absorb` deliberately leaves alone,
# keyed by `(table_name, column_name)` since there's no repointing code to hang
# a model reference off of. Read by the same schema-guard test as
# `REPOINTED_FKS`.
FK_ALLOWLIST: dict[tuple[str, str], str] = {
    ("ai_suggestions", "transaction_id"): (
        "an AI suggestion (#169) only matters while its row is in the Inbox. The "
        "dropped row's suggestion goes with it (ON DELETE CASCADE), and the kept "
        "row keeps its own: it's unique per transaction, so it couldn't be "
        "repointed onto a row that already has one anyway."
    ),
    ("transactions", "parent_id"): (
        "split rows are excluded from matching on both sides: _bank_candidates "
        "filters out a non-null parent_id and has_split_children(), and "
        "_find_manual_match does the same. Neither keep nor drop can be a split "
        "parent with children pointing at it, or a child itself, when absorb "
        "runs, so there is never an inbound parent_id to repoint."
    ),
}


@dataclass(frozen=True)
class ManualMatchResult:
    """What one `match_manual_entries` call did. `survivor_ids` are the manual rows
    that absorbed a bank row: they keep their own id, so the bank row's id is gone.
    """

    matched_count: int = 0
    survivor_ids: list[uuid.UUID] = field(default_factory=list)


def _bank_candidates(
    db: Session, user_id: uuid.UUID, bank_ids: Sequence[uuid.UUID]
) -> list[Transaction]:
    """The rows of `bank_ids` eligible to receive a match: posted, and not part of
    a split (`parent_id` null and no children of its own) since a split's sum
    already accounts for every dollar. `bank_ids` is expected to already be
    `app.services.sync.IngestResult.newly_posted_ids` -- rows that just became
    matchable -- so the `pending` check here is a cheap second guarantee, not the
    only one.

    Ordered by `(posted_date, id)`, a fixed order that has nothing to do with
    these rows' insertion order, so a pass over the same `bank_ids` always
    matches the same pairs regardless of the order the caller's sync landed them
    in.
    """
    if not bank_ids:
        return []
    stmt = (
        select(Transaction)
        .where(
            Transaction.id.in_(bank_ids),
            Transaction.user_id == user_id,
            Transaction.source == Source.simplefin,
            Transaction.pending.is_(False),
            Transaction.parent_id.is_(None),
            ~has_split_children(),
        )
        .order_by(Transaction.posted_date, Transaction.id)
    )
    return list(db.scalars(stmt).all())


def _capture_candidates(bank: Transaction, source: Source) -> Select[Transaction]:
    """Rows of `source` on `bank`'s account with its exact amount, within `MATCH_WINDOW`
    of its date, and not part of a split, closest date first (then oldest `created_at`,
    then `id`, so the order is total).
    """
    day_diff = func.abs(Transaction.posted_date - bank.posted_date)
    return (
        select(Transaction)
        .where(
            Transaction.user_id == bank.user_id,
            Transaction.source == source,
            Transaction.account_id == bank.account_id,
            Transaction.amount == bank.amount,
            Transaction.parent_id.is_(None),
            ~has_split_children(),
            day_diff <= MATCH_WINDOW.days,
        )
        .order_by(day_diff, Transaction.created_at, Transaction.id)
    )


def _find_manual_match(db: Session, user_id: uuid.UUID, bank: Transaction) -> Transaction | None:
    """The single best still-available manual row for `bank`: same account, the
    exact same amount, `posted_date` within `MATCH_WINDOW` either direction, and
    not part of a split.

    All three tie-breaks -- closest date, then oldest `created_at`, then `id` --
    are one `ORDER BY` ahead of a `LIMIT 1`, so Postgres always returns the same
    row for the same data: there is exactly one candidate set, and exactly one
    order over it. `FOR UPDATE SKIP LOCKED` skips a row the phone is concurrently
    `PATCH`ing (`app.services.transactions.update_transaction`) rather than
    waiting on it: a skipped row is simply left unmatched for this pass. Nothing
    retries it afterwards -- a later sync only ever passes its own
    `newly_posted_ids`, not this bank row again -- so `UNMATCHED_AFTER`'s 7-day
    flag is what eventually surfaces it to the user.
    """
    stmt = _capture_candidates(bank, Source.manual).limit(1).with_for_update(skip_locked=True)
    return db.scalars(stmt).one_or_none()


def _csv_group_rows(
    db: Session, user_id: uuid.UUID, bank: Transaction, window_start: date | None
) -> tuple[list[Transaction], list[Transaction]]:
    """The imported rows and the posted SimpleFIN rows of `bank`'s account and exact
    amount that a csv pairing could involve. With a `window_start` (the first day
    the sync fetched) only csv rows at least `MATCH_WINDOW` after it count, so every
    bank row within `MATCH_WINDOW` of one of them is known to the database; a csv
    row nearer the start might have a true twin the fetch never covered. The csv rows
    are locked with `SKIP LOCKED`, like `_find_manual_match`. Trade-off: a lagged pair
    straddling the bank's history start stays two rows (a deletable duplicate, not a loss).
    """
    csv_stmt = (
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.source == Source.csv,
            Transaction.account_id == bank.account_id,
            Transaction.amount == bank.amount,
            Transaction.parent_id.is_(None),
            ~has_split_children(),
        )
        .order_by(Transaction.posted_date, Transaction.created_at, Transaction.id)
        .with_for_update(skip_locked=True)
    )
    bank_stmt = (
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.source == Source.simplefin,
            Transaction.account_id == bank.account_id,
            Transaction.amount == bank.amount,
            Transaction.pending.is_(False),
            Transaction.parent_id.is_(None),
            ~has_split_children(),
        )
        .order_by(Transaction.posted_date, Transaction.id)
    )
    if window_start is not None:
        # A bank's history can begin after the window start; a csv row dated before its
        # earliest SimpleFIN row is outside the bank's coverage and never absorbs.
        earliest = db.scalar(
            select(func.min(Transaction.posted_date)).where(
                Transaction.user_id == user_id,
                Transaction.source == Source.simplefin,
                Transaction.account_id == bank.account_id,
            )
        )
        csv_start = window_start + MATCH_WINDOW
        bank_start = window_start
        if earliest is not None:
            csv_start = max(csv_start, earliest)
            bank_start = max(bank_start, earliest)
        csv_stmt = csv_stmt.where(Transaction.posted_date >= csv_start)
        bank_stmt = bank_stmt.where(Transaction.posted_date >= bank_start)
    return list(db.scalars(csv_stmt).all()), list(db.scalars(bank_stmt).all())


def _near(a: Transaction, b: Transaction) -> bool:
    return abs((a.posted_date - b.posted_date).days) <= MATCH_WINDOW.days


def _pair_csv_rows(
    new_banks: list[Transaction], csv_rows: list[Transaction], all_banks: list[Transaction]
) -> list[tuple[Transaction, Transaction]]:
    """One-to-one `(csv_row, bank)` pairs for one account and amount. First by exact
    posted date: on a date where every posted bank row is new in this pass, the
    csv rows of that date pair off with them (any pairing is equivalent, so two
    identical coffees on one day are fine). Then each remaining new bank row pairs
    with its csv row only when that is unambiguous both ways -- exactly one csv row
    within `MATCH_WINDOW` of it, and that csv row has no other remaining bank row
    within `MATCH_WINDOW`. Anything else is left as two rows and logged.
    """
    new_ids = {bank.id for bank in new_banks}
    pairs: list[tuple[Transaction, Transaction]] = []
    paired: set[uuid.UUID] = set()
    for day in sorted({row.posted_date for row in csv_rows}):
        csv_on_day = [row for row in csv_rows if row.posted_date == day]
        banks_on_day = [row for row in all_banks if row.posted_date == day]
        if any(row.id not in new_ids for row in banks_on_day):
            continue
        for csv_row, bank in zip(csv_on_day, banks_on_day, strict=False):
            pairs.append((csv_row, bank))
            paired.update((csv_row.id, bank.id))
    csv_left = [row for row in csv_rows if row.id not in paired]
    banks_left = [row for row in all_banks if row.id not in paired]
    for bank in banks_left:
        if bank.id not in new_ids:
            continue
        candidates = [row for row in csv_left if _near(row, bank)]
        if len(candidates) != 1:
            if candidates:
                logger.info("Leaving bank row {} unmatched: several imported candidates", bank.id)
            continue
        csv_row = candidates[0]
        if [row.id for row in banks_left if _near(row, csv_row)] != [bank.id]:
            logger.info("Leaving imported row {} unmatched: several bank candidates", csv_row.id)
            continue
        pairs.append((csv_row, bank))
        csv_left.remove(csv_row)
    return pairs


def _plan_csv_matches(
    db: Session, user_id: uuid.UUID, banks: Sequence[Transaction], window_start: date | None
) -> list[tuple[Transaction, Transaction]]:
    """`_pair_csv_rows` for every account/amount group among `banks`, the newly
    posted rows no manual capture claimed.
    """
    groups: dict[tuple[uuid.UUID, object], list[Transaction]] = {}
    for bank in banks:
        groups.setdefault((bank.account_id, bank.amount), []).append(bank)
    pairs: list[tuple[Transaction, Transaction]] = []
    for group in groups.values():
        csv_rows, all_banks = _csv_group_rows(db, user_id, group[0], window_start)
        if csv_rows:
            pairs.extend(_pair_csv_rows(group, csv_rows, all_banks))
    return pairs


def lock_bank_row(db: Session, bank_id: uuid.UUID) -> Transaction | None:
    """Re-fetch and lock `bank_id` with `FOR UPDATE SKIP LOCKED`, refreshing its
    attributes from that fresh read (`populate_existing`).

    `_bank_candidates` reads `bank` with a plain, unlocked `SELECT`, and some
    time passes between that read and `absorb`'s delete. Without re-locking and
    refreshing here, a `PATCH` to the bank row (merchant, category_id, notes,
    ...) that commits in that window would be silently lost: `absorb` would
    still see the stale snapshot, delete the row anyway, and never fold the
    PATCH's values anywhere. `populate_existing` matters specifically because
    the row is already in this session's identity map from `_bank_candidates` --
    without it, a second `SELECT` for the same id just hands back the same
    (stale) Python object rather than refreshing it.

    `None` if someone else holds the row; the caller skips the pair rather than
    waiting, the same choice `_find_manual_match` makes for the manual side --
    chosen for the same reason: the sync already holds the per-user advisory
    lock for the whole run, and blocking that on an unrelated `PATCH` request
    would be a worse failure mode than leaving one pair unmatched this pass.
    """
    stmt = select(Transaction).where(Transaction.id == bank_id).with_for_update(skip_locked=True)
    return db.scalars(stmt, execution_options={"populate_existing": True}).one_or_none()


def absorb(db: Session, *, keep: Transaction, drop: Transaction) -> None:
    """Fold `drop` into `keep`, which survives under its own id, and delete `drop`.

    `keep` and `drop` describe the same real-world charge; `keep` wins the id
    because something outside the database already depends on it -- for #52,
    the phone's outbox and a late receipt upload still hold the manual row's id
    (see `match_manual_entries`); for #50 (`app.services.reconcile`), the pending
    row's id is the one the phone already holds, so the pending row is `keep` and
    the bank's re-issued posted row is `drop`.

    In order:

    1. Every inbound reference to `drop` listed in `REPOINTED_FKS` is repointed
       to `keep` while both rows still exist: `receipts.transaction_id`
       (`ON DELETE RESTRICT`, so this has to happen before the delete below or
       it fails outright), `income_entries.transaction_id` (`ON DELETE SET
       NULL` -- no error, which is exactly why it has to be done by hand instead
       of relying on the FK), and any other transaction's `transfer_pair_id`
       pointing at `drop`. If `drop` itself had its own `transfer_pair_id` and
       `keep` doesn't, `keep` inherits it, so a mutual pair survives on both
       sides. (The caller guarantees `keep` and `drop` are never both already
       paired -- see `match_manual_entries`.)
    2. `drop` is deleted and flushed, before `keep` picks up `external_id` --
       otherwise that collides with `drop`'s own row under
       `UNIQUE(account_id, external_id)` while it still exists.
    3. The bank-owned fields (`_BANK_OWNED_FIELDS`) move onto `keep`
       unconditionally: `drop`'s bank data is now the only copy.
    4. `_FILL_IF_NULL_FIELDS` fill in from `drop` only where `keep` doesn't
       already have a value -- the user's own entry on the surviving row always
       wins, even over a value a #51 rule had already set on `drop` (see that
       constant's own docstring for this as a deliberate policy, not an
       oversight). `keep.reviewed` is left untouched; it's already the user's
       own flag.
    """
    fill_values = {
        field_name: getattr(drop, field_name)
        for field_name in _FILL_IF_NULL_FIELDS
        if getattr(keep, field_name) is None
    }
    inherited_transfer_pair_id = drop.transfer_pair_id if keep.transfer_pair_id is None else None
    # An imported row keeps its own fingerprint, so re-uploading the statement still
    # recognises it (#54); the bank's fingerprint would only describe the bank's wording.
    kept_fields = ("fingerprint",) if keep.source == Source.csv else ()
    bank_owned = {
        field_name: getattr(drop, field_name)
        for field_name in _BANK_OWNED_FIELDS
        if field_name not in kept_fields
    }

    for model, column_name in REPOINTED_FKS:
        column = getattr(model, column_name)
        db.execute(update(model).where(column == drop.id).values(**{column_name: keep.id}))

    db.delete(drop)
    db.flush()

    for field_name, value in fill_values.items():
        setattr(keep, field_name, value)
    if inherited_transfer_pair_id is not None:
        keep.transfer_pair_id = inherited_transfer_pair_id
    for field_name, value in bank_owned.items():
        setattr(keep, field_name, value)
    db.flush()


def _absorb_pair(db: Session, kept: Transaction, bank: Transaction) -> bool:
    """Absorb `bank` into `kept` under a savepoint; `False` when the pair is skipped
    (`bank` is locked elsewhere, or both rows already carry a `transfer_pair_id`).
    """
    locked_bank = lock_bank_row(db, bank.id)
    if locked_bank is None:
        return False
    if kept.transfer_pair_id is not None and locked_bank.transfer_pair_id is not None:
        logger.warning(
            "Skipping manual match {} <-> {}: both already carry a transfer_pair_id",
            kept.id,
            locked_bank.id,
        )
        return False
    with db.begin_nested():
        absorb(db, keep=kept, drop=locked_bank)
    return True


def match_manual_entries(
    db: Session,
    user_id: uuid.UUID,
    bank_ids: Sequence[uuid.UUID],
    window_start: date | None = None,
) -> ManualMatchResult:
    """Match newly-posted bank rows (`bank_ids`: `app.services.sync.IngestResult.
    newly_posted_ids`) against this user's unmatched manual captures, absorbing
    each match into the manual row (see `absorb`) so it survives under its own
    id with the bank's data.

    Deterministic by construction: `_bank_candidates` fixes the order bank rows
    are considered in, and for each one `_find_manual_match` picks at most one
    manual row via a single `ORDER BY ... LIMIT 1` with no ties left unresolved.
    A bank row with no manual match may instead absorb an imported statement row
    (`Source.csv`, #54) when the pairing is certain (`_pair_csv_rows`), so a sync
    that lands after an import, a backfill included, doesn't double-count.
    `window_start` is the first day that sync fetched: csv rows nearer to it than
    `MATCH_WINDOW` are never absorbed, because their true bank twin may not have
    been fetched (`None`, for callers with no window, applies no bound).
    A matched manual row is deleted from the *bank* row's perspective -- it's
    `drop` that's deleted in `absorb`, not `keep` -- so what actually leaves the
    pool after a match is the bank row's id, and the manual row (now carrying the
    bank's data) can never be selected again as a *manual* candidate because its
    `source` is now `simplefin`. A plain greedy pass is therefore enough to give
    every manual row at most one match and every bank row at most one match.

    Each match is applied inside its own savepoint (`db.begin_nested()`): a
    failure partway through one pair (a lock timeout, say) rolls back to before
    that pair's writes rather than leaving it half-applied. The exception still
    propagates out of this call from there, caught like any other post-process
    bug by `_run_post_process`'s own savepoint (see `app.services.sync`), so a
    failure here costs the whole pass of matches, not just the one pair.

    `bank` itself is re-locked and refreshed (`lock_bank_row`) before it's
    passed to `absorb` as `drop`, not used as the stale object `_bank_candidates`
    read; see that function's docstring for why. A pair is skipped, not matched,
    when `lock_bank_row` finds the row already locked, or when `manual` and
    `bank` both already carry their own `transfer_pair_id` -- absorbing would
    have to pick one and silently break the other's pairing, so neither row is
    touched and the collision is logged instead (left for a human, or a future
    #61/#50, to sort out).
    """
    survivor_ids: list[uuid.UUID] = []
    unclaimed: list[Transaction] = []
    for bank in _bank_candidates(db, user_id, bank_ids):
        manual = _find_manual_match(db, user_id, bank)
        if manual is None:
            unclaimed.append(bank)
        elif _absorb_pair(db, manual, bank):
            survivor_ids.append(manual.id)
    for csv_row, bank in _plan_csv_matches(db, user_id, unclaimed, window_start):
        if _absorb_pair(db, csv_row, bank):
            survivor_ids.append(csv_row.id)
    return ManualMatchResult(matched_count=len(survivor_ids), survivor_ids=survivor_ids)


def has_user_links(
    db: Session, user_id: uuid.UUID, txn: Transaction, *, count_subscription: bool = True
) -> bool:
    """Notes, an event, report or subscription link (`event_id`,
    `reimbursement_id`, `subscription_id`), or any inbound reference in
    `REPOINTED_FKS` (a receipt, an income entry, a transfer pair): things the
    user attached that a merge or a pairing must not bury.
    Shared by the pending reconcile (#50) and the transfer matcher (#61).

    `count_subscription=False` ignores `subscription_id`: only the subscription
    matcher (#56) writes it, so it is no decision of the user's, and `absorb`
    carries it over (fill-if-null) when a posted row is merged.
    """
    if (
        (txn.notes or "").strip()
        or txn.event_id is not None
        or txn.reimbursement_id is not None
        or (count_subscription and txn.subscription_id is not None)
    ):
        return True
    return any(
        db.scalar(
            select(exists().where(model.user_id == user_id, getattr(model, column_name) == txn.id))
        )
        for model, column_name in REPOINTED_FKS
    )


def _transfer_eligible(
    db: Session, user_id: uuid.UUID, row: Transaction, index: rules.RuleIndex
) -> bool:
    """Unpaired and untouched by the user. A receipt, notes, an event, a
    reimbursement or income link (`has_user_links`) or a reimbursable/income
    bucket always blocks pairing, even when a rule set it. Otherwise a row the
    user already moved to `transfer` may pair, but any other bucket/category/
    merchant no rule explains (`rules.values_set_by_hand`) is left alone.
    """
    if row.transfer_pair_id is not None or row.bucket in (Bucket.reimbursable, Bucket.income):
        return False
    if has_user_links(db, user_id, row, count_subscription=False):
        return False
    return row.bucket == Bucket.transfer or not rules.values_set_by_hand(row, index)


def has_pending_twin(db: Session, row: Transaction) -> bool:
    """Whether a pending row of the same account and amount, created no later than
    `row` and within `RECONCILE_WINDOW`, could still be `row`'s pending original.
    """
    return bool(
        db.scalar(
            select(
                exists().where(
                    Transaction.user_id == row.user_id,
                    Transaction.account_id == row.account_id,
                    Transaction.amount == row.amount,
                    Transaction.source == Source.simplefin,
                    Transaction.pending.is_(True),
                    Transaction.created_at <= row.created_at,
                    func.abs(Transaction.posted_date - row.posted_date) <= RECONCILE_WINDOW.days,
                )
            )
        )
    )


def _transfer_pool(
    db: Session, user_id: uuid.UUID, seeds: Sequence[Transaction], index: rules.RuleIndex
) -> tuple[list[Transaction], set[uuid.UUID]]:
    """Every row that could be either side of a pair with one of `seeds`: posted
    bank rows (a `simplefin` row always has an `external_id`; an imported `csv` row
    has none but is bank data too; a manual-only row is neither), not part of a
    split, unpaired, of an amount matching a seed's up to sign and inside the date
    window, `_transfer_eligible`, and with no possible pending twin: while the bank
    still lists the pending original, the pending
    reconcile will absorb this row once that drops, and a pair written now would
    read as user-touched and block the absorb. Such a row is left for a later run:
    it stays in the pool so it still counts toward ambiguity, and its id is in the
    returned deferred set so it is never paired.
    """
    amounts = {amount for seed in seeds for amount in (seed.amount, -seed.amount)}
    stmt = select(Transaction).where(
        Transaction.user_id == user_id,
        or_(
            and_(Transaction.source == Source.simplefin, Transaction.external_id.is_not(None)),
            Transaction.source == Source.csv,
        ),
        Transaction.pending.is_(False),
        Transaction.parent_id.is_(None),
        ~has_split_children(),
        Transaction.transfer_pair_id.is_(None),
        Transaction.amount.in_(amounts),
        Transaction.posted_date >= min(seed.posted_date for seed in seeds) - TRANSFER_WINDOW,
        Transaction.posted_date <= max(seed.posted_date for seed in seeds) + TRANSFER_WINDOW,
    )
    pool: list[Transaction] = []
    deferred: set[uuid.UUID] = set()
    for row in db.scalars(stmt):
        if not _transfer_eligible(db, user_id, row, index):
            continue
        if has_pending_twin(db, row):
            logger.info("Deferring transfer match for {}: a pending twin is still listed", row.id)
            deferred.add(row.id)
        pool.append(row)
    return pool, deferred


def _counterparts(row: Transaction, pool: Sequence[Transaction]) -> list[Transaction]:
    """Rows of `pool` that could be the other side of `row`: opposite amount, a
    different account, within `TRANSFER_WINDOW`.
    """
    return [
        other
        for other in pool
        if other.account_id != row.account_id
        and other.amount == -row.amount
        and abs(other.posted_date - row.posted_date) <= TRANSFER_WINDOW
    ]


def _unambiguous_pairs(
    pool: Sequence[Transaction], wanted: set[uuid.UUID], deferred: set[uuid.UUID]
) -> Iterator[tuple[Transaction, Transaction]]:
    """Each `(anchor, other)` where the anchor (a row of `wanted`) and the other
    have exactly one candidate each, in `(posted_date, id)` order, a row in at
    most one pair. Anything ambiguous is logged and skipped, never guessed at. A
    `deferred` row counts as a candidate but is never paired.
    """
    anchors = sorted(
        (row for row in pool if row.id in wanted and row.amount != 0),
        key=lambda row: (row.posted_date, row.id),
    )
    paired: set[uuid.UUID] = set()
    for anchor in anchors:
        if anchor.id in paired or anchor.id in deferred:
            continue
        candidates = _counterparts(anchor, pool)
        if not candidates:
            continue
        other = candidates[0]
        if len(candidates) > 1 or len(_counterparts(other, pool)) > 1:
            logger.warning(
                "Skipping transfer match for {}: ambiguous among {} candidates",
                anchor.id,
                len(candidates),
            )
            continue
        if other.id in deferred:
            logger.info("Skipping transfer match for {}: its counterpart is deferred", anchor.id)
            continue
        paired.update((anchor.id, other.id))
        yield anchor, other


def _write_pair(
    db: Session,
    user_id: uuid.UUID,
    index: rules.RuleIndex,
    anchor: Transaction,
    other: Transaction,
) -> bool:
    """Re-lock both rows, re-check them, and pair them in one savepoint. `False`
    (logged) when a row is locked by another writer or changed since it was read.
    """
    locked_anchor = lock_bank_row(db, anchor.id)
    locked_other = lock_bank_row(db, other.id)
    if locked_anchor is None or locked_other is None:
        logger.info(
            "Skipping transfer match {} <-> {}: a row is locked by another writer",
            anchor.id,
            other.id,
        )
        return False
    if not (
        _transfer_eligible(db, user_id, locked_anchor, index)
        and _transfer_eligible(db, user_id, locked_other, index)
    ):
        logger.info(
            "Skipping transfer match {} <-> {}: a row changed since it was read",
            anchor.id,
            other.id,
        )
        return False
    with db.begin_nested():
        for row, partner in ((locked_anchor, locked_other), (locked_other, locked_anchor)):
            row.bucket = Bucket.transfer
            row.reviewed = True
            row.transfer_pair_id = partner.id
        db.flush()
    return True


def match_transfers(
    db: Session, user_id: uuid.UUID, row_ids: Sequence[uuid.UUID], index: rules.RuleIndex
) -> int:
    """Pair a negative posted bank row with the positive one of the same amount on
    another of the user's accounts within `TRANSFER_WINDOW`: a card payment, a card
    to card balance transfer. Both become `transfer` and `reviewed`, linked both
    ways through `transfer_pair_id`, so neither counts as spending. A fee or
    interest row has no counterpart and stays an expense. Returns the pair count.

    `row_ids` are the rows this run created or posted, as they exist now: the
    caller resolves rows absorbed by the manual matcher or reconcile to their
    survivors. At least one side of a pair is one of them; the other may be older.
    A pair is made only when each side has exactly one candidate (`_counterparts`):
    two candidates for either, or one counterpart wanted by two rows, is ambiguous
    and logged, never guessed at. Both rows are re-locked (`lock_bank_row`) and
    re-checked before writing; a lock miss skips the pair. Each pair is written in
    its own savepoint.
    """
    wanted = set(row_ids)
    seeds = db.scalars(
        select(Transaction).where(
            Transaction.id.in_(wanted), Transaction.user_id == user_id, Transaction.amount != 0
        )
    ).all()
    if not seeds:
        return 0
    pool, deferred = _transfer_pool(db, user_id, seeds, index)
    return sum(
        _write_pair(db, user_id, index, anchor, other)
        for anchor, other in _unambiguous_pairs(pool, wanted, deferred)
    )
