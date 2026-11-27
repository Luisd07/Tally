"""Income entry creation, updates and the Income tab list. See `app.scoping` for the
user-scoping helpers.
"""

import uuid
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.errors import UnprocessableEntity
from app.models import Event, IncomeEntry, Transaction
from app.schemas.income import (
    IncomeCreate,
    IncomeListQuery,
    IncomeOut,
    IncomePage,
    IncomeTotals,
    IncomeUpdate,
)
from app.schemas.months import month_range
from app.scoping import get_owned, owned
from app.services.crud import apply_patch, check_owned_refs, create_idempotent, merged

_ZERO = Decimal("0.00")


def create_income(db: Session, user_id: uuid.UUID, data: IncomeCreate) -> tuple[IncomeEntry, bool]:
    """Idempotent create: a repeated `id` returns the existing row, ignoring the new body.

    Raises `NotFound` if `transaction_id`/`event_id` aren't the caller's, and `Conflict`
    if `id` already belongs to another user's row (via `create_idempotent`).
    """
    check_owned_refs(db, user_id, data, {"transaction_id": Transaction, "event_id": Event})
    return create_idempotent(db, IncomeEntry, user_id, data.model_dump())


def update_income(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, data: IncomeUpdate
) -> IncomeEntry:
    """Apply a partial update, checking the merged expected/received before writing.

    A one-sided clear (only `expected` or only `received` sent as null) is checked
    against the row's current other amount, so a bad combination is a 422 here
    rather than an `IntegrityError` from the table's CHECK.
    """
    entry = get_owned(db, IncomeEntry, id, user_id)
    check_owned_refs(db, user_id, data, {"transaction_id": Transaction, "event_id": Event})

    expected = merged(entry, data, "expected")
    received = merged(entry, data, "received")
    if expected is None and received is None:
        raise UnprocessableEntity.value_error("expected or received must be set")
    return apply_patch(db, entry, data)


def _filtered_income(user_id: uuid.UUID, query: IncomeListQuery) -> Select[IncomeEntry]:
    stmt = owned(IncomeEntry, user_id)
    if query.month is not None:
        start, end = month_range(query.month)
        stmt = stmt.where(IncomeEntry.date >= start, IncomeEntry.date < end)
    if query.type is not None:
        stmt = stmt.where(IncomeEntry.type == query.type)
    if query.event_id is not None:
        stmt = stmt.where(IncomeEntry.event_id == query.event_id)
    return stmt


def list_income(db: Session, user_id: uuid.UUID, query: IncomeListQuery) -> IncomePage:
    stmt = _filtered_income(user_id, query).order_by(IncomeEntry.date.desc(), IncomeEntry.id.desc())
    rows = list(db.scalars(stmt))

    filtered = _filtered_income(user_id, query).subquery()
    totals_stmt = select(
        func.coalesce(func.sum(filtered.c.expected), 0),
        func.coalesce(func.sum(filtered.c.received), 0),
    )
    expected_total, received_total = db.execute(totals_stmt).one()

    return IncomePage(
        items=[IncomeOut.model_validate(row) for row in rows],
        totals=IncomeTotals(expected=expected_total or _ZERO, received=received_total or _ZERO),
    )
