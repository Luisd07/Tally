"""Manual transaction capture, listing and editing.

See `.claude/rules/api.md` for user scoping: every client-supplied id (`account_id`,
`category_id`) is loaded through `get_owned` because the DB doesn't enforce same-user FKs.
Create and patch go through `app.services.crud`, the pattern shared with accounts/categories.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, exists, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.errors import Conflict, NotFound, UnprocessableEntity
from app.models import Account, Category, Event, Receipt, Reimbursement, Transaction
from app.models.enums import Bucket, ReimbursementStatus, Source
from app.schemas.months import month_range
from app.schemas.transactions import (
    TransactionCreate,
    TransactionListQuery,
    TransactionOut,
    TransactionPage,
    TransactionSplit,
    TransactionSplitPart,
    TransactionUpdate,
)
from app.scoping import get_owned, owned
from app.services import exports
from app.services.crud import apply_patch, check_owned_refs, create_idempotent
from app.services.matching import UNMATCHED_AFTER
from app.services.paging import filter_date_cursor, paginate
from app.services.splits import has_split_children


def _split_parent_ids(db: Session, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Ids among `ids` that are split parents (`has_split_children`)."""
    if not ids:
        return set()
    stmt = select(Transaction.id).where(Transaction.id.in_(ids), has_split_children())
    return set(db.scalars(stmt))


def _to_out_values(row: Transaction, is_split: bool) -> dict[str, Any]:
    """Every `TransactionOut` field read off `row`, plus the computed `is_split`.

    A plain dict, not `row` itself, is what gets validated: `is_split` has no
    default and isn't a column, so `TransactionOut.model_validate(row)` would fail
    with "field required" rather than silently defaulting it (see the field's
    docstring) -- this is the one place that's expected to supply it.
    """
    values: dict[str, Any] = {
        name: getattr(row, name) for name in TransactionOut.model_fields if name != "is_split"
    }
    values["is_split"] = is_split
    return values


def to_out_many(db: Session, rows: Sequence[Transaction]) -> list[TransactionOut]:
    """Serialize `rows`, filling the computed `is_split` (true for a row that's the
    parent of at least one other transaction) with one query covering all of them.
    """
    parent_ids = _split_parent_ids(db, [row.id for row in rows])
    return [
        TransactionOut.model_validate(_to_out_values(row, row.id in parent_ids)) for row in rows
    ]


def to_out(db: Session, row: Transaction) -> TransactionOut:
    return to_out_many(db, [row])[0]


def create_transaction(
    db: Session, user_id: uuid.UUID, body: TransactionCreate
) -> tuple[Transaction, bool]:
    """Idempotent create: a repeated `id` returns the existing row, ignoring the new body.

    Raises `NotFound` if `account_id`/`category_id` aren't the caller's, and `Conflict` if
    `id` already belongs to another user's row (via `create_idempotent`).
    """
    check_owned_refs(db, user_id, body, {"account_id": Account, "category_id": Category})

    values = body.model_dump()
    values.update(
        source=Source.manual,
        external_id=None,
        pending=False,
        reviewed=body.category_id is not None,
    )
    return create_idempotent(db, Transaction, user_id, values)


def update_transaction(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, body: TransactionUpdate
) -> Transaction:
    """Raises `NotFound` for a missing/foreign row, same as `get_owned` -- including
    one that existed when loaded above but was deleted by a concurrent transaction
    before this `PATCH`'s own `UPDATE` ran: that races the row out from under the
    flush inside `apply_patch`, which surfaces as `StaleDataError` rather than
    `NotFound` on its own. Vanishingly rare since `app.services.matching` stopped
    deleting a matched manual row (the survivor keeps its id); kept as a cheap
    backstop for any other path that still deletes a row a `PATCH` could be racing.
    """
    row = get_owned(db, Transaction, id, user_id)
    check_owned_refs(db, user_id, body, {"category_id": Category, "event_id": Event})
    try:
        return apply_patch(db, row, body)
    except StaleDataError:
        raise NotFound() from None


def bulk_update_transactions(
    db: Session, user_id: uuid.UUID, ids: list[uuid.UUID], patch: TransactionUpdate
) -> list[Transaction]:
    """Apply one `patch` to every row in `ids`, all-or-nothing.

    Every id must be the caller's own `Transaction`; a missing or foreign id raises
    `NotFound` before anything is touched, the same as a single `PATCH .../{id}`. FK
    refs on the patch are the same for every row, so they're checked once rather than
    once per row. Applies via `apply_patch` per row (reusing its unique-conflict
    handling and the model's `onupdate` for `updated_at`) rather than one bulk `UPDATE`,
    since 1..500 rows is cheap and this keeps the same code path PATCH already uses.
    """
    check_owned_refs(db, user_id, patch, {"category_id": Category, "event_id": Event})

    rows = db.scalars(
        select(Transaction).where(Transaction.id.in_(ids), Transaction.user_id == user_id)
    ).all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(ids):
        raise NotFound()

    for id in ids:
        apply_patch(db, by_id[id], patch)
    return [by_id[id] for id in ids]


