"""#243: the credit card payoff plan. `TODAY` is 2026-10-27 and the cards are due on the 15th,
so the first payment is in November 2026."""

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, Insight, User
from app.models.enums import AccountKind, InsightKind
from app.services import ai, ai_chat_tools, payoff
from app.services.accounts import get_payoff_plan
from app.services.insights import detect_insights, run_insights, write_messages
from app.services.payoff import Debt, plan, promo_pace, run_strategy, simulate
from tests.factories import make_category, make_snapshot, make_transaction

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 16, tzinfo=UTC)


def _debt(
    owed: str,
    apr: str | None = None,
    planned: str | None = None,
    name: str = "Card",
    promo_apr_end: date | None = None,
    due_day: int | None = 15,
) -> Debt:
    return Debt(
        id=uuid.uuid4(),
        name=name,
        owed=Decimal(owed),
        apr=Decimal(apr) if apr is not None else None,
        promo_apr_end=promo_apr_end,
        planned_payment=Decimal(planned) if planned is not None else None,
        due_day=due_day,
    )


# -- Simulator ----------------------------------------------------------------------


def test_known_amortization_to_the_cent() -> None:
    # $1,000 at 18% APR paid $100 a month: 1.5% a month, 11 payments, $91.62 in interest.
    result = simulate(_debt("1000.00", "18.00", "100.00"), TODAY)

    assert result == payoff.Payoff(11, Decimal("91.62"))


def test_payoff_month_counts_from_next_month() -> None:
    [card] = plan([_debt("1000.00", "18.00", "100.00")], TODAY).cards

    assert card.months_to_payoff == 11
    assert card.payoff_month == "2027-09"
    assert card.total_interest == Decimal("91.62")


def test_no_interest_while_the_promo_is_active() -> None:
    # Payments on the 15th: November through March fall before the April 1 promo end.
    debt = _debt("1200.00", "24.00", "100.00", promo_apr_end=date(2027, 4, 1))

    assert payoff.interest(debt, Decimal("700.00"), date(2027, 3, 1)) == Decimal("0.00")
    assert payoff.interest(debt, Decimal("700.00"), date(2027, 4, 1)) == Decimal("14.00")
    # Five free months leave $700, then 2% a month: 14.00 + 12.28 + 10.53 + 8.74 + 6.91
    # + 5.05 + 3.15 + 1.21.
    assert simulate(debt, TODAY) == payoff.Payoff(13, Decimal("61.87"))


def test_never_pays_off_when_the_payment_only_covers_interest() -> None:
    [card] = plan([_debt("5000.00", "24.00", "100.00")], TODAY).cards

    assert card.months_to_payoff is None
    assert card.payoff_month is None
    assert card.total_interest is None


def test_caps_at_600_months() -> None:
    assert simulate(_debt("6000.00", planned="10.00"), TODAY).months == 600
    assert simulate(_debt("6000.01", planned="10.00"), TODAY) == payoff.Payoff(None, None)


def test_minimum_without_a_planned_payment() -> None:
    # 1% of $1,000 plus $20.00 interest beats the $25 floor; $2,000 at no APR doesn't.
    [high, low] = plan([_debt("1000.00", "24.00"), _debt("2000.00")], TODAY).cards

    assert high.minimum_payment == Decimal("30.00")
    assert low.minimum_payment == Decimal("25.00")
    assert high.months_to_payoff is not None


def test_a_plan_is_never_debt_free_when_one_card_never_pays_off() -> None:
    result = plan([_debt("1000.00", "18.00", "100.00"), _debt("5000.00", "24.00", "100.00")], TODAY)

    assert result.months_to_debt_free is None
    assert result.total_interest is None
    assert result.avalanche.interest_saved is None


# -- Strategies -----------------------------------------------------------------------


