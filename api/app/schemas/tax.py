"""Query and response shapes for the Tax screen: the missing-receipt list (#71) and
the year's tax summary by Schedule C line (#70).
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import Field, field_validator

from app.models.enums import Bucket
from app.schemas.base import Schema
from app.schemas.paging import decode_date_cursor
from app.schemas.transactions import TransactionOut
from app.schemas.types import Money, Percent

#: The only buckets a receipt is ever required for: `business` for the tax view itself
#: (spec "Receipts, A tax view that totals business spending by Schedule C line and
#: exports it with receipts"), `reimbursable` on request for the expense-report flow.
RECEIPT_REQUIRED_BUCKETS = frozenset({Bucket.business, Bucket.reimbursable})


class MissingReceiptListQuery(Schema):
    """Query params for `GET /api/tax/missing-receipts`.

    `cursor` is opaque to the client: base64 of `<posted_date>|<id>`, the last row of
    the previous page, ordered the same way as the listing (`posted_date` DESC, `id`
    DESC).
    """

    year: Annotated[int, Field(ge=1900, le=2100)]
    bucket: Bucket | None = None
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = None

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: Bucket | None) -> Bucket | None:
        if value is not None and value not in RECEIPT_REQUIRED_BUCKETS:
            raise ValueError("bucket must be business or reimbursable")
        return value

    @field_validator("cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        if value is not None:
            decode_date_cursor(value)
        return value


class MissingReceiptPage(Schema):
    items: list[TransactionOut]
    next_cursor: str | None
    count: int


class TaxSummaryQuery(Schema):
    """Query params for `GET /api/tax/summary`."""

    year: Annotated[int, Field(ge=1900, le=2100)]


class ExpenseLine(Schema):
    """One Schedule C line's business spend and its deductible share.

    `deductible` is computed per category (spec "Tax lines": different categories on
    the same line can carry different `deductible_pct`, e.g. 50% meals sharing a line
    with 100% travel) and summed onto the line before `app.services.tax` quantizes it
    once. `deductible_pct` here is the line's blended rate, `deductible / spent` as a
    percent -- the category rate itself when every category on the line shares one,
    a derived display value when they don't. `None` when the line's categories net to
    opposite signs, where no blended rate in 0..100 exists.
    """

    tax_line: str
    spent: Money
    deductible_pct: Percent | None
    deductible: Money


class UncategorizedBusiness(Schema):
    """Business spend that can't be put on a line: no category, or a category with
    no `tax_line` set.
    """

    spent: Money
    count: int


class MileageRateUsed(Schema):
    """A rate the year's trips were priced at, and the business miles priced at it."""

    effective_from: date
    rate: Decimal
    miles: Decimal


class MileageSummary(Schema):
    """How line 9 (`car_truck`) was built under the standard mileage rate (#167).

    `mileage_amount` (business miles at the rate in force on each trip's date) plus
    `parking_tolls` (spend on `car_parking_tolls` categories) is the line's `spent`.
    `unpriced_miles` fell on dates with no rate in force and add nothing.
    `excluded_actual_car_costs` is spend on `car_truck` categories (gas, repairs,
    insurance), which the standard mileage rate replaces, so no line counts it.
    The odometer fields are `None` until the year's reading is set; `total_miles`
    also needs the end reading.
    """

    business_miles: Decimal
    mileage_amount: Money
    parking_tolls: Money
    unpriced_miles: Decimal
    excluded_actual_car_costs: Money
    rates: list[MileageRateUsed]
    odometer_start: Decimal | None
    odometer_end: Decimal | None
    total_miles: Decimal | None


class TaxSummaryOut(Schema):
    year: int
    gross_receipts: Money
    expenses_by_line: list[ExpenseLine]
    uncategorized_business: UncategorizedBusiness
    total_deductible: Money
    reimbursements_included: bool
    mileage: MileageSummary
    notes: list[str]
