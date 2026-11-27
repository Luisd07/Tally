"""Expense report creation, status transitions, the owed-to-you total (#45's Home
tab) and the Reimbursements tab list. See `app.scoping` for the user-scoping
helpers.

Status only moves forward, `draft` -> `submitted` -> `paid`, one step at a time;
there's no path back to `draft` once filed -- the spec never asks for an "unsubmit",
and a report that shouldn't have been filed can simply not be marked paid.
`add_transaction_ids`/`remove_transaction_ids` are only honored while a report is
still `draft`; `note` can be edited at any status, since it doesn't affect the
report's total or its lifecycle. Marking paid requires an `income_entry_id` of
type `reimbursement` -- the spec's "Mark paid links the income entry" is read as
mandatory, not optional, since linking *is* what marking paid means here -- and
one income entry can settle at most one report. `submitted_date` only exists once
a report is submitted, `paid_date`/`income_entry_id` once it's paid, and a paid
report's dates and income link no longer change.
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.orm import Session

from app.errors import Conflict, NotFound, UnprocessableEntity
from app.models import IncomeEntry, Reimbursement, Transaction
from app.models.enums import Bucket, IncomeType, ReimbursementStatus
from app.schemas.reimbursements import (
    ReimbursementCreate,
    ReimbursementListQuery,
    ReimbursementOut,
    ReimbursementPage,
    ReimbursementUpdate,
)
from app.scoping import get_owned
from app.services.crud import apply_patch, create_idempotent, merged
from app.services.splits import has_split_children

_ZERO = Decimal("0.00")

# A status only ever advances one step; there's no entry for `paid`, so trying to
# move off of it (back to `draft`/`submitted`, or nowhere) is always rejected.
_FORWARD_TRANSITIONS = {
    ReimbursementStatus.draft: ReimbursementStatus.submitted,
    ReimbursementStatus.submitted: ReimbursementStatus.paid,
}

# Not table columns: `_plan_transactions` handles them, `update_reimbursement` leaves
# them out of the generic column patch.
_NON_COLUMN_FIELDS = frozenset({"add_transaction_ids", "remove_transaction_ids"})

_INCOME_ENTRY_TAKEN = "This income entry is already linked to another report."


def _counted() -> tuple[ColumnElement[bool], ...]:
    """The one rule for which transactions count toward a report's `total`/
    `transaction_count` and toward `owed_to_you`: reimbursable bucket, settled, and
    not a split parent (its children count instead). A linked row that stops
    matching (re-bucketed, say) drops out of both, so they can't disagree.
    """
    return (
        Transaction.bucket == Bucket.reimbursable,
        Transaction.pending.is_(False),
        ~has_split_children(),
    )


def _totals(user_id: uuid.UUID) -> Select[uuid.UUID | None, Decimal, int]:
    """Per-report `(reimbursement_id, total, transaction_count)` over `_counted` rows."""
    return (
        select(
            Transaction.reimbursement_id.label("reimbursement_id"),
            func.sum(-Transaction.amount).label("total"),
            func.count(Transaction.id).label("transaction_count"),
        )
        .where(Transaction.user_id == user_id, Transaction.reimbursement_id.is_not(None))
        .where(*_counted())
        .group_by(Transaction.reimbursement_id)
    )


def _split_parent_ids(db: Session, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Ids among `ids` that are split parents (`app.services.splits.
    has_split_children`)."""
    if not ids:
        return set()
    stmt = select(Transaction.id).where(Transaction.id.in_(ids), has_split_children())
    return set(db.scalars(stmt))


