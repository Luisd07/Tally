import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Self

from pydantic import ConfigDict, model_validator

from app.schemas.base import PatchSchema, Schema
from app.schemas.types import Name100, Text100, Text200


class EventCreate(Schema):
    """Fields the phone may set on create."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: Name100
    series: Text100 | None = None
    start_date: date
    end_date: date
    location: Text200 | None = None

    @model_validator(mode="after")
    def _end_on_or_after_start(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class EventUpdate(PatchSchema):
    """Partial update; only fields present in the request body are applied.

    Cross-field date validation against the merged result happens in
    `app.services.events.update_event`, since this schema alone can't see the row's
    other date when only one is sent.
    """

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"name", "start_date", "end_date"})

    name: Name100 | None = None
    series: Text100 | None = None
    start_date: date | None = None
    end_date: date | None = None
    location: Text200 | None = None


class EventOut(Schema):
    id: uuid.UUID
    name: str
    series: str | None
    start_date: date
    end_date: date
    location: str | None
    created_at: datetime
    updated_at: datetime


class EventListItem(EventOut):
    """`GET /api/events` items: the race weekend's linked costs and pay (#79).

    Totals are computed in `app.services.events.list_events`, in two queries
    aggregated across every listed event rather than one per event; a missing side
    (no linked transactions or no linked income) is zero, not null.
    """

    costs_total: Decimal
    pay_total: Decimal
