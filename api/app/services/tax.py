"""The Tax screen: the missing-receipt list (#71) and the year's summary by Schedule
C line (#70).

Business-bucket transactions need a receipt for the tax view's export (spec
"Receipts": *"A tax view that totals business spending by Schedule C line and exports
it with receipts"*). The spec doesn't say the same about `reimbursable` rows — those
belong to the separate expense-report flow (#41) — so they stay opt-in through
`?bucket=reimbursable` rather than joining the default `business` listing.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import NamedTuple

from sqlalchemy import ColumnElement, Select, exists, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Category, IncomeEntry, Receipt, Transaction
from app.models.enums import Bucket, IncomeType
from app.schemas.tax import (
    ExpenseLine,
    MileageSummary,
    MissingReceiptListQuery,
    MissingReceiptPage,
    TaxSummaryOut,
    TaxSummaryQuery,
    UncategorizedBusiness,
)
from app.scoping import owned
from app.services.mileage import MileageTotals, find_odometer, priced_trips, summarize_mileage
from app.services.paging import filter_date_cursor, paginate
from app.services.splits import has_split_children
from app.services.transactions import to_out_many

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")
_NON_REIMBURSEMENT_TYPES = tuple(t for t in IncomeType if t != IncomeType.reimbursement)
_FULLY_DEDUCTIBLE = 100

#: Schedule C line 9. Categories tagged with it hold actual car costs (gas, repairs,
#: insurance), which the standard mileage rate replaces (#67), so they're deducted
#: nowhere; the line itself is built from the mileage log plus `CAR_PARKING_TOLLS`.
CAR_TRUCK = "car_truck"
#: Business parking and tolls, deductible on top of the standard mileage rate.
CAR_PARKING_TOLLS = "car_parking_tolls"


class CategorySpend(NamedTuple):
    tax_line: str
    deductible_pct: int
    spent: Decimal


@dataclass(frozen=True)
class _CarSpend:
    """Category spend on line 9's tags: `parking` (`CAR_PARKING_TOLLS`) is deductible
    on top of the standard mileage rate, `actual` (`CAR_TRUCK`) is not.
    """

    parking: list[CategorySpend]
    actual: list[CategorySpend]

    @property
    def parking_spent(self) -> Decimal:
        return sum((row.spent for row in self.parking), _ZERO)

    @property
    def actual_spent(self) -> Decimal:
        return sum((row.spent for row in self.actual), _ZERO)

    @property
    def tagged(self) -> bool:
        return bool(self.parking or self.actual)


def is_actual_car_cost(tax_line: str | None) -> bool:
    """Whether spend on `tax_line` is an actual car cost, which the standard mileage
    rate replaces, so it deducts nothing: the summary leaves it off every line and
    the export shows it at `deductible_pct` 0 (`effective_deductible_pct`).
    """
    return tax_line == CAR_TRUCK


def effective_deductible_pct(tax_line: str | None, pct: int | None) -> int | None:
    """The `deductible_pct` a row on `tax_line` actually gets: 0 for actual car costs,
    otherwise its category's own `pct`.
    """
    return 0 if is_actual_car_cost(tax_line) else pct


def _base_query(user_id: uuid.UUID, query: MissingReceiptListQuery) -> Select[Transaction]:
    year_start = date(query.year, 1, 1)
    year_end = date(query.year + 1, 1, 1)
    bucket = query.bucket or Bucket.business

    # #80 keeps a split's receipt on the parent, so a receipt there covers every child.
    has_receipt = exists().where(
        or_(
            Receipt.transaction_id == Transaction.id,
            Receipt.transaction_id == Transaction.parent_id,
        )
    )

    return owned(Transaction, user_id).where(
        Transaction.bucket == bucket,
        Transaction.posted_date >= year_start,
        Transaction.posted_date < year_end,
        Transaction.amount < 0,
        Transaction.pending.is_(False),
        ~has_receipt,
        ~has_split_children(),
    )


def list_missing_receipts(
    db: Session, user_id: uuid.UUID, query: MissingReceiptListQuery
) -> MissingReceiptPage:
    stmt = _base_query(user_id, query)
    count_stmt = select(func.count()).select_from(stmt.with_only_columns(Transaction.id).subquery())
    count = db.scalar(count_stmt)

    paged = filter_date_cursor(stmt, Transaction.posted_date, Transaction.id, query.cursor)
    paged = paged.order_by(Transaction.posted_date.desc(), Transaction.id.desc())
    rows, next_cursor = paginate(db, paged, query.limit, lambda row: (row.posted_date, row.id))

    return MissingReceiptPage(
        items=to_out_many(db, rows),
        next_cursor=next_cursor,
        count=count or 0,
    )


def expense_buckets(settings: Settings) -> tuple[Bucket, ...]:
    """The buckets that count as business spending for the tax view (spec
    "Classification rules"): `business` always, plus `reimbursable` when
    `reimbursements_on_1099` is on ("the tax view counts them as income and counts
    the expenses as deductions"). Off, a reimbursable row stays out of the tax view
    entirely. Shared with #72's export, which should filter its transactions through
    this same set so the zip matches the summary.
    """
    if settings.reimbursements_on_1099:
        return (Bucket.business, Bucket.reimbursable)
    return (Bucket.business,)


def _effective_pct(spent: Decimal, deductible: Decimal) -> int | None:
    """`deductible` as a percent of `spent`, for `ExpenseLine.deductible_pct`.

    Exact when every category on the line shares one `deductible_pct` (the common
    case); a rounded blend of them when they don't. `0` when a category's refunds
    net its spend to exactly zero for the year, since there's no rate left to show.
    `None` when categories on the line net to opposite signs (a refund-only
    category next to a spending one), where the blend falls outside 0..100 and
    means nothing; `deductible` still carries the line's amount.
    """
    if spent == 0:
        return 0
    pct = int((deductible / spent * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    return pct if 0 <= pct <= 100 else None


def tax_rows(
    user_id: uuid.UUID, buckets: tuple[Bucket, ...], start: date, end: date
) -> tuple[ColumnElement[bool], ...]:
    """The rows the summary counts as business spend: the user's settled,
    non-split-parent transactions in `buckets` within `[start, end)`. Shared by the
    categorized and uncategorized queries so the two can't drift apart.
    """
    return (
        Transaction.user_id == user_id,
        Transaction.bucket.in_(buckets),
        Transaction.pending.is_(False),
        Transaction.posted_date >= start,
        Transaction.posted_date < end,
        ~has_split_children(),
    )


def _category_spend(
    db: Session, user_id: uuid.UUID, buckets: tuple[Bucket, ...], start: date, end: date
) -> list[CategorySpend]:
    """A `CategorySpend` per category that has a `tax_line`, for
    settled, non-split rows in `[start, end)`. Grouped by `Category.id` (not just
    `tax_line`) so two categories sharing a line keep their own `deductible_pct`;
    `get_tax_summary` rolls them up by line afterwards.
    """
    stmt = (
        select(Category.tax_line, Category.deductible_pct, func.sum(-Transaction.amount))
        .select_from(Transaction)
        .join(Category, Category.id == Transaction.category_id)
        .where(*tax_rows(user_id, buckets, start, end), Category.tax_line.is_not(None))
        .group_by(Category.id, Category.tax_line, Category.deductible_pct)
    )
    rows = db.execute(stmt).all()
    # `Category.tax_line.is_not(None)` above guarantees this, but the column itself
    # is nullable, so the row type still carries `str | None`.
    return [
        CategorySpend(tax_line, pct, spent) for tax_line, pct, spent in rows if tax_line is not None
    ]


def _uncategorized_business(
    db: Session, user_id: uuid.UUID, buckets: tuple[Bucket, ...], start: date, end: date
) -> UncategorizedBusiness:
    """Spend with no category, or a category with no `tax_line`, for the same rows
    `_category_spend` covers.
    """
    stmt = (
        select(func.sum(-Transaction.amount), func.count())
        .select_from(Transaction)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            *tax_rows(user_id, buckets, start, end),
            or_(Transaction.category_id.is_(None), Category.tax_line.is_(None)),
        )
    )
    spent, count = db.execute(stmt).one()
    return UncategorizedBusiness(spent=spent or _ZERO, count=count or 0)


def _gross_receipts(
    db: Session, user_id: uuid.UUID, start: date, end: date, include_reimbursements: bool
) -> Decimal:
    """Sum of `income_entries.received` in `[start, end)`, by `date` -- the pay that
    actually landed, not what was merely expected (spec "Data model"). A
    `reimbursement`-type entry counts only when `include_reimbursements` is set,
    matching the expenses side of the same setting.
    """
    types = list(IncomeType) if include_reimbursements else _NON_REIMBURSEMENT_TYPES
    stmt = select(func.sum(IncomeEntry.received)).where(
        IncomeEntry.user_id == user_id,
        IncomeEntry.date >= start,
        IncomeEntry.date < end,
        IncomeEntry.type.in_(types),
    )
    return db.scalar(stmt) or _ZERO


def _reimbursement_note(settings: Settings) -> str:
    state = "on" if settings.reimbursements_on_1099 else "off"
    return (
        f"Reimbursements are {state} the 1099 total (`reimbursements_on_1099`); "
        "that setting is still undecided -- see #66."
    )


def _excluded_car_costs_note(car: _CarSpend) -> str | None:
    if not car.actual_spent:
        return None
    return (
        f"${car.actual_spent} of actual car costs (categories tagged "
        "car_truck) is not deductible under the standard mileage rate and is left "
        "out of every line."
    )


def _unpriced_note(totals: MileageTotals) -> str | None:
    if not totals.unpriced_miles:
        return None
    dates = totals.unpriced_dates
    count = f"{len(dates)} date" if len(dates) == 1 else f"{len(dates)} dates"
    first, last = dates[0].isoformat(), dates[-1].isoformat()
    span = first if first == last else f"{first} to {last}"
    return (
        f"{totals.unpriced_miles} business miles fall on dates with no mileage rate "
        f"in force ({count}, {span}) and add $0; add a rate that covers them."
    )


def _tagging_note(totals: MileageTotals, car: _CarSpend) -> str | None:
    if not totals.business_miles or car.tagged:
        return None
    return (
        "You logged trips this year, but no spend is on a category tagged "
        "car_parking_tolls or car_truck. Tag your parking and tolls category "
        "car_parking_tolls and your gas, repair and insurance categories car_truck "
        "so line 9 is right."
    )


def _notes(settings: Settings, totals: MileageTotals, car: _CarSpend) -> list[str]:
    fixed = [
        _reimbursement_note(settings),
        "Car and truck expenses (line 9) use the standard mileage rate (#67): business "
        "miles at the rate in force on each trip's date, plus business parking and tolls.",
        "Subscriptions are personal, not business expenses (#68).",
    ]
    conditional = [
        _excluded_car_costs_note(car),
        _unpriced_note(totals),
        _tagging_note(totals, car),
    ]
    return fixed + [note for note in conditional if note is not None]


def _split_car_spend(rows: list[CategorySpend]) -> tuple[list[CategorySpend], _CarSpend]:
    """`rows` off line 9, and the spend on line 9's own tags."""
    car = _CarSpend(
        parking=[row for row in rows if row.tax_line == CAR_PARKING_TOLLS],
        actual=[row for row in rows if is_actual_car_cost(row.tax_line)],
    )
    others = [
        row
        for row in rows
        if row.tax_line != CAR_PARKING_TOLLS and not is_actual_car_cost(row.tax_line)
    ]
    return others, car


def _line_9_rows(car: _CarSpend, mileage_amount: Decimal) -> list[CategorySpend]:
    """Line 9 under the standard mileage rate, as `CAR_TRUCK` rows: the mileage amount,
    fully deductible, plus parking and tolls at their categories' own
    `deductible_pct`. Actual car costs are left out. Empty when both are zero.
    """
    if not (mileage_amount or car.parking_spent):
        return []
    return [
        CategorySpend(CAR_TRUCK, _FULLY_DEDUCTIBLE, mileage_amount),
        *(row._replace(tax_line=CAR_TRUCK) for row in car.parking),
    ]


def _mileage_out(totals: MileageTotals, car: _CarSpend) -> MileageSummary:
    return MileageSummary(
        business_miles=totals.business_miles,
        mileage_amount=totals.mileage_amount,
        parking_tolls=car.parking_spent,
        unpriced_miles=totals.unpriced_miles,
        excluded_actual_car_costs=car.actual_spent,
        rates=totals.rates,
        odometer_start=totals.odometer_start,
        odometer_end=totals.odometer_end,
        total_miles=totals.total_miles,
    )


def _roll_up_by_line(rows: list[CategorySpend]) -> list[ExpenseLine]:
    """Sum per-category rows onto their Schedule C line. `deductible` is computed per
    category and summed before being quantized once (`ROUND_HALF_EVEN`, to the cent)
    -- not quantized per category first -- so two categories on one line can't each
    round away from the other's half-cent.
    """
    spent_by_line: dict[str, Decimal] = {}
    deductible_by_line: dict[str, Decimal] = {}
    for tax_line, pct, spent in rows:
        spent_by_line[tax_line] = spent_by_line.get(tax_line, _ZERO) + spent
        deductible_by_line[tax_line] = deductible_by_line.get(tax_line, _ZERO) + spent * pct / 100
    return [
        ExpenseLine(
            tax_line=tax_line,
            spent=spent_by_line[tax_line],
            deductible_pct=_effective_pct(spent_by_line[tax_line], deductible_by_line[tax_line]),
            deductible=deductible_by_line[tax_line].quantize(_CENT, rounding=ROUND_HALF_EVEN),
        )
        for tax_line in sorted(spent_by_line)
    ]


def get_tax_summary(
    db: Session, user_id: uuid.UUID, query: TaxSummaryQuery, settings: Settings
) -> TaxSummaryOut:
    """The year's tax summary by Schedule C line (#70, spec "API endpoints").

    `spent` is a positive magnitude (`sum(-amount)`), so a refund posted as a
    positive business amount nets against the category's spend the same way it does
    on the dashboard (`app.services.dashboard`).
    """
    start = date(query.year, 1, 1)
    end = date(query.year + 1, 1, 1)
    buckets = expense_buckets(settings)

    others, car = _split_car_spend(_category_spend(db, user_id, buckets, start, end))
    totals = summarize_mileage(
        priced_trips(db, user_id, query.year), find_odometer(db, user_id, query.year)
    )
    expenses_by_line = _roll_up_by_line(others + _line_9_rows(car, totals.mileage_amount))
    total_deductible = sum((line.deductible for line in expenses_by_line), _ZERO)

    return TaxSummaryOut(
        year=query.year,
        gross_receipts=_gross_receipts(db, user_id, start, end, settings.reimbursements_on_1099),
        expenses_by_line=expenses_by_line,
        uncategorized_business=_uncategorized_business(db, user_id, buckets, start, end),
        total_deductible=total_deductible,
        reimbursements_included=settings.reimbursements_on_1099,
        mileage=_mileage_out(totals, car),
        notes=_notes(settings, totals, car),
    )