def _three_cards() -> list[Debt]:
    return [
        _debt("3000.00", "24.00", "100.00", name="Big"),
        _debt("1000.00", "18.00", "50.00", name="Middle"),
        _debt("500.00", "12.00", "40.00", name="Small"),
    ]


def test_strategy_orders() -> None:
    result = plan(_three_cards(), TODAY)

    assert [s.name for s in result.avalanche.order] == ["Big", "Middle", "Small"]
    assert [s.name for s in result.snowball.order] == ["Small", "Middle", "Big"]


def test_avalanche_costs_no_more_than_snowball_and_both_beat_planned_payments() -> None:
    result = plan(_three_cards(), TODAY, Decimal("400.00"))
    avalanche, snowball = result.avalanche, result.snowball

    assert result.planned_budget == Decimal("190.00")
    assert result.budget == Decimal("400.00")
    assert result.total_interest is not None
    assert avalanche.total_interest is not None and snowball.total_interest is not None
    assert avalanche.total_interest < snowball.total_interest
    assert avalanche.interest_saved == result.total_interest - avalanche.total_interest
    assert snowball.interest_saved == result.total_interest - snowball.total_interest
    assert snowball.interest_saved is not None and snowball.interest_saved > 0
    assert avalanche.debt_free_month is not None and result.debt_free_month is not None
    assert avalanche.debt_free_month < result.debt_free_month


def test_payoff_months_follow_the_strategy_order() -> None:
    result = plan(_three_cards(), TODAY, Decimal("400.00"))

    snowball = [s.payoff_month or "never" for s in result.snowball.order]
    assert "never" not in snowball
    assert snowball == sorted(snowball)
    assert result.snowball.debt_free_month == snowball[-1]


def test_freed_payments_roll_over_at_the_planned_budget() -> None:
    result = plan(_three_cards(), TODAY)

    assert result.budget == Decimal("190.00")
    assert result.avalanche.interest_saved is not None and result.avalanche.interest_saved > 0
    assert result.avalanche.months_to_debt_free is not None
    assert result.months_to_debt_free is not None
    assert result.avalanche.months_to_debt_free < result.months_to_debt_free


def test_a_budget_below_the_planned_payments_still_pays_them() -> None:
    assert plan(_three_cards(), TODAY, Decimal("10.00")).budget == Decimal("190.00")


def _first(card: Debt) -> payoff.Rank:
    """A fixed order: `card` first, then the smaller balance."""
    return lambda d, b, m: (Decimal(d is not card), b, d.name)


def test_avalanche_ranks_a_promo_card_by_its_later_apr_when_minimums_leave_a_balance() -> None:
    # Promo's one payment before December 1 leaves most of $5000 to accrue 29.99%, so
    # avalanche counts it at 29.99% from the start rather than 0% in November.
    promo = _debt("5000.00", "29.99", name="Promo", promo_apr_end=date(2026, 12, 1))
    regular = _debt("2000.00", "10.00", name="Regular")
    budget = Decimal("300.00")

    result = plan([promo, regular], TODAY, budget)
    promo_first = run_strategy([promo, regular], _first(promo), budget, TODAY)

    assert [s.name for s in result.avalanche.order] == ["Promo", "Regular"]
    avalanche, snowball = result.avalanche.total_interest, result.snowball.total_interest
    assert avalanche is not None and snowball is not None
    assert promo_first.total_interest is not None
    assert avalanche <= promo_first.total_interest
    assert avalanche < snowball


def test_avalanche_ranks_a_promo_card_at_zero_when_minimums_clear_it_in_time() -> None:
    # Promo's $200 payments clear $1000 by March, before the June 1 promo end: the extra
    # goes to Regular instead, which costs less than paying Promo down first.
    promo = _debt("1000.00", "29.99", "200.00", name="Promo", promo_apr_end=date(2027, 6, 1))
    regular = _debt("3000.00", "20.00", name="Regular")
    budget = Decimal("500.00")

    result = plan([promo, regular], TODAY, budget)
    promo_first = run_strategy([promo, regular], _first(promo), budget, TODAY)

    assert [s.name for s in result.avalanche.order] == ["Regular", "Promo"]
    assert result.avalanche.total_interest is not None
    assert promo_first.total_interest is not None
    assert result.avalanche.total_interest < promo_first.total_interest


