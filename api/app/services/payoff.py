"""Credit card payoff plans (#243): month-by-month amortization of what each card owes,
the avalanche and snowball strategies for a monthly budget, and 0% promo pacing.

Pure functions over `Debt`; `app.services.accounts.get_payoff_plan` loads the cards.

A card's first payment is this month while its payment date is still ahead of `today`,
else next month, and each month's payment lands on the card's due day (the last day of
the month without one). A month's interest is the balance going into it
x APR / 100 / 12, rounded to the cent, and nothing while a promo is active on the payment
date or without an APR. The monthly payment is `planned_payment`, or without one the
minimum: the larger of $25 and 1% of the balance plus that month's interest. A plan that
hasn't paid off within `MAX_MONTHS` never does.
"""

import calendar
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Literal

from app.schemas.accounts import (
    PayoffCardOut,
    PayoffPlanOut,
    PayoffStepOut,
    PayoffStrategyOut,
    PromoPaceOut,
)

MAX_MONTHS = 600
MIN_PAYMENT = Decimal("25.00")
MIN_PAYMENT_RATE = Decimal("0.01")

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Debt:
    """A card and what it owes, as a positive amount."""

    id: uuid.UUID
    name: str
    owed: Decimal
    apr: Decimal | None = None
    promo_apr_end: date | None = None
    planned_payment: Decimal | None = None
    due_day: int | None = None


def _cents(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, ROUND_HALF_UP)


