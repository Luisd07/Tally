import datetime as dt
import uuid
from decimal import Decimal
from typing import Self

from pydantic import ConfigDict, model_validator

from app.models.enums import IncomeType
from app.schemas.base import PatchSchema, Schema
from app.schemas.months import Month
from app.schemas.types import MoneyNonNegative, Text500


class IncomeCreate(Schema):
    """Fields the phone may set on create."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    date: dt.date
    type: IncomeType
    description: Text500 | None = None
    expected: MoneyNonNegative | None = None
    received: MoneyNonNegative | None = None
    transaction_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _expected_or_received(self) -> Self:
        if self.expected is None and self.received is None:
            raise ValueError("expected or received must be set")
        return self


class IncomeUpdate(PatchSchema):
    """Partial update; only fields present in the request body are applied.

    The expected/received CHECK against the merged result happens in
    `app.services.income.update_income`, since this schema alone can't see the
    row's other amount when only one is sent.
    """

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"date", "type"})

    date: dt.date | None = None
    type: IncomeType | None = None
    description: Text500 | None = None
    expected: MoneyNonNegative | None = None
    received: MoneyNonNegative | None = None
    transaction_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None


class IncomeOut(Schema):
    id: uuid.UUID
    date: dt.date
    type: IncomeType
    description: str | None
    expected: Decimal | None
    received: Decimal | None
    transaction_id: uuid.UUID | None
    event_id: uuid.UUID | None
    created_at: dt.datetime
    updated_at: dt.datetime


class IncomeTotals(Schema):
    """Sums over the filtered list; a side with no rows set is 0, not null."""

    expected: Decimal
    received: Decimal


class IncomePage(Schema):
    items: list[IncomeOut]
    totals: IncomeTotals


class IncomeListQuery(Schema):
    """Query params for `GET /api/income`."""

    month: Month | None = None
    type: IncomeType | None = None
    event_id: uuid.UUID | None = None