def test_a_budget_below_the_planned_payments_is_raised_and_says_so() -> None:
    low = plan(_three_cards(), TODAY, Decimal("10.00"))
    high = plan(_three_cards(), TODAY, Decimal("400.00"))
    planned = plan(_three_cards(), TODAY)

    assert (low.budget, low.requested_budget, low.clamped) == (
        Decimal("190.00"),
        Decimal("10.00"),
        True,
    )
    assert (high.budget, high.requested_budget, high.clamped) == (
        Decimal("400.00"),
        Decimal("400.00"),
        False,
    )
    assert (planned.requested_budget, planned.clamped) == (None, False)


# -- The first payment ----------------------------------------------------------------------


def test_the_first_payment_is_this_month_while_its_due_date_is_ahead() -> None:
    # October 3 with the card due on the 28th: the first of 11 payments is October 28.
    oct_3 = date(2026, 10, 3)
    debt = _debt("1000.00", "18.00", "100.00", due_day=28)

    result = plan([debt], oct_3)

    [card] = result.cards
    assert (card.months_to_payoff, card.payoff_month) == (11, "2027-08")
    assert (result.months_to_debt_free, result.debt_free_month) == (11, "2027-08")
    assert result.avalanche.debt_free_month == "2027-08"
    assert result.avalanche.months_to_debt_free == 11


def test_promo_pace_counts_this_months_payment_while_it_is_ahead() -> None:
    # October 28 and November 28 fall before the December 1 promo end.
    debt = _debt("1000.00", "24.00", "100.00", promo_apr_end=date(2026, 12, 1), due_day=28)

    pace = promo_pace(debt, date(2026, 10, 3))

    assert pace is not None
    assert (pace.months_left, pace.monthly_payment, pace.short) == (2, Decimal("500.00"), True)


def test_a_card_already_paid_this_month_waits_for_next_month() -> None:
    # October 20: Early (due the 15th) was paid this month, Late (due the 28th) wasn't, so
    # the plan starts in October with Late alone, and Early's payment still counts against
    # the budget: the extra $100 goes to Late.
    oct_20 = date(2026, 10, 20)
    early = _debt("1000.00", planned="100.00", name="Early", due_day=15)
    late = _debt("1000.00", planned="100.00", name="Late", due_day=28)

    result = plan([early, late], oct_20, Decimal("300.00"))

    # On their own: Early's 10 payments run November to August, Late's October to July.
    assert [c.payoff_month for c in result.cards] == ["2027-08", "2027-07"]
    assert (result.months_to_debt_free, result.debt_free_month) == (11, "2027-08")
    avalanche = {s.name: s.payoff_month for s in result.avalanche.order}
    # Late: $200 a month from October clears it in 5 payments, by February. Early has $600
    # left after four $100 payments, then gets $300 a month: March and April.
    assert avalanche == {"Late": "2027-02", "Early": "2027-04"}
    assert result.avalanche.months_to_debt_free == 7


# -- Promo pacing --------------------------------------------------------------------------


def test_promo_pace_clears_the_balance_by_the_promo_end() -> None:
    debt = _debt("1200.00", "24.00", "100.00", promo_apr_end=date(2027, 4, 1))

    pace = promo_pace(debt, TODAY)

    assert pace is not None
    assert (pace.months_left, pace.monthly_payment, pace.short) == (5, Decimal("240.00"), True)
    paced = _debt("1200.00", "24.00", "240.00", promo_apr_end=date(2027, 4, 1))
    assert simulate(paced, TODAY) == payoff.Payoff(5, Decimal("0.00"))
    paced_pace = promo_pace(paced, TODAY)
    assert paced_pace is not None and paced_pace.short is False