def _month(today: date, k: int) -> date:
    """The first day of the month `k` months after `today`'s."""
    index = today.year * 12 + today.month - 1 + k
    return date(index // 12, index % 12 + 1, 1)


def _label(month: date) -> str:
    return f"{month.year:04d}-{month.month:02d}"


def payment_date(debt: Debt, month: date) -> date:
    """When `month`'s payment lands: the due day, clamped to the month's length."""
    last = calendar.monthrange(month.year, month.month)[1]
    return month.replace(day=min(debt.due_day or last, last))


def promo_active(debt: Debt, month: date) -> bool:
    return debt.promo_apr_end is not None and debt.promo_apr_end > payment_date(debt, month)


def interest(debt: Debt, balance: Decimal, month: date) -> Decimal:
    """`month`'s interest on `balance`, the amount owed going into it."""
    if debt.apr is None or balance <= 0 or promo_active(debt, month):
        return _ZERO
    return _cents(balance * debt.apr / 100 / 12)


def minimum(debt: Debt, balance: Decimal, month_interest: Decimal) -> Decimal:
    """The monthly payment: planned, or the larger of $25 and 1% of `balance` plus interest."""
    if debt.planned_payment is not None:
        return debt.planned_payment
    return max(MIN_PAYMENT, _cents(balance * MIN_PAYMENT_RATE + month_interest))


def first_month(debt: Debt, today: date) -> int:
    """The month of `debt`'s first payment, counted from `today`'s: 0 while this month's
    payment date is still ahead, else 1."""
    return 0 if payment_date(debt, _month(today, 0)) > today else 1


def first_payment(debt: Debt, today: date) -> Decimal:
    """`debt`'s monthly payment in the month of its first payment."""
    month = _month(today, first_month(debt, today))
    return minimum(debt, debt.owed, interest(debt, debt.owed, month))


def apr_in_effect(debt: Debt, month: date) -> Decimal:
    """The rate `debt` charges in `month`: 0 during an active promo or without an APR."""
    if debt.apr is None or promo_active(debt, month):
        return _ZERO
    return debt.apr


@dataclass(frozen=True)
class Payoff:
    """A card paid on its own: `months` (its number of payments) and `total_interest` are
    `None` when it never pays off."""

    months: int | None
    total_interest: Decimal | None


def simulate(debt: Debt, today: date) -> Payoff:
    """Pay `debt` at its monthly payment until it's clear, or give up after `MAX_MONTHS`."""
    balance = debt.owed
    total = _ZERO
    if balance <= 0:
        return Payoff(0, _ZERO)
    first = first_month(debt, today)
    for months in range(1, MAX_MONTHS + 1):
        month = _month(today, first + months - 1)
        charged = interest(debt, balance, month)
        payment = min(minimum(debt, balance, charged), balance + charged)
        balance += charged - payment
        total += charged
        if balance <= 0:
            return Payoff(months, total)
    return Payoff(None, None)


# Ranks a card for a strategy's extra payment in a month, given what it owes after its
# monthly payment: the lowest key gets it first.
Rank = Callable[[Debt, Decimal, date], tuple[Decimal, Decimal, str]]


def avalanche_apr(debt: Debt, balance: Decimal, month: date) -> Decimal:
    """The rate avalanche ranks `debt` by in `month`, owing `balance` after that month's
    payment: during an active promo, 0 when its monthly payments clear it before the promo
    ends, else the APR it charges after; outside one, `apr_in_effect`."""
    if debt.apr is None or not promo_active(debt, month):
        return apr_in_effect(debt, month)
    k = 1
    while balance > 0 and promo_active(debt, _month(month, k)):
        balance -= minimum(debt, balance, _ZERO)
        k += 1
    return _ZERO if balance <= 0 else debt.apr


def avalanche_rank(debt: Debt, balance: Decimal, month: date) -> tuple[Decimal, Decimal, str]:
    """Highest `avalanche_apr` first, then the smaller balance."""
    return (-avalanche_apr(debt, balance, month), balance, debt.name)


def snowball_rank(debt: Debt, balance: Decimal, month: date) -> tuple[Decimal, Decimal, str]:
    """Smallest balance first, then the higher APR in effect that month."""
    return (balance, -apr_in_effect(debt, month), debt.name)


@dataclass(frozen=True)
class Strategy:
    """Every card at its monthly payment, the rest of the budget on the best-ranked card
    still owing. `payoff_months` holds the month each card cleared (counted from `today`'s),
    `focus` the cards in the order they first ranked first, and `start` the plan's first
    month; the totals are `None` when not every card pays off."""

    months: int | None
    total_interest: Decimal | None
    payoff_months: dict[uuid.UUID, int]
    focus: list[uuid.UUID]
    start: int


def _span(lasts: Iterable[int], start: int) -> int:
    """The months from the plan's first, `start`, through the last of `lasts`."""
    return max(max(lasts, default=0) - start + 1, 0)


def run_strategy(debts: list[Debt], rank: Rank, budget: Decimal, today: date) -> Strategy:
    """Each month, every card owing gets its monthly payment; what's left of `budget` goes
    to the cards owing by `rank` that month, rolling over to the next as each is cleared.
    The plan starts this month when a card's payment date is still ahead: that month only
    those cards are paid, the others' payments still count against `budget`. The monthly
    payments are paid in full even when they add up to more than `budget`."""
    balances = {debt.id: debt.owed for debt in debts}
    payoff_months = {debt.id: 0 for debt in debts if debt.owed <= 0}
    firsts = {debt.id: first_month(debt, today) for debt in debts}
    start = min((firsts[debt.id] for debt in debts if debt.owed > 0), default=1)
    focus: list[uuid.UUID] = []
    total = _ZERO
    for k in range(start, start + MAX_MONTHS):
        if len(payoff_months) == len(debts):
            break
        month = _month(today, k)
        spent = _ZERO
        due = []
        for debt in debts:
            balance = balances[debt.id]
            if balance <= 0:
                continue
            charged = interest(debt, balance, month)
            payment = min(minimum(debt, balance, charged), balance + charged)
            spent += payment
            if k < firsts[debt.id]:
                continue
            balances[debt.id] = balance + charged - payment
            total += charged
            due.append(debt)
        extra = budget - spent
        ranked = sorted(due, key=lambda d: rank(d, balances[d.id], month))
        top = next((debt.id for debt in ranked if balances[debt.id] > 0), None)
        if top is not None and top not in focus:
            focus.append(top)
        for debt in ranked:
            if extra <= 0:
                break
            payment = min(extra, max(balances[debt.id], _ZERO))
            balances[debt.id] -= payment
            extra -= payment
        for debt in debts:
            if debt.id not in payoff_months and balances[debt.id] <= 0:
                payoff_months[debt.id] = k
    if len(payoff_months) < len(debts):
        return Strategy(None, None, payoff_months, focus, start)
    return Strategy(_span(payoff_months.values(), start), total, payoff_months, focus, start)


def promo_pace(debt: Debt, today: date) -> PromoPaceOut | None:
    """For a card owing on a 0% promo that's active `today`, the monthly payment that clears
    it with the payments due before the promo ends (all of it now when none are), and whether
    its monthly payment falls short of that."""
    end = debt.promo_apr_end
    if end is None or end <= today or debt.owed <= 0:
        return None
    first = first_month(debt, today)
    months_left = 0
    while payment_date(debt, _month(today, first + months_left)) < end:
        months_left += 1
    needed = debt.owed
    if months_left:
        needed = (debt.owed / months_left).quantize(_CENT, ROUND_UP)
    return PromoPaceOut(
        ends=end,
        months_left=months_left,
        monthly_payment=needed,
        short=first_payment(debt, today) < needed,
    )


def _strategy_out(
    name: Literal["avalanche", "snowball"],
    debts: list[Debt],
    rank: Rank,
    budget: Decimal,
    baseline: Decimal | None,
    today: date,
) -> PayoffStrategyOut:
    result = run_strategy(debts, rank, budget, today)
    plan_start = _month(today, result.start)
    rest = sorted(
        (debt for debt in debts if debt.id not in result.focus),
        key=lambda d: rank(d, d.owed, plan_start),
    )
    by_id = {debt.id: debt for debt in debts}
    order = [by_id[debt_id] for debt_id in result.focus] + rest
    saved = None
    if baseline is not None and result.total_interest is not None:
        saved = baseline - result.total_interest
    last = max(result.payoff_months.values(), default=0)
    return PayoffStrategyOut(
        strategy=name,
        order=[
            PayoffStepOut(
                account_id=debt.id,
                name=debt.name,
                payoff_month=(
                    _label(_month(today, result.payoff_months[debt.id]))
                    if debt.id in result.payoff_months
                    else None
                ),
            )
            for debt in order
        ],
        months_to_debt_free=result.months,
        debt_free_month=None if result.months is None else _label(_month(today, last)),
        total_interest=result.total_interest,
        interest_saved=saved,
    )


def plan(debts: list[Debt], today: date, budget: Decimal | None = None) -> PayoffPlanOut:
    """Each card paid on its own, the debt-free month and interest at the monthly payments,
    and both strategies for `budget` (the sum of the monthly payments when `None`, and raised
    to it when less, with `clamped` set)."""
    cards = []
    lasts: list[int | None] = []
    planned_budget = _ZERO
    for debt in debts:
        first_amount = first_payment(debt, today)
        planned_budget += first_amount
        payoff = simulate(debt, today)
        last = None
        if payoff.months is not None:
            last = max(first_month(debt, today) + payoff.months - 1, 0)
        lasts.append(last)
        cards.append(
            PayoffCardOut(
                account_id=debt.id,
                name=debt.name,
                owed=debt.owed,
                apr=debt.apr,
                promo_apr_end=debt.promo_apr_end,
                planned_payment=debt.planned_payment,
                minimum_payment=first_amount,
                months_to_payoff=payoff.months,
                payoff_month=None if last is None else _label(_month(today, last)),
                total_interest=payoff.total_interest,
                promo=promo_pace(debt, today),
            )
        )

    start = min((first_month(debt, today) for debt in debts if debt.owed > 0), default=1)
    finished = [last for last in lasts if last is not None]
    never = len(finished) < len(lasts)
    months = None if never else _span(finished, start)
    baseline = None if never else sum((card.total_interest or _ZERO for card in cards), _ZERO)
    requested = None if budget is None else _cents(budget)
    used = planned_budget if requested is None else max(requested, planned_budget)
    return PayoffPlanOut(
        budget=used,
        requested_budget=requested,
        clamped=requested is not None and requested < planned_budget,
        planned_budget=planned_budget,
        months_to_debt_free=months,
        debt_free_month=None if never else _label(_month(today, max(finished, default=0))),
        total_interest=baseline,
        cards=cards,
        avalanche=_strategy_out("avalanche", debts, avalanche_rank, used, baseline, today),
        snowball=_strategy_out("snowball", debts, snowball_rank, used, baseline, today),
    )
