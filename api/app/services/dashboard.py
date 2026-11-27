"""Dashboard totals: this month's spending by category against budget, plus the
trend over the trailing months. See `.claude/rules/api.md` for user scoping.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, exists, func, select
from sqlalchemy.orm import Session, aliased

from app.models import Category, Transaction
from app.models.enums import Bucket, CategoryKind
from app.schemas.dashboard import CategorySpend, DashboardOut, DashboardQuery, TrendMonth
from app.schemas.months import month_range, shift_month
from app.services.categories import list_categories
from app.services.splits import has_split_children

# spec "Classification rules": only `personal` and `business` count in spending.
# `reimbursable` is money owed back, not spend; `transfer` moves money between the
# user's own accounts; `income` isn't spend at all.
SPENDING_BUCKETS = (Bucket.personal, Bucket.business)

_ZERO = Decimal("0.00")


def spending(stmt: Select[Any, Any], start: date, end: date) -> Select[Any, Any]:
    """Narrow `stmt` (already scoped to the user) to settled spending rows in
    `[start, end)`.

    Spend is reported as a positive magnitude (`sum(-amount)`, since SimpleFIN's
    amounts are negative for money leaving the account), so a refund posted as a
    positive amount in the same category/month nets against it automatically.
    `has_split_children` drops pre-split parents (see `app.services.splits`).
    Rows filed under an `income`-kind category (e.g. Paycheck) are not spending
    and are excluded; uncategorized rows still count.
    """
    income_category = aliased(Category)
    return stmt.where(
        ~exists().where(
            income_category.id == Transaction.category_id,
            income_category.kind == CategoryKind.income,
        ),
        Transaction.bucket.in_(SPENDING_BUCKETS),
        Transaction.pending.is_(False),
        Transaction.posted_date >= start,
        Transaction.posted_date < end,
        ~has_split_children(),
    )


def _spend_by_category(
    db: Session, user_id: uuid.UUID, start: date, end: date
) -> dict[uuid.UUID | None, Decimal]:
    stmt = spending(
        select(Transaction.category_id, func.sum(-Transaction.amount)).where(
            Transaction.user_id == user_id
        ),
        start,
        end,
    ).group_by(Transaction.category_id)
    return {category_id: spent for category_id, spent in db.execute(stmt).all()}


def _category_rows(
    categories: list[Category], spend_by_category: dict[uuid.UUID | None, Decimal]
) -> list[CategorySpend]:
    """Row-assembly: a category needs a budget or spend to appear.

    Only `expense`-kind categories carry a budget here: an `income`-kind category
    with `monthly_budget` set isn't a spending target, so a budget-only row for one
    is hidden, and its budget never reaches `total_budget`. An archived category likewise
    carries no budget but still reports its spend. One with actual
    personal/business spend of its own still appears, budget-less. Spend left over
    once every category's been matched rolls into a single uncategorized row, last.
    """
    rows: list[CategorySpend] = []
    for category in categories:
        spent = spend_by_category.pop(category.id, None)
        budget = (
            category.monthly_budget
            if category.kind == CategoryKind.expense and not category.archived
            else None
        )
        if spent is None and budget is None:
            continue
        spent = spent if spent is not None else _ZERO
        remaining = None if budget is None else budget - spent
        rows.append(
            CategorySpend(
                category_id=category.id,
                name=category.name,
                budget=budget,
                spent=spent,
                remaining=remaining,
            )
        )

    uncategorized_spent = spend_by_category.pop(None, None)
    if uncategorized_spent is not None:
        rows.append(
            CategorySpend(
                category_id=None, name=None, budget=None, spent=uncategorized_spent, remaining=None
            )
        )
    return rows


def _trend(db: Session, user_id: uuid.UUID, month: str, months: int) -> list[TrendMonth]:
    """The trailing `months` months ending at (and including) `month`, zero-filled.

    `DashboardQuery` already rejects a `month`/`months` pair whose window would
    start before year 1, so `shift_month` here can't raise.
    """
    sequence = [shift_month(month, offset) for offset in range(-(months - 1), 1)]
    start, _ = month_range(sequence[0])
    _, end = month_range(month)

    bucket = func.date_trunc("month", Transaction.posted_date)
    stmt = spending(
        select(bucket, func.sum(-Transaction.amount)).where(Transaction.user_id == user_id),
        start,
        end,
    ).group_by(bucket)
    spent_by_month = {row[0].date(): row[1] for row in db.execute(stmt).all()}

    return [
        TrendMonth(month=m, spent=spent_by_month.get(month_range(m)[0], _ZERO)) for m in sequence
    ]


def get_dashboard(db: Session, user_id: uuid.UUID, query: DashboardQuery) -> DashboardOut:
    start, end = month_range(query.month)

    spend_by_category = _spend_by_category(db, user_id, start, end)
    categories = list_categories(db, user_id)
    rows = _category_rows(categories, spend_by_category)

    total_spent = sum((row.spent for row in rows), _ZERO)
    total_budget = sum((row.budget for row in rows if row.budget is not None), _ZERO)

    return DashboardOut(
        month=query.month,
        by_category=rows,
        total_spent=total_spent,
        total_budget=total_budget,
        trend=_trend(db, user_id, query.month, query.months),
    )