def test_promo_pace_rounds_up_to_the_cent() -> None:
    pace = promo_pace(_debt("1000.00", "24.00", promo_apr_end=date(2027, 2, 1)), TODAY)

    assert pace is not None
    assert (pace.months_left, pace.monthly_payment) == (3, Decimal("333.34"))
    assert pace.short is True  # the $25 minimum


def test_promo_pace_without_a_due_day_assumes_month_end() -> None:
    debt = Debt(uuid.uuid4(), "Card", Decimal("900.00"), promo_apr_end=date(2027, 1, 31))

    pace = promo_pace(debt, TODAY)

    # October 31, November 30 and December 31 fall before it; January 31 doesn't.
    assert pace is not None and pace.months_left == 3


def test_no_promo_pace_without_an_active_promo() -> None:
    assert promo_pace(_debt("1000.00", "24.00"), TODAY) is None
    assert promo_pace(_debt("1000.00", "24.00", promo_apr_end=TODAY), TODAY) is None
    assert promo_pace(_debt("0.00", "24.00", promo_apr_end=date(2027, 4, 1)), TODAY) is None


# -- Loading cards, the route and the chat tool ----------------------------------------------


def _card(db: Session, user: User, balance: str | None, **kwargs: object) -> Account:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Freedom",
        "kind": AccountKind.credit_card,
        "due_day": 15,
    }
    defaults.update(kwargs)
    card = Account(**defaults)
    db.add(card)
    db.flush()
    if balance is not None:
        db.add(make_snapshot(user, card, date=date(2026, 10, 1), balance=Decimal(balance)))
        db.flush()
    return card


def test_plan_covers_only_active_cards_that_owe(
    db_session: Session, user: User, other_user: User
) -> None:
    owes = _card(db_session, user, "-1000.00", apr=Decimal("18.00"), planned_payment=Decimal(100))
    _card(db_session, user, "50.00", name="Credit")
    _card(db_session, user, None, name="No balance")
    _card(db_session, user, "-300.00", name="Closed", active=False)
    _card(db_session, other_user, "-900.00", name="Theirs")
    checking = Account(user_id=user.id, name="Checking", kind=AccountKind.checking)
    db_session.add(checking)
    db_session.flush()
    db_session.add(make_snapshot(user, checking, balance=Decimal("-20.00")))
    db_session.flush()

    result = get_payoff_plan(db_session, user.id, None, NOW)

    [card] = result.cards
    assert card.account_id == owes.id
    assert card.owed == Decimal("1000.00")
    assert card.total_interest == Decimal("91.62")


def test_plan_counts_from_today_in_the_app_timezone(db_session: Session, user: User) -> None:
    # 02:00 UTC on November 1 is still October 31 in New York, so the card due on the 1st
    # makes its first payment on November 1 (on December 1 by the UTC date).
    _card(
        db_session, user, "-1000.00", apr=Decimal("18.00"), planned_payment=Decimal(100), due_day=1
    )

    result = get_payoff_plan(db_session, user.id, None, datetime(2026, 11, 1, 2, tzinfo=UTC))

    assert result.cards[0].payoff_month == "2027-09"


