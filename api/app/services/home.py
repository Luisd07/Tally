"""Everything the Home screen shows, in one call (spec: "Home is one call, not
five"). See `.claude/rules/api.md` for user scoping.
"""

import calendar
import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.config import APP_TIMEZONE
from app.models import (
    Account,
    BalanceSnapshot,
    Category,
    Device,
    IncomeEntry,
    SimplefinConnection,
    Subscription,
    SyncRun,
    Transaction,
)
from app.models.enums import AccountKind, CategoryKind, RunStatus
from app.schemas.home import (
    BillDue,
    BucketSpend,
    HomeOut,
    HomeQuery,
    IncomeSummary,
    SetupProgress,
    SpendingByBucket,
    StaleBank,
)
from app.schemas.months import month_range
from app.services.dashboard import SPENDING_BUCKETS, spending
from app.services.inbox import inbox_base
from app.services.reimbursements import owed_to_you

_ZERO = Decimal("0.00")
_BILLS_WINDOW_DAYS = 7
# A bank whose newest SimpleFIN balance date is older than this has stopped refreshing.
STALE_AFTER = dt.timedelta(days=2)
# The Inbox counts as reviewed once this few rows are left in it.
_INBOX_DONE_AT = 5
_CATEGORIES_READY_AT = 3


def _spending_by_bucket(
    db: Session, user_id: uuid.UUID, start: dt.date, end: dt.date
) -> SpendingByBucket:
    stmt = spending(
        select(Transaction.bucket, func.sum(-Transaction.amount)).where(
            Transaction.user_id == user_id
        ),
        start,
        end,
    ).group_by(Transaction.bucket)
    spent = {bucket: total for bucket, total in db.execute(stmt).all()}
    buckets = [BucketSpend(bucket=b.value, spent=spent.get(b, _ZERO)) for b in SPENDING_BUCKETS]
    return SpendingByBucket(buckets=buckets, total=sum((b.spent for b in buckets), _ZERO))


def _income(db: Session, user_id: uuid.UUID, start: dt.date, end: dt.date) -> IncomeSummary:
    stmt = select(
        func.coalesce(func.sum(IncomeEntry.expected), _ZERO),
        func.coalesce(func.sum(IncomeEntry.received), _ZERO),
    ).where(IncomeEntry.user_id == user_id, IncomeEntry.date >= start, IncomeEntry.date < end)
    expected, received = db.execute(stmt).one()
    return IncomeSummary(expected=expected or _ZERO, received=received or _ZERO)


def _clamped(year: int, month: int, day: int) -> dt.date:
    return dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _next_due_date(due_day: int, today: dt.date) -> dt.date:
    """The first date on or after `today` that falls on `due_day`, clamping a day
    past the month's end (31 in a 30-day month) to its last day.
    """
    candidate = _clamped(today.year, today.month, due_day)
    if candidate >= today:
        return candidate
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _clamped(year, month, due_day)


def _subscription_bills(
    db: Session, user_id: uuid.UUID, today: dt.date, last: dt.date
) -> list[BillDue]:
    subscriptions = db.scalars(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.active.is_(True),
            Subscription.next_due >= today,
            Subscription.next_due <= last,
        )
    )
    bills: list[BillDue] = []
    for sub in subscriptions:
        assert sub.next_due is not None
        bills.append(
            BillDue(
                kind="subscription",
                id=sub.id,
                name=sub.name,
                due_date=sub.next_due,
                amount=-sub.amount,
            )
        )
    return bills


def _card_bills(db: Session, user_id: uuid.UUID, today: dt.date, last: dt.date) -> list[BillDue]:
    cards = db.scalars(
        select(Account).where(
            Account.user_id == user_id,
            Account.active.is_(True),
            Account.kind.in_((AccountKind.credit_card, AccountKind.loan)),
            Account.due_day.is_not(None),
        )
    )
    bills: list[BillDue] = []
    for card in cards:
        assert card.due_day is not None
        due = _next_due_date(card.due_day, today)
        if due <= last:
            bills.append(
                BillDue(
                    kind="card",
                    id=card.id,
                    name=card.name,
                    due_date=due,
                    amount=card.planned_payment,
                )
            )
    return bills


def _bills_due(db: Session, user_id: uuid.UUID, today: dt.date) -> list[BillDue]:
    last = today + dt.timedelta(days=_BILLS_WINDOW_DAYS)
    bills = _subscription_bills(db, user_id, today, last) + _card_bills(db, user_id, today, last)
    bills.sort(key=lambda bill: (bill.due_date, bill.name))
    return bills


def _last_updated(db: Session, user_id: uuid.UUID) -> dt.datetime | None:
    return db.scalar(
        select(func.max(SyncRun.finished_at)).where(
            SyncRun.user_id == user_id, SyncRun.status == RunStatus.ok
        )
    )


def _stale_banks(db: Session, user_id: uuid.UUID, today: dt.date) -> list[StaleBank]:
    """Synced banks whose newest balance date, across their accounts, is before
    `today - STALE_AFTER`. A sync still finishes "ok" when SimpleFIN serves old data, so
    this is the only sign of a bank connection that needs the user."""
    newest = func.max(BalanceSnapshot.date)
    rows = db.execute(
        select(Account.institution, newest)
        .join(BalanceSnapshot, BalanceSnapshot.account_id == Account.id)
        .where(
            Account.user_id == user_id,
            Account.active.is_(True),
            Account.simplefin_account_id.is_not(None),
            Account.institution.is_not(None),
        )
        .group_by(Account.institution)
        .having(newest < today - STALE_AFTER)
        .order_by(Account.institution)
    )
    return [
        StaleBank(institution=name, last_refreshed=date) for name, date in rows if name is not None
    ]


def _setup(db: Session, user_id: uuid.UUID, now: dt.datetime) -> SetupProgress:
    """Getting-started steps, each from the user's own rows."""
    inbox_total = db.scalar(
        select(func.count()).select_from(
            inbox_base(user_id, now).limit(_INBOX_DONE_AT + 1).subquery()
        )
    )
    has_reviewed = db.scalar(
        select(exists().where(Transaction.user_id == user_id, Transaction.reviewed.is_(True)))
    )
    live = (Category.user_id == user_id, Category.archived.is_(False))
    expense_categories = db.scalar(
        select(func.count()).where(*live, Category.kind == CategoryKind.expense)
    )
    return SetupProgress(
        bank_connected=bool(
            db.scalar(select(exists().where(SimplefinConnection.user_id == user_id)))
        ),
        inbox_reviewed=bool(has_reviewed) and (inbox_total or 0) <= _INBOX_DONE_AT,
        categories_ready=(expense_categories or 0) >= _CATEGORIES_READY_AT,
        budgets_set=bool(
            db.scalar(select(exists().where(*live, Category.monthly_budget.is_not(None))))
        ),
        notifications_on=bool(db.scalar(select(exists().where(Device.user_id == user_id)))),
    )


def get_home(db: Session, user_id: uuid.UUID, query: HomeQuery, now: dt.datetime) -> HomeOut:
    start, end = month_range(query.month)
    return HomeOut(
        month=query.month,
        spending_by_bucket=_spending_by_bucket(db, user_id, start, end),
        owed_to_you=owed_to_you(db, user_id),
        income=_income(db, user_id, start, end),
        bills_due=_bills_due(db, user_id, now.astimezone(APP_TIMEZONE).date()),
        last_updated=_last_updated(db, user_id),
        stale_banks=_stale_banks(db, user_id, now.astimezone(APP_TIMEZONE).date()),
        setup=_setup(db, user_id, now),
    )
