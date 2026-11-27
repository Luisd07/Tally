import uuid
from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, model_validator

from app.schemas.base import Schema
from app.schemas.months import Month, shift_month


class DashboardQuery(Schema):
    """Query params for `GET /api/dashboard`. `months` sets how many months the
    trend covers, ending at and including `month`.
    """

    month: Month
    months: Annotated[int, Field(ge=1, le=24)] = 6

    @model_validator(mode="after")
    def _trend_start_in_range(self) -> Self:
        try:
            shift_month(self.month, -(self.months - 1))
        except ValueError as exc:
            raise ValueError("months would start before year 1") from exc
        return self


class CategorySpend(Schema):
    """One row of the by-category breakdown. `category_id`/`name` are both `None`
    for the single row that rolls up uncategorized spending.
    """

    category_id: uuid.UUID | None
    name: str | None
    budget: Decimal | None
    spent: Decimal
    remaining: Decimal | None


class TrendMonth(Schema):
    month: str
    spent: Decimal


class DashboardOut(Schema):
    month: str
    by_category: list[CategorySpend]
    total_spent: Decimal
    total_budget: Decimal
    trend: list[TrendMonth]
