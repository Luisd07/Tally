"""Event creation and updates. See `app.scoping` for the user-scoping helpers."""

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Event, IncomeEntry, Transaction
from app.schemas.events import EventCreate, EventListItem, EventUpdate
from app.scoping import get_owned, owned
from app.services.crud import apply_patch, check_date_order, create_idempotent
from app.services.splits import has_split_children

_ZERO = Decimal("0.00")


def _totals_by_event(
    db: Session, user_id: uuid.UUID, event_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """Costs (SimpleFIN-signed transaction amounts) and pay (received income, NULL as
    0) per event id, scoped to `user_id`.

    Two aggregated queries covering every id in `event_ids` at once, not one per
    event, so a list of N events costs a constant two queries rather than N+1. An id
    with no linked rows on one side (or either side) is absent from that query's
    result and defaults to zero here. `~has_split_children()` drops a split parent
    from the cost sum: its children (which can inherit its `event_id`, see #80)
    already count the same spend, same as the dashboard and tax totals.
    """
    if not event_ids:
        return {}
    costs = dict(
        db.execute(
            select(Transaction.event_id, func.sum(Transaction.amount))
            .where(
                Transaction.user_id == user_id,
                Transaction.event_id.in_(event_ids),
                ~has_split_children(),
            )
            .group_by(Transaction.event_id)
        ).all()
    )
    pay = dict(
        db.execute(
            select(IncomeEntry.event_id, func.sum(func.coalesce(IncomeEntry.received, 0)))
            .where(IncomeEntry.user_id == user_id, IncomeEntry.event_id.in_(event_ids))
            .group_by(IncomeEntry.event_id)
        ).all()
    )
    return {
        event_id: (costs.get(event_id, _ZERO), pay.get(event_id, _ZERO)) for event_id in event_ids
    }


def _to_list_item(row: Event, totals: dict[uuid.UUID, tuple[Decimal, Decimal]]) -> EventListItem:
    costs_total, pay_total = totals.get(row.id, (_ZERO, _ZERO))
    return EventListItem(
        id=row.id,
        name=row.name,
        series=row.series,
        start_date=row.start_date,
        end_date=row.end_date,
        location=row.location,
        created_at=row.created_at,
        updated_at=row.updated_at,
        costs_total=costs_total,
        pay_total=pay_total,
    )


def list_events(db: Session, user_id: uuid.UUID, year: int | None) -> list[EventListItem]:
    stmt = owned(Event, user_id).order_by(Event.start_date.desc())
    if year is not None:
        stmt = stmt.where(Event.start_date.between(date(year, 1, 1), date(year, 12, 31)))
    rows = list(db.scalars(stmt))
    totals = _totals_by_event(db, user_id, [row.id for row in rows])
    return [_to_list_item(row, totals) for row in rows]


def create_event(db: Session, user_id: uuid.UUID, data: EventCreate) -> tuple[Event, bool]:
    return create_idempotent(db, Event, user_id, data.model_dump())


def update_event(db: Session, user_id: uuid.UUID, id: uuid.UUID, data: EventUpdate) -> Event:
    """Apply a partial update, checking the merged start/end dates before writing.

    A one-sided date change (only `start_date` or only `end_date` sent) is checked
    against the row's current other date, so a bad combination is a 422 here rather
    than an `IntegrityError` from the table's CHECK.
    """
    event = get_owned(db, Event, id, user_id)
    check_date_order(event, data)
    return apply_patch(db, event, data)
