import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal

from app.schemas.base import Schema
from app.schemas.months import Month


class HomeQuery(Schema):
    """Query params for `GET /api/home`."""

    month: Month


class BucketSpend(Schema):
    bucket: Literal["personal", "business"]
    spent: Decimal


class SpendingByBucket(Schema):
    buckets: list[BucketSpend]
    total: Decimal


class IncomeSummary(Schema):
    expected: Decimal
    received: Decimal


class BillDue(Schema):
    """A bill due within the next 7 days. `amount` is a positive magnitude; it's
    `None` for a card with no `planned_payment` set.
    """

    kind: Literal["subscription", "card"]
    id: uuid.UUID
    name: str
    due_date: dt.date
    amount: Decimal | None


class StaleBank(Schema):
    """A bank SimpleFIN hasn't refreshed lately: its newest balance date is older than
    `STALE_AFTER`. Usually the bank wants the user to sign in again in SimpleFIN Bridge."""

    institution: str
    last_refreshed: dt.date


class SetupProgress(Schema):
    """Which getting-started steps are done, from real data."""

    bank_connected: bool
    inbox_reviewed: bool
    categories_ready: bool
    budgets_set: bool
    notifications_on: bool


class HomeOut(Schema):
    month: str
    spending_by_bucket: SpendingByBucket
    owed_to_you: Decimal
    income: IncomeSummary
    bills_due: list[BillDue]
    last_updated: dt.datetime | None
    stale_banks: list[StaleBank]
    setup: SetupProgress