def _lock_parent(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> Transaction:
    """Fetch `id`/`user_id`'s row with `FOR UPDATE`, so two concurrent splits of the
    same parent serialize: the second waits for the first's commit (or rollback)
    before it reads `old_children`, rather than racing it under READ COMMITTED and
    leaving two sets of children that no longer sum to the parent.

    Same "missing or someone else's" -> `NotFound` shape as `get_owned`, which has
    no locking variant.
    """
    row = db.scalars(
        select(Transaction)
        .where(Transaction.id == id, Transaction.user_id == user_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise NotFound()
    return row


def _validate_parts(
    db: Session, user_id: uuid.UUID, row: Transaction, body: TransactionSplit
) -> None:
    """Raise `NotFound` for a part's foreign `category_id`/`event_id`, and
    `UnprocessableEntity` if the parts don't sum to `row`'s amount with its sign.
    """
    for part in body.parts:
        check_owned_refs(db, user_id, part, {"category_id": Category, "event_id": Event})

    total = sum((part.amount for part in body.parts), Decimal("0.00"))
    if total != row.amount:
        raise UnprocessableEntity.value_error("parts must sum to the transaction's amount")
    negative = row.amount < 0
    if any((part.amount < 0) != negative for part in body.parts):
        raise UnprocessableEntity.value_error(
            "each part must have the same sign as the transaction"
        )


def _replace_children(db: Session, user_id: uuid.UUID, row: Transaction) -> None:
    """Delete `row`'s existing children (a no-op the first time it's split), scoped
    to `user_id` since the DB doesn't enforce same-user `parent_id` (`app.services.
    splits`).

    Raises `Conflict` if a child has since picked up its own receipt: `receipts.
    transaction_id` is `ON DELETE RESTRICT`, and deleting a child out from under its
    receipt would otherwise surface as a raw `IntegrityError`. That check is *not*
    also filtered on `Receipt.user_id`: `old_ids` already only names this user's own
    children, and the RESTRICT constraint fires regardless of who owns the
    referencing receipt, so narrowing by receipt ownership could let a delete
    through that the database would then reject anyway.
    """
    old_children = db.scalars(
        select(Transaction).where(Transaction.parent_id == row.id, Transaction.user_id == user_id)
    ).all()
    if not old_children:
        return
    old_ids = [child.id for child in old_children]
    has_receipts = db.scalar(select(exists().where(Receipt.transaction_id.in_(old_ids))))
    if has_receipts:
        raise Conflict("A part of this split has a receipt; move it before re-splitting.")
    for child in old_children:
        db.delete(child)
    db.flush()


def _inherit(part: TransactionSplitPart, field: str, parent_value: Any) -> Any:
    """`part`'s own value for `field` if the request set it (explicitly, including
    `null`), otherwise `parent_value` -- the field was left out entirely.
    """
    return getattr(part, field) if field in part.model_fields_set else parent_value


def _build_children(
    user_id: uuid.UUID, row: Transaction, parts: Sequence[TransactionSplitPart]
) -> list[Transaction]:
    """One new child per part: the parent's account, dates, source, `description_raw`
    and `external_id=None`; always `pending=False` (`row.pending` already guarded to
    be `False`, see `split_transaction`) and `reviewed=True`. `category_id`/`bucket`/
    `event_id` fall back to the parent's own value when a part omits them (see
    `TransactionSplitPart`); `merchant`/`notes` are never inherited.
    """
    return [
        Transaction(
            user_id=user_id,
            account_id=row.account_id,
            parent_id=row.id,
            posted_date=row.posted_date,
            transacted_at=row.transacted_at,
            amount=part.amount,
            description_raw=row.description_raw,
            merchant=part.merchant,
            category_id=_inherit(part, "category_id", row.category_id),
            bucket=_inherit(part, "bucket", row.bucket),
            event_id=_inherit(part, "event_id", row.event_id),
            source=row.source,
            external_id=None,
            pending=False,
            reviewed=True,
            notes=part.notes,
        )
        for part in parts
    ]


def _split_report_id(db: Session, user_id: uuid.UUID, row: Transaction) -> uuid.UUID | None:
    """The expense report `row` (or, on a re-split, its existing children) sits in,
    for `split_transaction` to move onto the new reimbursable children.

    Raises `Conflict` when that report isn't a `draft`: a submitted or paid report's
    total and what it's owed are settled, and a split would change both. Returns
    `None` when nothing is linked, or when the children are in different reports
    (nothing to carry over unambiguously; the user re-attaches).
    """
    report_ids = set(
        db.scalars(
            select(Transaction.reimbursement_id).where(
                Transaction.user_id == user_id,
                Transaction.reimbursement_id.is_not(None),
                (Transaction.id == row.id) | (Transaction.parent_id == row.id),
            )
        )
    )
    if not report_ids:
        return None
    in_filed_report = db.scalar(
        select(
            exists().where(
                Reimbursement.id.in_(report_ids), Reimbursement.status != ReimbursementStatus.draft
            )
        )
    )
    if in_filed_report:
        raise Conflict("This transaction is in a submitted or paid report; it can't be split.")
    return next(iter(report_ids)) if len(report_ids) == 1 else None


def split_transaction(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, body: TransactionSplit
) -> tuple[Transaction, list[Transaction]]:
    """Replace `id` with one new child row per part, keeping the parent's own row
    (and its `external_id`) so a re-sync can't duplicate it; the parent is also
    marked `reviewed`, since splitting it is itself a classification. Re-splitting
    an already-split parent replaces its children, deleted first in the same
    transaction the new rows are inserted in.

    Locks the parent row for the duration (`_lock_parent`), so two concurrent splits
    of the same parent serialize rather than racing into two sets of children.

    Raises `NotFound` for a missing/foreign transaction or a part's foreign
    `category_id`/`event_id`, `UnprocessableEntity` for a child, a pending row, or
    parts that don't sum to the parent's amount with its sign, and `Conflict` if
    re-splitting would delete a child that's since picked up its own receipt, or if
    the transaction is in a submitted or paid expense report. In a draft report the
    link moves from the parent to the new `reimbursable` children (the others are
    left out of it), so the report never counts a split parent.
    """
    row = _lock_parent(db, user_id, id)
    if row.parent_id is not None:
        raise UnprocessableEntity.value_error("a split transaction cannot itself be split")
    if row.pending:
        raise UnprocessableEntity.value_error("a pending transaction cannot be split")

    _validate_parts(db, user_id, row, body)
    report_id = _split_report_id(db, user_id, row)
    _replace_children(db, user_id, row)

    children = _build_children(user_id, row, body.parts)
    if report_id is not None:
        row.reimbursement_id = None
        for child in children:
            if child.bucket == Bucket.reimbursable:
                child.reimbursement_id = report_id
    db.add_all(children)
    row.reviewed = True
    db.flush()
    return row, children


def split_into(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, parts: Sequence[tuple[Decimal, Bucket]]
) -> tuple[Transaction, list[Transaction]]:
    """`split_transaction` for server-side callers: one child per (amount, bucket)."""
    body = TransactionSplit(
        parts=[TransactionSplitPart(amount=amount, bucket=bucket) for amount, bucket in parts]
    )
    return split_transaction(db, user_id, id, body)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_month[*Ts](stmt: Select[*Ts], month: str | None) -> Select[*Ts]:
    if month is None:
        return stmt
    start, end = month_range(month)
    return stmt.where(Transaction.posted_date >= start, Transaction.posted_date < end)


def _filter_search[*Ts](stmt: Select[*Ts], q: str | None) -> Select[*Ts]:
    if q is None:
        return stmt
    pattern = f"%{_escape_like(q)}%"
    return stmt.where(
        Transaction.description_raw.ilike(pattern, escape="\\")
        | Transaction.merchant.ilike(pattern, escape="\\")
        | Transaction.notes.ilike(pattern, escape="\\")
    )


def _simplefin_account_exists() -> ColumnElement[bool]:
    """`EXISTS`, true when the row's own `account_id` belongs to an account the
    Bridge actually syncs. Correlated to the enclosing query's `Transaction`, the
    same pattern as `app.services.splits.has_split_children`, so it works whether
    that query selects plain `Transaction` entities or `export_select`'s labeled
    columns.
    """
    return (
        select(Account.id)
        .where(Account.id == Transaction.account_id, Account.simplefin_account_id.is_not(None))
        .exists()
    )


def unmatched_manual_clause(now: datetime) -> ColumnElement[bool]:
    """#53's Inbox flag (spec "Receipts": "No match after 7 days..."). Computed, not
    stored: `app.services.matching.match_manual_entries` folds a matched manual row
    into its bank row the moment it matches, so a manual row that's still here
    `UNMATCHED_AFTER` its own `posted_date` is, by construction, unmatched.

    Excludes a manual row with no bank to ever match against -- a cash or other
    account the Bridge doesn't sync (`simplefin_account_id IS NULL`; spec "Credit
    cards": "Cards the Bridge can't connect stay manual") -- and a split child,
    which is flagged next to its own parent otherwise.
    """
    cutoff = now.date() - UNMATCHED_AFTER
    return and_(
        Transaction.source == Source.manual,
        Transaction.posted_date <= cutoff,
        Transaction.parent_id.is_(None),
        _simplefin_account_exists(),
    )


def _filter_unmatched_manual[*Ts](stmt: Select[*Ts], flag: bool, now: datetime) -> Select[*Ts]:
    return stmt.where(unmatched_manual_clause(now)) if flag else stmt


def _apply_filters[*Ts](
    stmt: Select[*Ts], query: TransactionListQuery, now: datetime
) -> Select[*Ts]:
    """The filters `list_transactions` and the CSV export (`stream_export_rows`)
    share: everything on `TransactionListQuery` except `cursor`/`limit`, which only
    mean anything for the paginated JSON list. Generic over the selected columns
    (`*Ts`): the two callers select different things (`Transaction` entities vs.
    `app.services.exports.export_select`'s labeled columns), and `Select`'s type
    parameters are invariant, so a fixed `Select[Transaction]` or `Select[Any]` here
    would only accept one of them.
    """
    stmt = _filter_month(stmt, query.month)
    if query.account_id is not None:
        stmt = stmt.where(Transaction.account_id == query.account_id)
    if query.bucket is not None:
        stmt = stmt.where(Transaction.bucket == query.bucket)
    if query.category_id is not None:
        stmt = stmt.where(Transaction.category_id == query.category_id)
    if query.event_id is not None:
        stmt = stmt.where(Transaction.event_id == query.event_id)
    if query.reimbursement_id is not None:
        stmt = stmt.where(Transaction.reimbursement_id == query.reimbursement_id)
    if query.reviewed is not None:
        stmt = stmt.where(Transaction.reviewed == query.reviewed)
    if query.pending is not None:
        stmt = stmt.where(Transaction.pending == query.pending)
    if query.exclude_split_parents:
        stmt = stmt.where(~has_split_children())
    stmt = _filter_unmatched_manual(stmt, query.unmatched_manual, now)
    return _filter_search(stmt, query.q)


def list_transactions(
    db: Session, user_id: uuid.UUID, query: TransactionListQuery, now: datetime
) -> TransactionPage:
    stmt = owned(Transaction, user_id)
    stmt = _apply_filters(stmt, query, now)
    stmt = filter_date_cursor(stmt, Transaction.posted_date, Transaction.id, query.cursor)

    stmt = stmt.order_by(Transaction.posted_date.desc(), Transaction.id.desc())
    rows, next_cursor = paginate(db, stmt, query.limit, lambda row: (row.posted_date, row.id))

    return TransactionPage(items=to_out_many(db, rows), next_cursor=next_cursor)


# --- CSV export (#73) -------------------------------------------------------------------

# Rows per round trip to Postgres while streaming the CSV export, so a large export's
# memory footprint stays flat instead of holding every matching row at once. Passed as
# the `yield_per` *execution option* (not `Result.yield_per()`, called after `execute`),
# which is what actually makes psycopg open a server-side cursor; without it, the whole
# result ships from Postgres to the client before the first row is yielded regardless
# of how the `Result` is iterated afterwards.
EXPORT_BATCH_SIZE = 500


def stream_export_rows(db: Session, user_id: uuid.UUID, query: TransactionListQuery, now: datetime):
    """Every row `GET /api/transactions?format=csv` matches: the same filters as
    `list_transactions` (minus `cursor`/`limit`, which only mean anything for the
    paginated JSON list) narrowing `app.services.exports.export_select`, fetched from
    Postgres over a server-side cursor in `EXPORT_BATCH_SIZE`-row batches rather than
    all at once, for the router to hand to `app.services.exports.stream_transaction_csv`.
    No return annotation: `export_select`'s columns are built from a column spec, not a
    fixed tuple, so pyright infers their exact type instead of a hand-written one.
    """
    stmt = _apply_filters(exports.export_select(user_id), query, now)
    stmt = stmt.order_by(Transaction.posted_date.desc(), Transaction.id.desc())
    return db.execute(stmt.execution_options(yield_per=EXPORT_BATCH_SIZE))
