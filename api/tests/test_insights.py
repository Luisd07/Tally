"""#225: spending insights found by rules, worded by Claude and pushed at most once a day.
`NOW` is 2026-09-10 in the app timezone: day 10 of a 30-day month, so a month-to-date spend
projects to exactly 3x by month end. The `account` fixture carries one small June charge,
so a merchant charged for the first time in September is new against enough history. The
SDK client and APNs are replaced, so no test reaches either."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import worker
from app.config import APP_TIMEZONE, Settings
from app.models import (
    Account,
    AIAction,
    AIUsage,
    Category,
    Insight,
    Subscription,
    Transaction,
    User,
)
from app.models.enums import AccountKind, AIActionSource, AIActionStatus, InsightKind
from app.services import ai, sync
from app.services.ai_actions import EXPIRES_AFTER
from app.services.insights import (
    build_prompt,
    detect_insights,
    run_insights,
    safe_name,
    write_messages,
)
from tests.factories import (
    make_account,
    make_category,
    make_device,
    make_snapshot,
    make_subscription,
    make_transaction,
)

NOW = datetime(2026, 9, 10, 16, tzinfo=UTC)
TODAY = date(2026, 9, 10)


@pytest.fixture
def account(db_session: Session, user: User) -> Account:
    acct = make_account(user)
    db_session.add(acct)
    db_session.flush()
    _txn(db_session, user, acct, "-5.00", date(2026, 6, 1), merchant="CORNER STORE")
    return acct


def _category(db_session: Session, user: User, **kwargs: object) -> Category:
    category = make_category(user, name="Groceries", **kwargs)
    db_session.add(category)
    db_session.flush()
    return category


def _txn(
    db_session: Session, user: User, account: Account, amount: str, posted: date, **kwargs: object
) -> Transaction:
    txn = make_transaction(user, account, amount=Decimal(amount), posted_date=posted, **kwargs)
    db_session.add(txn)
    db_session.flush()
    return txn


def _detect(db_session: Session, user: User, kind: InsightKind, now: datetime = NOW):
    return [i for i in detect_insights(db_session, user.id, now) if i.kind == kind]


def _action(db_session: Session, insight: Insight) -> AIAction:
    assert insight.action_id is not None
    return db_session.get_one(AIAction, insight.action_id)


# --- pace --------------------------------------------------------------------------------


def test_pace_over_budget_fires(db_session: Session, user: User, account: Account) -> None:
    category = _category(db_session, user, monthly_budget=Decimal("300.00"))
    _txn(db_session, user, account, "-120.00", date(2026, 9, 3), category_id=category.id)

    [insight] = _detect(db_session, user, InsightKind.pace)

    assert insight.category_id == category.id
    assert insight.subject_key == f"pace:{category.id}:2026-09"
    assert insight.day == TODAY
    assert "$360.00" in insight.message and "$300.00 budget" in insight.message
    assert insight.action_id is None


def test_pace_under_fifty_over_budget_does_not_fire(
    db_session: Session, user: User, account: Account
) -> None:
    category = _category(db_session, user, monthly_budget=Decimal("300.00"))
    # Projects to $348: $48 over.
    _txn(db_session, user, account, "-116.00", date(2026, 9, 3), category_id=category.id)

    assert _detect(db_session, user, InsightKind.pace) == []


def test_pace_waits_until_day_five(db_session: Session, user: User, account: Account) -> None:
    category = _category(db_session, user, monthly_budget=Decimal("300.00"))
    _txn(db_session, user, account, "-120.00", date(2026, 9, 2), category_id=category.id)
    day_four = datetime(2026, 9, 4, 16, tzinfo=UTC)

    assert _detect(db_session, user, InsightKind.pace, day_four) == []
    assert _detect(db_session, user, InsightKind.pace, day_four + timedelta(days=1))


def _usual_months(db_session: Session, user: User, account: Account, category: Category) -> None:
    for month in (6, 7, 8):
        _txn(db_session, user, account, "-100.00", date(2026, month, 2), category_id=category.id)


def test_pace_without_budget_against_usual_month(
    db_session: Session, user: User, account: Account
) -> None:
    category = _category(db_session, user)
    _usual_months(db_session, user, account, category)
    # Projects to $180 against 1.25 x $100.
    _txn(db_session, user, account, "-60.00", date(2026, 9, 3), category_id=category.id)

    [insight] = _detect(db_session, user, InsightKind.pace)

    assert "usual $100.00" in insight.message


def test_pace_without_budget_under_fifty_over_does_not_fire(
    db_session: Session, user: User, account: Account
) -> None:
    category = _category(db_session, user)
    _usual_months(db_session, user, account, category)
    # Projects to $174: $49 over $125.
    _txn(db_session, user, account, "-58.00", date(2026, 9, 3), category_id=category.id)

    assert _detect(db_session, user, InsightKind.pace) == []


def test_pace_without_budget_or_history_does_not_fire(
    db_session: Session, user: User, account: Account
) -> None:
    category = _category(db_session, user)
    _txn(db_session, user, account, "-150.00", date(2026, 9, 3), category_id=category.id)

    assert _detect(db_session, user, InsightKind.pace) == []


def test_pace_usual_month_averages_only_months_with_spend(
    db_session: Session, user: User, account: Account
) -> None:
    category = _category(db_session, user)
    for month in (6, 8):
        _txn(db_session, user, account, "-100.00", date(2026, month, 2), category_id=category.id)
    _txn(db_session, user, account, "-60.00", date(2026, 9, 3), category_id=category.id)

    [insight] = _detect(db_session, user, InsightKind.pace)

    assert "usual $100.00" in insight.message


def test_pace_without_budget_needs_two_months_of_spend(
    db_session: Session, user: User, account: Account
) -> None:
    category = _category(db_session, user)
    _txn(db_session, user, account, "-100.00", date(2026, 8, 2), category_id=category.id)
    _txn(db_session, user, account, "-150.00", date(2026, 9, 3), category_id=category.id)

    assert _detect(db_session, user, InsightKind.pace) == []


def test_pace_ignores_archived_categories(
    db_session: Session, user: User, account: Account
) -> None:
    category = _category(db_session, user, monthly_budget=Decimal("300.00"), archived=True)
    _txn(db_session, user, account, "-120.00", date(2026, 9, 3), category_id=category.id)

    assert _detect(db_session, user, InsightKind.pace) == []


def test_pace_ignores_pending_rows(db_session: Session, user: User, account: Account) -> None:
    category = _category(db_session, user, monthly_budget=Decimal("300.00"))
    _txn(
        db_session,
        user,
        account,
        "-120.00",
        date(2026, 9, 3),
        category_id=category.id,
        pending=True,
    )

    assert _detect(db_session, user, InsightKind.pace) == []


# --- unusual charges ---------------------------------------------------------------------


def _cafe_history(db_session: Session, user: User, account: Account, count: int = 3) -> None:
    for day in range(count):
        _txn(db_session, user, account, "-10.00", date(2026, 8, 3 + day * 7), merchant="CAFE")


def test_charge_at_two_and_a_half_times_usual_fires(
    db_session: Session, user: User, account: Account
) -> None:
    _cafe_history(db_session, user, account)
    txn = _txn(db_session, user, account, "-25.00", date(2026, 9, 9), merchant="CAFE")

    [insight] = _detect(db_session, user, InsightKind.unusual_charge)

    assert insight.transaction_id == txn.id
    assert insight.subject_key == f"charge:{txn.id}"
    assert "usually spend about $10.00" in insight.message


def test_charge_at_two_point_four_times_usual_does_not_fire(
    db_session: Session, user: User, account: Account
) -> None:
    _cafe_history(db_session, user, account)
    _txn(db_session, user, account, "-24.00", date(2026, 9, 9), merchant="CAFE")

    assert _detect(db_session, user, InsightKind.unusual_charge) == []


def test_charge_needs_three_prior_charges(
    db_session: Session, user: User, account: Account
) -> None:
    _cafe_history(db_session, user, account, count=2)
    _txn(db_session, user, account, "-100.00", date(2026, 9, 9), merchant="CAFE")

    assert _detect(db_session, user, InsightKind.unusual_charge) == []


def test_only_charges_from_the_last_three_days(
    db_session: Session, user: User, account: Account
) -> None:
    _cafe_history(db_session, user, account)
    _txn(db_session, user, account, "-50.00", date(2026, 9, 6), merchant="CAFE")

    assert _detect(db_session, user, InsightKind.unusual_charge) == []


def test_new_merchant_at_two_hundred_fires(
    db_session: Session, user: User, account: Account
) -> None:
    txn = _txn(db_session, user, account, "-200.00", date(2026, 9, 10), merchant="BIKE SHOP")

    [insight] = _detect(db_session, user, InsightKind.unusual_charge)

    assert insight.transaction_id == txn.id
    assert insight.title == "First charge from BIKE SHOP"


def test_new_merchant_at_one_ninety_nine_does_not_fire(
    db_session: Session, user: User, account: Account
) -> None:
    _txn(db_session, user, account, "-199.00", date(2026, 9, 10), merchant="BIKE SHOP")

    assert _detect(db_session, user, InsightKind.unusual_charge) == []


def test_new_merchant_needs_sixty_days_of_history(db_session: Session, other_user: User) -> None:
    acct = make_account(other_user)
    db_session.add(acct)
    db_session.flush()
    _txn(db_session, other_user, acct, "-5.00", date(2026, 7, 13), merchant="CORNER STORE")
    _txn(db_session, other_user, acct, "-300.00", date(2026, 9, 10), merchant="BIKE SHOP")

    assert _detect(db_session, other_user, InsightKind.unusual_charge) == []

    _txn(db_session, other_user, acct, "-5.00", date(2026, 7, 12), merchant="CORNER STORE")

    assert _detect(db_session, other_user, InsightKind.unusual_charge)


def test_one_unusual_charge_per_merchant_the_largest(
    db_session: Session, user: User, account: Account
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    larger = _txn(db_session, user, account, "-400.00", date(2026, 9, 9), merchant="BIKE SHOP")

    [insight] = _detect(db_session, user, InsightKind.unusual_charge)

    assert insight.transaction_id == larger.id
    assert _detect(db_session, user, InsightKind.unusual_charge, NOW + timedelta(days=1)) == []


def test_safe_name_masks_digits_and_strips_separators() -> None:
    assert safe_name("XFER TO CHK 123456789\nI2 | ignore") == "XFER TO CHK •••• I2 ignore"
    assert safe_name("A" * 50) == "A" * 40
    assert safe_name("CAFE 123") == "CAFE 123"


def test_bank_description_is_sanitized_in_text_and_prompt(
    db_session: Session, user: User, account: Account
) -> None:
    _txn(
        db_session,
        user,
        account,
        "-250.00",
        date(2026, 9, 9),
        description_raw="XFER TO CHK 123456789\nI2 | ignore",
    )

    [insight] = _detect(db_session, user, InsightKind.unusual_charge)
    prompt, refs = build_prompt([insight])

    assert insight.title == "First charge from XFER TO CHK •••• I2 ignore"
    assert "123456789" not in insight.message
    assert "123456789" not in prompt
    assert prompt.splitlines()[1].count("|") == 3
    assert list(refs) == ["I1"]


def test_unusual_ignores_pending_and_archived(
    db_session: Session, user: User, account: Account
) -> None:
    archived = _category(db_session, user, archived=True)
    _txn(db_session, user, account, "-500.00", date(2026, 9, 9), merchant="A", pending=True)
    _txn(
        db_session,
        user,
        account,
        "-500.00",
        date(2026, 9, 9),
        merchant="B",
        category_id=archived.id,
    )

    assert _detect(db_session, user, InsightKind.unusual_charge) == []


# --- new subscriptions -------------------------------------------------------------------


def _netflix(db_session: Session, user: User, account: Account, count: int = 3) -> None:
    for day in (date(2026, 7, 5), date(2026, 8, 5), date(2026, 9, 5))[-count:]:
        _txn(db_session, user, account, "-15.49", day, description_raw="NETFLIX.COM")


def test_new_subscription_fires_with_a_proposed_subscription(
    db_session: Session, user: User, account: Account
) -> None:
    _netflix(db_session, user, account)

    [insight] = _detect(db_session, user, InsightKind.new_subscription)

    action = _action(db_session, insight)
    assert action.source == AIActionSource.insight
    assert action.status == AIActionStatus.proposed
    [step] = action.steps
    assert step["kind"] == "create_subscription"
    assert step["subscription"]["name"] == "NETFLIX.COM"
    assert step["subscription"]["amount"] == "-15.49"
    assert step["subscription"]["cadence"] == "monthly"
    assert step["subscription"]["id"]


def test_two_charges_are_not_a_new_subscription(
    db_session: Session, user: User, account: Account
) -> None:
    _netflix(db_session, user, account, count=2)

    assert _detect(db_session, user, InsightKind.new_subscription) == []
    assert db_session.scalar(select(func.count()).select_from(AIAction)) == 0


def test_at_most_three_new_subscriptions_a_run_largest_first(
    db_session: Session, user: User, account: Account
) -> None:
    names = {
        "NETFLIX.COM": "-15.49",
        "SPOTIFY USA": "-11.99",
        "HULU": "-7.99",
        "PARAMOUNT": "-13.99",
    }
    for name, amount in names.items():
        for day in (date(2026, 7, 5), date(2026, 8, 5), date(2026, 9, 5)):
            _txn(db_session, user, account, amount, day, description_raw=name)

    found = _detect(db_session, user, InsightKind.new_subscription)

    assert sorted(i.subject_key for i in found) == [
        "subscription:NETFLIX.COM",
        "subscription:PARAMOUNT",
        "subscription:SPOTIFY USA",
    ]


@pytest.mark.parametrize("declined", ["not now", "dismissed", None])
def test_a_declined_subscription_is_not_raised_again(
    db_session: Session, user: User, account: Account, declined: str | None
) -> None:
    _netflix(db_session, user, account)
    [insight] = _detect(db_session, user, InsightKind.new_subscription)
    if declined == "not now":
        _action(db_session, insight).status = AIActionStatus.dismissed
    elif declined == "dismissed":
        insight.dismissed_at = NOW
    _txn(db_session, user, account, "-15.49", date(2026, 10, 5), description_raw="NETFLIX.COM")

    later = _detect(db_session, user, InsightKind.new_subscription, NOW + timedelta(days=31))

    assert bool(later) is (declined is None)


# --- price changes -----------------------------------------------------------------------


def _spotify(db_session: Session, user: User, account: Account, charged: str) -> Subscription:
    sub = make_subscription(user, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    db_session.add(sub)
    db_session.flush()
    _txn(db_session, user, account, charged, date(2026, 9, 1), subscription_id=sub.id)
    return sub


def test_price_change_fires_with_a_proposed_update(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _spotify(db_session, user, account, "-13.99")

    [insight] = _detect(db_session, user, InsightKind.price_change)

    assert insight.subscription_id == sub.id
    assert insight.title == "Spotify went up"
    action = _action(db_session, insight)
    assert action.source == AIActionSource.insight
    assert action.steps == [
        {"kind": "update_subscription", "id": str(sub.id), "patch": {"amount": "-13.99"}}
    ]


@pytest.mark.parametrize("declined", [True, False])
def test_a_declined_price_change_is_not_raised_again(
    db_session: Session, user: User, account: Account, declined: bool
) -> None:
    _spotify(db_session, user, account, "-13.99")
    [insight] = _detect(db_session, user, InsightKind.price_change)
    if declined:
        _action(db_session, insight).status = AIActionStatus.dismissed

    later = _detect(db_session, user, InsightKind.price_change, NOW + timedelta(days=31))

    assert bool(later) is not declined


def test_price_within_one_percent_does_not_fire(
    db_session: Session, user: User, account: Account
) -> None:
    _spotify(db_session, user, account, "-12.05")

    assert _detect(db_session, user, InsightKind.price_change) == []


# --- dedupe ------------------------------------------------------------------------------


def test_second_run_creates_nothing_new(db_session: Session, user: User, account: Account) -> None:
    category = _category(db_session, user, monthly_budget=Decimal("300.00"))
    _txn(db_session, user, account, "-120.00", date(2026, 9, 3), category_id=category.id)
    _netflix(db_session, user, account)
    _spotify(db_session, user, account, "-13.99")
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    card = make_account(
        user, name="Slate", kind=AccountKind.credit_card, promo_apr_end=date(2027, 4, 1)
    )
    db_session.add(card)
    db_session.flush()
    db_session.add(make_snapshot(user, card, date=date(2026, 9, 1), balance=Decimal("-1200.00")))
    db_session.flush()

    first = detect_insights(db_session, user.id, NOW)
    second = detect_insights(db_session, user.id, NOW + timedelta(days=1))

    assert {i.kind for i in first} == set(InsightKind)
    assert second == []
    assert db_session.scalar(select(func.count()).select_from(AIAction)) == 2


@pytest.mark.parametrize(("days_ago", "fires"), [(29, False), (30, True)])
def test_a_raised_insight_holds_for_thirty_days_even_dismissed(
    db_session: Session, user: User, account: Account, days_ago: int, fires: bool
) -> None:
    txn = _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    db_session.add(
        Insight(
            user_id=user.id,
            kind=InsightKind.unusual_charge,
            title="t",
            message="m",
            subject_key=f"charge:{txn.id}",
            day=TODAY - timedelta(days=days_ago),
            dismissed_at=NOW,
        )
    )
    db_session.flush()

    assert bool(_detect(db_session, user, InsightKind.unusual_charge)) is fires


def test_dedupe_is_per_user(
    db_session: Session, user: User, other_user: User, account: Account
) -> None:
    txn = _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    db_session.add(
        Insight(
            user_id=other_user.id,
            kind=InsightKind.unusual_charge,
            title="t",
            message="m",
            subject_key=f"charge:{txn.id}",
            day=TODAY,
        )
    )
    db_session.flush()

    assert _detect(db_session, user, InsightKind.unusual_charge)


def test_detect_flushes_and_never_commits(
    db_session: Session, user: User, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")

    def refuse() -> None:
        raise AssertionError("detect_insights must not commit")

    monkeypatch.setattr(db_session, "commit", refuse)
    [insight] = detect_insights(db_session, user.id, NOW)

    assert db_session.get(Insight, insight.id) is insight
    assert insight.created_at is not None


# --- messages ----------------------------------------------------------------------------


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: Any = {"insights": []}
        self.stop_reason = "end_turn"

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(
                input_tokens=500,
                output_tokens=100,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            content=[SimpleNamespace(type="text", text=json.dumps(self.answer))],
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMessages:
    messages = FakeMessages()
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    monkeypatch.setattr(ai, "make_client", lambda settings: client)
    return messages


@pytest.fixture
def ai_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})


def test_messages_are_stored_from_claudes_answer(
    db_session: Session, user: User, account: Account, fake: FakeMessages, ai_settings: Settings
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    insights = detect_insights(db_session, user.id, NOW)
    fake.answer = {
        "insights": [
            {
                "id": "I1",
                "title": "Big bike shop bill\x00" + "x" * 80,
                "message": "$250.00 " + "y" * 300,
            },
            {"id": "I9", "title": "Not sent", "message": "Ignored"},
        ]
    }

    write_messages(db_session, ai_settings, user.id, insights, NOW)

    [call] = fake.calls
    prompt = call["messages"][0]["content"]
    assert "I1 | unusual_charge | First charge from BIKE SHOP" in prompt
    assert account.name not in prompt
    assert call["output_config"]["format"]["type"] == "json_schema"
    [insight] = insights
    assert insight.title == ("Big bike shop bill" + "x" * 80)[:60]
    assert insight.message == ("$250.00 " + "y" * 300)[:200]
    usage = db_session.scalars(select(AIUsage).where(AIUsage.user_id == user.id)).one()
    assert usage.feature == "insights"


def test_rule_text_stays_when_ai_is_off(
    db_session: Session, user: User, account: Account, fake: FakeMessages, settings: Settings
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    [insight] = detect_insights(db_session, user.id, NOW)

    write_messages(db_session, settings, user.id, [insight], NOW)

    assert fake.calls == []
    assert insight.title == "First charge from BIKE SHOP"
    assert insight.message.startswith("$250.00 at BIKE SHOP")


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ("Big bike bill", "You spent $25.00 at a new bike shop."),
        ("Big bike bill", "You spent a lot at a new bike shop."),
        ("", "You spent $250.00 at a new bike shop."),
    ],
)
def test_rule_text_stays_when_claudes_text_drops_the_amount(
    db_session: Session,
    user: User,
    account: Account,
    fake: FakeMessages,
    ai_settings: Settings,
    title: str,
    message: str,
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    [insight] = detect_insights(db_session, user.id, NOW)
    rule_message = insight.message
    fake.answer = {"insights": [{"id": "I1", "title": title, "message": message}]}

    write_messages(db_session, ai_settings, user.id, [insight], NOW)

    assert insight.title == "First charge from BIKE SHOP"
    assert insight.message == rule_message


def test_rule_text_stays_when_claude_declines(
    db_session: Session, user: User, account: Account, fake: FakeMessages, ai_settings: Settings
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    [insight] = detect_insights(db_session, user.id, NOW)
    fake.stop_reason = "refusal"
    fake.answer = {"insights": [{"id": "I1", "title": "T", "message": "M"}]}

    write_messages(db_session, ai_settings, user.id, [insight], NOW)

    assert insight.title == "First charge from BIKE SHOP"


# --- nightly run and push ----------------------------------------------------------------


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    def send_alert(db: Session, user_id: object, title: str, body: str, *args: object) -> None:
        sent.append((title, body))

    monkeypatch.setattr(sync.apns, "send_alert", send_alert)
    return sent


def _no_apns() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("APNs must not be reached")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _run(db_session: Session, settings: Settings, user: User, now: datetime = NOW):
    found, push = run_insights(db_session, settings, user.id, now)
    sync.notify(db_session, user.id, now, settings, push, apns_client=_no_apns())
    return found


def _night(
    db_session: Session,
    settings: Settings,
    user: User,
    push: sync.PushIntent | None,
    now: datetime = NOW,
) -> None:
    worker.insights_and_push(db_session, settings, user.id, now, push, apns_client=_no_apns())


CHARGES = sync.PushIntent("Tally", "2 new charges")


def test_new_charges_and_an_insight_make_one_push(
    db_session: Session,
    user: User,
    account: Account,
    settings: Settings,
    pushes: list[tuple[str, str]],
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")

    _night(db_session, settings, user, CHARGES)

    assert pushes == [("Tally", "2 new charges · First charge from BIKE SHOP")]
    insight = db_session.scalars(select(Insight).where(Insight.user_id == user.id)).one()
    assert insight.pushed_at == NOW


def test_new_charges_alone_send_the_sync_push(
    db_session: Session, user: User, settings: Settings, pushes: list[tuple[str, str]]
) -> None:
    _night(db_session, settings, user, CHARGES)

    assert pushes == [("Tally", "2 new charges")]


def test_an_insight_alone_sends_its_own_push(
    db_session: Session,
    user: User,
    account: Account,
    settings: Settings,
    pushes: list[tuple[str, str]],
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")

    _night(db_session, settings, user, None)

    insight = db_session.scalars(select(Insight).where(Insight.user_id == user.id)).one()
    assert pushes == [("Tally", insight.message)]


def test_a_second_night_run_the_same_day_adds_no_insight(
    db_session: Session,
    user: User,
    account: Account,
    settings: Settings,
    pushes: list[tuple[str, str]],
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    _night(db_session, settings, user, CHARGES)
    _txn(db_session, user, account, "-300.00", date(2026, 9, 10), merchant="SKI SHOP")
    _night(db_session, settings, user, CHARGES, NOW + timedelta(hours=2))

    assert pushes[-1] == ("Tally", "2 new charges")
    assert len(pushes) == 2


def test_a_failed_insights_run_still_sends_the_sync_push(
    db_session: Session,
    user: User,
    settings: Settings,
    pushes: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(worker.insights, "run_insights", fail)

    _night(db_session, settings, user, CHARGES)

    assert pushes == [("Tally", "2 new charges")]


def test_one_push_a_day_however_many_runs(
    db_session: Session,
    user: User,
    account: Account,
    settings: Settings,
    pushes: list[tuple[str, str]],
) -> None:
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    [first] = _run(db_session, settings, user)
    _txn(db_session, user, account, "-300.00", date(2026, 9, 10), merchant="SKI SHOP")
    [second] = _run(db_session, settings, user, NOW + timedelta(hours=2))

    assert pushes == [("Tally", first.message)]
    assert first.pushed_at == NOW
    assert second.pushed_at is None

    _txn(db_session, user, account, "-400.00", date(2026, 9, 11), merchant="SURF SHOP")
    [third] = _run(db_session, settings, user, NOW + timedelta(days=1))

    assert pushes[-1] == ("Tally", third.message)


def test_a_quiet_day_pushes_nothing(
    db_session: Session, user: User, settings: Settings, pushes: list[tuple[str, str]]
) -> None:
    assert _run(db_session, settings, user) == []
    assert pushes == []


def test_push_picks_charges_before_subscriptions_then_the_largest(
    db_session: Session,
    user: User,
    account: Account,
    settings: Settings,
    pushes: list[tuple[str, str]],
) -> None:
    _netflix(db_session, user, account)
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")
    _txn(db_session, user, account, "-400.00", date(2026, 9, 9), merchant="SKI SHOP")

    found = _run(db_session, settings, user)

    assert {i.kind for i in found} == {InsightKind.unusual_charge, InsightKind.new_subscription}
    [pushed] = [i for i in found if i.pushed_at is not None]
    assert pushed.transaction_id is not None
    assert "SKI SHOP" in pushed.message
    assert pushes == [("Tally", pushed.message)]


def test_a_failed_push_still_commits_the_insights(
    db_session: Session,
    user: User,
    account: Account,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(make_device(user))
    _txn(db_session, user, account, "-250.00", date(2026, 9, 9), merchant="BIKE SHOP")

    def send_alert(*args: object) -> None:
        raise RuntimeError("apns is down")

    monkeypatch.setattr(sync.apns, "send_alert", send_alert)
    [insight] = _run(db_session, settings, user)
    db_session.rollback()

    stored = db_session.get_one(Insight, insight.id)
    assert stored.pushed_at == NOW


# --- routes ------------------------------------------------------------------------------


def _local_today() -> date:
    return datetime.now(UTC).astimezone(APP_TIMEZONE).date()


def _stored(db_session: Session, user: User, key: str, day: date, **kwargs: object) -> Insight:
    insight = Insight(
        user_id=user.id,
        kind=InsightKind.unusual_charge,
        title=key,
        message=f"{key} message",
        subject_key=key,
        day=day,
        **kwargs,
    )
    db_session.add(insight)
    db_session.flush()
    return insight


def test_list_shows_recent_undismissed_newest_first(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    today = _local_today()
    _stored(db_session, user, "older", today - timedelta(days=13))
    _stored(db_session, user, "newest", today)
    _stored(db_session, user, "too old", today - timedelta(days=14))
    _stored(db_session, user, "dismissed", today, dismissed_at=NOW)
    _stored(db_session, other_user, "theirs", today)

    response = db_client.get("/api/insights", headers=auth_headers)

    assert response.status_code == 200
    assert [i["title"] for i in response.json()] == ["newest", "older"]
    assert response.json()[0]["action_status"] is None


def test_list_carries_the_proposed_action(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
) -> None:
    _netflix(db_session, user, account)
    [insight] = _detect(db_session, user, InsightKind.new_subscription)
    insight.day = _local_today()
    db_session.flush()

    [item] = db_client.get("/api/insights", headers=auth_headers).json()

    assert item["action_id"] == str(insight.action_id)
    assert item["action_status"] == AIActionStatus.proposed


def test_list_leaves_off_an_expired_action(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
) -> None:
    _netflix(db_session, user, account)
    [insight] = _detect(db_session, user, InsightKind.new_subscription)
    insight.day = _local_today()
    _action(db_session, insight).created_at = (
        datetime.now(UTC) - EXPIRES_AFTER - timedelta(minutes=1)
    )
    db_session.flush()

    [item] = db_client.get("/api/insights", headers=auth_headers).json()

    assert item["id"] == str(insight.id)
    assert item["action_id"] is None
    assert item["action_status"] is None


def test_dismiss_hides_it_and_others_are_not_found(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    mine = _stored(db_session, user, "mine", _local_today())
    theirs = _stored(db_session, other_user, "theirs", _local_today())

    response = db_client.post(f"/api/insights/{mine.id}/dismiss", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["dismissed_at"] is not None
    assert db_client.get("/api/insights", headers=auth_headers).json() == []

    response = db_client.post(f"/api/insights/{theirs.id}/dismiss", headers=auth_headers)
    assert response.status_code == 404
    assert theirs.dismissed_at is None