def _load_owned_transactions(
    db: Session, user_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> list[Transaction]:
    """The caller's transactions for `ids` in one query, in `ids` order (repeats
    collapsed); `NotFound` if any isn't theirs, like `get_owned`.
    """
    unique_ids = list(dict.fromkeys(ids))
    rows = db.scalars(
        select(Transaction).where(Transaction.id.in_(unique_ids), Transaction.user_id == user_id)
    ).all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(unique_ids):
        raise NotFound()
    return [by_id[id] for id in unique_ids]


def _attachable_rows(
    db: Session,
    user_id: uuid.UUID,
    reimbursement: Reimbursement,
    transaction_ids: Sequence[uuid.UUID],
) -> list[Transaction]:
    """The rows for `transaction_ids`, checked as attachable to `reimbursement`.

    Each must be the caller's own transaction (`NotFound` otherwise), bucket
    `reimbursable`, settled and not a split parent (`UnprocessableEntity`
    otherwise), and not already linked to a *different* report (`Conflict`
    otherwise); one already linked to this same report is accepted as a no-op.
    """
    rows = _load_owned_transactions(db, user_id, transaction_ids)
    split_parent_ids = _split_parent_ids(db, [row.id for row in rows])
    for row in rows:
        if row.bucket != Bucket.reimbursable:
            raise UnprocessableEntity.value_error(
                "only reimbursable-bucket transactions can be attached to a report"
            )
        if row.pending:
            raise UnprocessableEntity.value_error("a pending transaction cannot be attached")
        if row.id in split_parent_ids:
            raise UnprocessableEntity.value_error("a split parent cannot be attached to a report")
        if row.reimbursement_id is not None and row.reimbursement_id != reimbursement.id:
            raise Conflict("This transaction is already in another report.")
    return rows


def _plan_transactions(
    db: Session, user_id: uuid.UUID, reimbursement: Reimbursement, data: ReimbursementUpdate
) -> tuple[list[Transaction], list[Transaction]]:
    """Validate `data`'s `add_`/`remove_transaction_ids` and return the rows to
    attach and to detach, without touching either.

    Both lists are only honored while the report is `draft` and must name the
    caller's own transactions (`NotFound` otherwise); an id in both lists is a
    `UnprocessableEntity`. A row to detach that isn't linked to *this* report
    (unlinked, or in another) is left alone by the caller.
    """
    add_ids = data.add_transaction_ids or []
    remove_ids = data.remove_transaction_ids or []
    if not add_ids and not remove_ids:
        return [], []
    if reimbursement.status != ReimbursementStatus.draft:
        raise UnprocessableEntity.value_error(
            "transactions can only be added to or removed from a draft report"
        )
    if set(add_ids) & set(remove_ids):
        raise UnprocessableEntity.value_error(
            "a transaction cannot be both added and removed in one request"
        )
    to_attach = _attachable_rows(db, user_id, reimbursement, add_ids) if add_ids else []
    to_detach = _load_owned_transactions(db, user_id, remove_ids) if remove_ids else []
    return to_attach, to_detach


def _check_transition(current: ReimbursementStatus, target: ReimbursementStatus) -> None:
    if _FORWARD_TRANSITIONS.get(current) != target:
        raise UnprocessableEntity.value_error(f"cannot move a {current} report to {target}")


def _check_dates(
    reimbursement: Reimbursement, data: ReimbursementUpdate, status: ReimbursementStatus
) -> None:
    """Mirror the table's date CHECKs against the merged result, so a bad
    combination is a 422 here rather than an `IntegrityError` from the DB, and keep
    a date off a report that hasn't reached that step: `submitted_date` needs a
    submitted or paid report, `paid_date` a paid one.
    """
    submitted_date = merged(reimbursement, data, "submitted_date")
    paid_date = merged(reimbursement, data, "paid_date")
    filed = status in (ReimbursementStatus.submitted, ReimbursementStatus.paid)
    if filed and submitted_date is None:
        raise UnprocessableEntity.value_error(
            "submitted_date is required once a report is submitted"
        )
    if not filed and submitted_date is not None:
        raise UnprocessableEntity.value_error("submitted_date is only allowed once submitted")
    if status == ReimbursementStatus.paid and paid_date is None:
        raise UnprocessableEntity.value_error("paid_date is required once a report is paid")
    if status != ReimbursementStatus.paid and paid_date is not None:
        raise UnprocessableEntity.value_error("paid_date is only allowed once paid")
    if submitted_date is not None and paid_date is not None and paid_date < submitted_date:
        raise UnprocessableEntity.value_error("paid_date cannot be before submitted_date")


def _check_paid_is_frozen(reimbursement: Reimbursement, data: ReimbursementUpdate) -> None:
    """A paid report's dates and income link are settled: a request may repeat the
    current values but not change them.
    """
    if reimbursement.status != ReimbursementStatus.paid:
        return
    for field in ("submitted_date", "paid_date", "income_entry_id"):
        if field in data.model_fields_set and getattr(data, field) != getattr(reimbursement, field):
            raise UnprocessableEntity.value_error(f"{field} cannot change on a paid report")


def _check_income_entry(
    db: Session,
    user_id: uuid.UUID,
    reimbursement: Reimbursement,
    data: ReimbursementUpdate,
    status: ReimbursementStatus,
) -> None:
    """The merged `income_entry_id` is required to mark a report paid and only
    allowed on a paid one (`UnprocessableEntity`). When this request sets it, it
    must be the caller's own (`NotFound` via `get_owned`), of type `reimbursement`
    (`UnprocessableEntity`) and not already linked to another report (`Conflict`).
    """
    income_entry_id = merged(reimbursement, data, "income_entry_id")
    paid = status == ReimbursementStatus.paid
    if paid and income_entry_id is None:
        raise UnprocessableEntity.value_error("income_entry_id is required to mark a report paid")
    if not paid and income_entry_id is not None:
        raise UnprocessableEntity.value_error("income_entry_id is only allowed once paid")
    if "income_entry_id" not in data.model_fields_set or income_entry_id is None:
        return
    entry = get_owned(db, IncomeEntry, income_entry_id, user_id)
    if entry.type != IncomeType.reimbursement:
        raise UnprocessableEntity.value_error(
            "income_entry_id must be a reimbursement income entry"
        )
    taken = db.scalar(
        select(
            exists().where(
                Reimbursement.income_entry_id == entry.id, Reimbursement.id != reimbursement.id
            )
        )
    )
    if taken:
        raise Conflict(_INCOME_ENTRY_TAKEN)


def _validate_update(
    db: Session, user_id: uuid.UUID, reimbursement: Reimbursement, data: ReimbursementUpdate
) -> None:
    """Every check on the status, dates and income link `data` would produce,
    against the merged result; raises before anything is changed.
    """
    target_status = merged(reimbursement, data, "status")
    if "status" in data.model_fields_set and target_status != reimbursement.status:
        _check_transition(reimbursement.status, target_status)
    _check_paid_is_frozen(reimbursement, data)
    _check_dates(reimbursement, data, target_status)
    _check_income_entry(db, user_id, reimbursement, data, target_status)


def create_reimbursement(
    db: Session, user_id: uuid.UUID, data: ReimbursementCreate
) -> tuple[Reimbursement, bool]:
    """Idempotent create (`app.services.crud.create_idempotent`): a repeated `id`
    returns the existing row unchanged, without re-attaching `transaction_ids`
    (see `ReimbursementCreate`). Raises `NotFound`/`UnprocessableEntity`/`Conflict`
    from `_attachable_rows` for a bad transaction, and `Conflict` for `id`
    already belonging to another user's row.
    """
    row, created = create_idempotent(db, Reimbursement, user_id, {"id": data.id, "note": data.note})
    if created and data.transaction_ids:
        for transaction in _attachable_rows(db, user_id, row, data.transaction_ids):
            transaction.reimbursement_id = row.id
        db.flush()
    return row, created


def update_reimbursement(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, data: ReimbursementUpdate
) -> Reimbursement:
    """Apply a PATCH in three steps: validate everything (`_plan_transactions`,
    `_validate_update`) so a bad request changes nothing, then attach/detach the
    planned transactions, then patch the report's own columns.
    """
    reimbursement = get_owned(db, Reimbursement, id, user_id)
    to_attach, to_detach = _plan_transactions(db, user_id, reimbursement, data)
    _validate_update(db, user_id, reimbursement, data)

    for row in to_attach:
        row.reimbursement_id = reimbursement.id
    for row in to_detach:
        if row.reimbursement_id == reimbursement.id:
            row.reimbursement_id = None

    columns = ReimbursementUpdate.model_validate(
        data.model_dump(exclude=set(_NON_COLUMN_FIELDS), exclude_unset=True)
    )
    return apply_patch(
        db,
        reimbursement,
        columns,
        unique_conflicts={"uq_reimbursements_income_entry_id": _INCOME_ENTRY_TAKEN},
    )


def _serialize(
    row: Reimbursement, total: Decimal | None, transaction_count: int | None
) -> ReimbursementOut:
    values = {
        name: getattr(row, name)
        for name in ReimbursementOut.model_fields
        if name not in ("total", "transaction_count")
    }
    values["total"] = total if total is not None else _ZERO
    values["transaction_count"] = transaction_count or 0
    return ReimbursementOut.model_validate(values)


def to_out(db: Session, user_id: uuid.UUID, row: Reimbursement) -> ReimbursementOut:
    """Serialize one row (after create/update) with the same `total`/
    `transaction_count` the list endpoint computes.
    """
    found = db.execute(_totals(user_id).where(Transaction.reimbursement_id == row.id)).one_or_none()
    if found is None:
        return _serialize(row, None, None)
    return _serialize(row, found.total, found.transaction_count)


def list_reimbursements(
    db: Session, user_id: uuid.UUID, query: ReimbursementListQuery
) -> ReimbursementPage:
    """One query for the rows, outer-joined against one aggregate subquery for
    their totals -- no per-row query, however many reports there are.
    """
    totals = _totals(user_id).subquery()

    stmt = (
        select(Reimbursement, totals.c.total, totals.c.transaction_count)
        .where(Reimbursement.user_id == user_id)
        .outerjoin(totals, totals.c.reimbursement_id == Reimbursement.id)
    )
    if query.status is not None:
        stmt = stmt.where(Reimbursement.status == query.status)
    stmt = stmt.order_by(Reimbursement.created_at.desc(), Reimbursement.id.desc())

    rows = db.execute(stmt).all()
    return ReimbursementPage(items=[_serialize(row, total, count) for row, total, count in rows])


def owed_to_you(db: Session, user_id: uuid.UUID) -> Decimal:
    """What you are currently owed (spec "Reimbursable expenses": *"The Home
    tab shows one number: what you are currently owed"*): the sum of
    magnitudes of the `_counted` transactions (the same rule as a report's `total`)
    that are either in no report at all or in a report that isn't yet `paid`.

    Doesn't look at `reimbursements_on_1099` -- that setting only changes the tax
    view, not what's still owed back to you.
    """
    unpaid_report_ids = select(Reimbursement.id).where(
        Reimbursement.user_id == user_id, Reimbursement.status != ReimbursementStatus.paid
    )
    stmt = select(func.coalesce(func.sum(-Transaction.amount), _ZERO)).where(
        Transaction.user_id == user_id,
        *_counted(),
        (Transaction.reimbursement_id.is_(None))
        | (Transaction.reimbursement_id.in_(unpaid_report_ids)),
    )
    return db.scalar(stmt) or _ZERO
