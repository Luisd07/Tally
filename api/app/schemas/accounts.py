import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict

from app.models.enums import AccountKind
from app.schemas.base import PatchSchema, Schema
from app.schemas.months import Month
from app.schemas.types import Apr, DayOfMonth, Last4, Money, MoneyNonNegative, Name100, Text100


class AccountCreate(Schema):
    """Fields the phone may set on create. `simplefin_account_id` is sync-only (#48)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: Name100
    kind: AccountKind
    institution: Text100 | None = None
    last4: Last4 | None = None
    due_day: DayOfMonth | None = None
    planned_payment: Money | None = None
    apr: Apr | None = None
    promo_apr_end: date | None = None
    active: bool = True


class AccountUpdate(PatchSchema):
    """Partial update; only fields present in the request body are applied."""

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"name", "kind", "active"})

    name: Name100 | None = None
    kind: AccountKind | None = None
    institution: Text100 | None = None
    last4: Last4 | None = None
    due_day: DayOfMonth | None = None
    planned_payment: Money | None = None
    apr: Apr | None = None
    promo_apr_end: date | None = None
    active: bool | None = None


class AccountOut(Schema):
    id: uuid.UUID
    name: str
    kind: AccountKind
    institution: str | None
    simplefin_account_id: str | None
    last4: str | None
    due_day: int | None
    planned_payment: Decimal | None
    apr: Decimal | None
    promo_apr_end: date | None
    active: bool
    created_at: datetime
    updated_at: datetime


class InterestMonthOut(Schema):
    """One month of interest and fees charged to a card, as a positive cost: `amount` is
    `interest + fees`."""

    month: str
    amount: Decimal
    interest: Decimal
    fees: Decimal


class CardInterestOut(Schema):
    """What a card's interest and fees are costing: the last 12 months (oldest first,
    the current month last), the year to date, this month's estimate and an APR check.

    `estimate` is the latest balance (no older than 45 days) owed x APR / 12, `0.00` while a 0%
    promo is active, and `None` without an APR or a balance. `mismatch` is true when the last
    full month's actual interest (fees excluded) differs from the estimate for that month by
    more than 25% and $5, suggesting the stored APR is wrong.
    """

    months: list[InterestMonthOut]
    year_to_date: Decimal
    year_to_date_interest: Decimal
    year_to_date_fees: Decimal
    estimate: Decimal | None
    mismatch: bool


class InterestSummaryOut(Schema):
    """Response for `GET /api/accounts/interest`: interest and fees charged across the
    user's cards this year, with each card's breakdown.
    """

    year_to_date: Decimal
    cards: dict[uuid.UUID, CardInterestOut]


class CardOut(Schema):
    """One row of the Cards screen: an account plus its latest balance.

    `source_hint` is `"synced"` when the account has a `simplefin_account_id`,
    `"manual"` otherwise -- the Bridge couldn't connect it, so balances are entered
    by hand (see `BalanceUpsert`).
    """

    account: AccountOut
    balance: Decimal | None
    balance_date: date | None
    source_hint: Literal["synced", "manual"]
    interest: CardInterestOut | None = None


class LoanOut(Schema):
    """One row of the loans list: an active `loan` account, what it owes on its latest
    balance (`None` without one, or when it owes nothing) and that debt paid on its own at
    its `planned_payment` (or the minimum without one). `months_to_payoff`, `payoff_month`
    and `total_interest` are `None` when it never pays off."""

    account: AccountOut
    owed: Decimal | None
    balance_date: date | None
    months_to_payoff: int | None
    payoff_month: str | None
    total_interest: Decimal | None


class BalanceUpsert(Schema):
    """Body for `PUT /api/accounts/{id}/balances/{date}`.

    `balance` follows SimpleFIN's sign, same as a synced snapshot: card debt is
    negative, consistent with `transactions.amount` and `balance_snapshots.balance`.
    """

    model_config = ConfigDict(extra="forbid")

    balance: Money


class BalanceSnapshotOut(Schema):
    id: uuid.UUID
    account_id: uuid.UUID
    date: date
    balance: Decimal
    created_at: datetime
    updated_at: datetime


class CardLogOut(Schema):
    """Response for `GET /api/accounts/{id}/card-log`: the monthly reconciliation
    the Cards tab's sheet log replaces (spec "Credit cards").

    Every amount keeps SimpleFIN's sign: a charge or a fee leaves the account
    (negative), a payment or a refund arrives (positive). `starting_balance`,
    `ending_balance`, `computed_ending` and `difference` are `None` when the
    account is missing a snapshot on or before the relevant month-end -- there's
    nothing to reconcile against yet. `starting_balance_date`/`ending_balance_date`
    are the dates those snapshots actually carry, so a stale one (reused because
    nothing newer qualified) is visible even when the balance itself is not
    `None`; `difference` is `None` in that case too (#63 review UB-5/MV-2).
    """

    month: str
    starting_balance: Decimal | None
    starting_balance_date: date | None
    new_charges: Decimal
    payments: Decimal
    interest_and_fees: Decimal
    other: Decimal
    ending_balance: Decimal | None
    ending_balance_date: date | None
    computed_ending: Decimal | None
    difference: Decimal | None


class CardLogQuery(Schema):
    """Query params for `GET /api/accounts/{id}/card-log`."""

    month: Month


class PayoffQuery(Schema):
    """Query params for `GET /api/accounts/payoff`: the total monthly budget for the
    strategies, the sum of the planned payments when omitted."""

    budget: MoneyNonNegative | None = None


class PromoPaceOut(Schema):
    """Pacing for a card on an active 0% promo: `monthly_payment` clears what it owes
    over the `months_left` payments before `ends`, and `short` says the card's monthly
    payment (planned, or the minimum without one) is less than that."""

    ends: date
    months_left: int
    monthly_payment: Decimal
    short: bool


class PayoffCardOut(Schema):
    """One card paid on its own at its monthly payment: `planned_payment`, or without
    one the minimum (the larger of $25 and 1% of the balance plus the month's interest;
    `minimum_payment` is the first month's). `months_to_payoff`, `payoff_month` and
    `total_interest` are `None` when it never pays off (the payment doesn't cover the
    interest, or it would take more than 600 months)."""

    account_id: uuid.UUID
    name: str
    owed: Decimal
    apr: Decimal | None
    promo_apr_end: date | None
    planned_payment: Decimal | None
    minimum_payment: Decimal
    months_to_payoff: int | None
    payoff_month: str | None
    total_interest: Decimal | None
    promo: PromoPaceOut | None


class PayoffStepOut(Schema):
    """A card in a strategy's order to pay, with the month it's paid off."""

    account_id: uuid.UUID
    name: str
    payoff_month: str | None


class PayoffStrategyOut(Schema):
    """Every card at its monthly payment, with the rest of the budget on the card the
    strategy ranks first that month among those still owing. `order` lists the cards in the
    order they first rank first, then the rest as ranked at the start. `interest_saved` is
    against paying only the monthly payments, card by card; `None` fields mean it never pays
    off."""

    strategy: Literal["avalanche", "snowball"]
    order: list[PayoffStepOut]
    months_to_debt_free: int | None
    debt_free_month: str | None
    total_interest: Decimal | None
    interest_saved: Decimal | None


class PayoffPlanOut(Schema):
    """Response for `GET /api/accounts/payoff`: each card owing a balance paid on its own,
    when that makes the user debt-free and what it costs in interest, and the avalanche
    (highest APR in effect first, re-ranked each month as promos end) and snowball
    (smallest balance first) strategies for `budget`. `planned_budget` is the sum of the
    monthly payments; `budget` is the one used, never less than that: `requested_budget`
    is the one asked for, and `clamped` says it was raised to cover the monthly payments."""

    budget: Decimal
    requested_budget: Decimal | None
    clamped: bool
    planned_budget: Decimal
    months_to_debt_free: int | None
    debt_free_month: str | None
    total_interest: Decimal | None
    cards: list[PayoffCardOut]
    avalanche: PayoffStrategyOut
    snowball: PayoffStrategyOut