def test_payoff_route(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _card(db_session, user, "-1000.00", apr=Decimal("18.00"), planned_payment=Decimal(100))

    body = db_client.get("/api/accounts/payoff?budget=250", headers=auth_headers).json()
    default = db_client.get("/api/accounts/payoff", headers=auth_headers).json()

    assert body["budget"] == "250.00"
    assert (body["requested_budget"], body["clamped"]) == ("250.00", False)
    assert body["planned_budget"] == "100.00"
    assert body["avalanche"]["strategy"] == "avalanche"
    assert body["snowball"]["order"][0]["name"] == "Freedom"
    assert body["cards"][0]["months_to_payoff"] == 11
    assert default["budget"] == "100.00"
    assert (default["requested_budget"], default["clamped"]) == (None, False)
    low = db_client.get("/api/accounts/payoff?budget=40", headers=auth_headers).json()
    assert (low["budget"], low["requested_budget"], low["clamped"]) == ("100.00", "40.00", True)


@pytest.mark.parametrize("budget", ["-1", "abc", "1.234"])
def test_payoff_route_rejects_a_bad_budget(
    db_client: TestClient, auth_headers: dict[str, str], budget: str
) -> None:
    response = db_client.get(f"/api/accounts/payoff?budget={budget}", headers=auth_headers)

    assert response.status_code == 422


def test_payoff_route_is_not_captured_by_id(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = db_client.get("/api/accounts/payoff", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["cards"] == []


def test_chat_tool_returns_the_plan_for_the_user_only(
    db_session: Session, settings: Settings, user: User, other_user: User
) -> None:
    _card(
        db_session,
        user,
        "-1000.00",
        name="Sapphire 4242",
        apr=Decimal("18.00"),
        planned_payment=Decimal(100),
    )
    _card(db_session, other_user, "-900.00", name="Theirs")

    content, is_error = ai_chat_tools.run(
        db_session, settings, user.id, "payoff_plan", {"monthly_budget": "300"}
    )

    assert not is_error
    result = json.loads(content)
    assert result["budget"] == "300.00"
    assert (result["requested_budget"], result["clamped"]) == ("300.00", False)
    assert [c["name"] for c in result["cards"]] == ["Sapphire ••••"]
    assert result["avalanche"]["order"][0]["name"] == "Sapphire ••••"
    assert "4242" not in content


def test_chat_tool_says_when_the_budget_was_raised(
    db_session: Session, settings: Settings, user: User
) -> None:
    _card(db_session, user, "-1000.00", apr=Decimal("18.00"), planned_payment=Decimal(100))

    content, is_error = ai_chat_tools.run(
        db_session, settings, user.id, "payoff_plan", {"monthly_budget": "40"}
    )

    assert not is_error
    result = json.loads(content)
    assert (result["budget"], result["requested_budget"], result["clamped"]) == (
        "100.00",
        "40.00",
        True,
    )
    [tool] = [t for t in ai_chat_tools.TOOLS if t["name"] == "payoff_plan"]
    assert "clamped" in tool["description"]


def test_chat_tool_counts_from_today_in_the_app_timezone(
    db_session: Session, settings: Settings, user: User
) -> None:
    _card(
        db_session, user, "-1000.00", apr=Decimal("18.00"), planned_payment=Decimal(100), due_day=1
    )

    content, _ = ai_chat_tools.run(
        db_session,
        settings,
        user.id,
        "payoff_plan",
        {"monthly_budget": ""},
        now=datetime(2026, 11, 1, 2, tzinfo=UTC),
    )

    assert json.loads(content)["cards"][0]["payoff_month"] == "2027-09"


@pytest.mark.parametrize("budget", ["abc", "NaN", "-5"])
def test_chat_tool_rejects_a_bad_budget(
    db_session: Session, settings: Settings, user: User, budget: str
) -> None:
    content, is_error = ai_chat_tools.run(
        db_session, settings, user.id, "payoff_plan", {"monthly_budget": budget}
    )

    assert is_error
    assert "monthly_budget" in json.loads(content)["error"]


# -- The promo_pace insight ---------------------------------------------------------------


def _promo_insights(db: Session, user: User, now: datetime = NOW) -> list[Insight]:
    return [i for i in detect_insights(db, user.id, now) if i.kind == InsightKind.promo_pace]


def test_promo_pace_insight_when_the_planned_payment_falls_short(
    db_session: Session, user: User
) -> None:
    card = _card(
        db_session,
        user,
        "-1200.00",
        name="Slate",
        apr=Decimal("24.00"),
        planned_payment=Decimal(100),
        promo_apr_end=date(2027, 4, 1),
    )

    [insight] = _promo_insights(db_session, user)

    assert insight.subject_key == f"promo_pace:{card.id}:2026-10"
    assert insight.title == "Pay $240.00/mo to clear Slate by Apr 1, 2027"
    assert insight.message == (
        "Slate's 0% promo ends Apr 1, 2027. Paying $240.00 a month clears it by then; "
        "your planned payment is $100.00."
    )


def test_promo_pace_insight_is_raised_once_per_card_a_month(
    db_session: Session, user: User
) -> None:
    _card(
        db_session,
        user,
        "-1200.00",
        promo_apr_end=date(2027, 4, 1),
        planned_payment=Decimal(100),
    )

    assert len(_promo_insights(db_session, user)) == 1
    assert _promo_insights(db_session, user, datetime(2026, 10, 30, 16, tzinfo=UTC)) == []
    [november] = _promo_insights(db_session, user, datetime(2026, 11, 28, 16, tzinfo=UTC))
    assert november.subject_key.endswith(":2026-11")


def test_no_promo_pace_insight_when_on_pace_or_for_another_user(
    db_session: Session, user: User, other_user: User
) -> None:
    _card(
        db_session,
        user,
        "-1200.00",
        promo_apr_end=date(2027, 4, 1),
        planned_payment=Decimal(240),
    )
    _card(db_session, other_user, "-1200.00", promo_apr_end=date(2027, 4, 1))

    assert _promo_insights(db_session, user) == []


def test_promo_pace_insight_without_a_planned_payment(db_session: Session, user: User) -> None:
    _card(db_session, user, "-1200.00", promo_apr_end=date(2027, 4, 1))

    [insight] = _promo_insights(db_session, user)

    assert insight.message.endswith("you have no planned payment set.")


def test_promo_pace_insight_is_not_sent_to_claude(
    db_session: Session, settings: Settings, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    _card(db_session, user, "-1200.00", name="Slate", promo_apr_end=date(2027, 4, 1))
    [insight] = _promo_insights(db_session, user)
    ai_settings = settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})

    def no_client(settings: Settings) -> None:
        raise AssertionError("Claude was called")

    monkeypatch.setattr(ai, "make_client", no_client)
    write_messages(db_session, ai_settings, user.id, [insight], NOW)

    assert insight.title.startswith("Pay $240.00/mo")


def test_promo_pace_and_pace_rank_by_the_same_minimum_for_the_push(
    db_session: Session, settings: Settings, user: User
) -> None:
    # The promo ends November 1, before the November 15 payment: all $1,000 is due now, and
    # that payment carries $20.00 of interest, so its minimum is $30.00 and it falls $970.00
    # short. Groceries is on pace for $1,085.00 against $112.00, $973.00 over, so it goes
    # first; without the interest the promo would be $975.00 short and win.
    _card(
        db_session,
        user,
        "-1000.00",
        name="Slate",
        apr=Decimal("24.00"),
        promo_apr_end=date(2026, 11, 1),
    )
    checking = Account(user_id=user.id, name="Checking", kind=AccountKind.checking)
    groceries = make_category(user, name="Groceries", monthly_budget=Decimal("112.00"))
    db_session.add_all([checking, groceries])
    db_session.flush()
    db_session.add(
        make_transaction(
            user,
            checking,
            amount=Decimal("-945.00"),
            posted_date=date(2026, 10, 1),
            category_id=groceries.id,
        )
    )
    db_session.flush()

    found, _ = run_insights(db_session, settings, user.id, NOW)

    assert {i.kind for i in found} == {InsightKind.pace, InsightKind.promo_pace}
    [pushed] = [i for i in found if i.pushed_at is not None]
    assert pushed.kind == InsightKind.pace
