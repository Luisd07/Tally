"""#78: subscription suggestions and flags, computed on read. Service tests pass a fixed
`NOW` (2026-10-16); the HTTP tests build dates relative to the real clock the router uses.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import Account, Subscription, Transaction, User
from app.models.enums import Cadence
from app.services.subscription_detection import subscription_flags, suggest_subscriptions
from tests.factories import make_account, make_subscription, make_transaction

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
TODAY = NOW.date()
SUBSCRIPTIONS = "/api/subscriptions"


@pytest.fixture
def account(db_session: Session, user: User) -> Account:
    acct = make_account(user)
    db_session.add(acct)
    db_session.flush()
    return acct


def _charges(
    db_session: Session,
    user: User,
    account: Account,
    last: date,
    gaps: list[int],
    amounts: list[str] | None = None,
    description: str = "NETFLIX.COM 8776",
    **kwargs: object,
) -> list[Transaction]:
    """Charges ending on `last`, `gaps` days apart going back (so len(gaps) + 1 rows)."""
    days = [last]
    for gap in gaps:
        days.insert(0, days[0] - timedelta(days=gap))
    amounts = amounts or ["-15.49"] * len(days)
    rows = [
        make_transaction(
            user,
            account,
            posted_date=day,
            amount=Decimal(amount),
            description_raw=description,
            **kwargs,
        )
        for day, amount in zip(days, amounts, strict=True)
    ]
    db_session.add_all(rows)
    db_session.flush()
    return rows


def _sub(db_session: Session, user: User, **kwargs: object) -> Subscription:
    sub = make_subscription(user, created_at=datetime(2026, 1, 1, tzinfo=UTC), **kwargs)
    db_session.add(sub)
    db_session.flush()
    return sub


def _flags(db_session: Session, user: User, sub: Subscription):
    return subscription_flags(db_session, user.id, [sub], NOW)[sub.id]


# --- suggestions (AC-1) ------------------------------------------------------------------


def test_three_monthly_charges_are_suggested(
    db_session: Session, user: User, account: Account
) -> None:
    rows = _charges(db_session, user, account, date(2026, 10, 5), [30, 31])

    [suggestion] = suggest_subscriptions(db_session, user.id, NOW)

    assert suggestion.name == "NETFLIX.COM 8776"
    assert suggestion.amount == Decimal("-15.49")
    assert suggestion.cadence == Cadence.monthly
    assert suggestion.next_due == date(2026, 11, 5)
    assert suggestion.charge_count == 3
    assert suggestion.transaction_ids == [row.id for row in rows]


def test_the_merchant_field_names_the_suggestion(
    db_session: Session, user: User, account: Account
) -> None:
    _charges(db_session, user, account, date(2026, 10, 5), [28, 29], merchant="Netflix")

    [suggestion] = suggest_subscriptions(db_session, user.id, NOW)

    assert suggestion.name == "Netflix"


def test_two_charges_are_not_enough(db_session: Session, user: User, account: Account) -> None:
    _charges(db_session, user, account, date(2026, 10, 5), [30])

    assert suggest_subscriptions(db_session, user.id, NOW) == []


@pytest.mark.parametrize("gap", [27, 32])
def test_a_gap_outside_28_to_31_days_is_rejected(
    db_session: Session, user: User, account: Account, gap: int
) -> None:
    _charges(db_session, user, account, date(2026, 10, 5), [30, gap])

    assert suggest_subscriptions(db_session, user.id, NOW) == []


def test_amount_drift_beyond_ten_percent_is_rejected(
    db_session: Session, user: User, account: Account
) -> None:
    _charges(db_session, user, account, date(2026, 10, 5), [30, 30], ["-10.00", "-10.00", "-12.00"])

    assert suggest_subscriptions(db_session, user.id, NOW) == []


def test_amounts_within_ten_percent_are_accepted(
    db_session: Session, user: User, account: Account
) -> None:
    _charges(db_session, user, account, date(2026, 10, 5), [30, 30], ["-10.00", "-10.50", "-9.60"])

    [suggestion] = suggest_subscriptions(db_session, user.id, NOW)

    assert suggestion.amount == Decimal("-9.60")


def test_charges_linked_to_a_subscription_are_not_suggested(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, name="Something else")
    _charges(db_session, user, account, date(2026, 10, 5), [30, 30], subscription_id=sub.id)

    assert suggest_subscriptions(db_session, user.id, NOW) == []


def test_a_merchant_covered_by_an_active_subscription_is_excluded(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, name="netflix")
    _charges(db_session, user, account, date(2026, 10, 5), [30, 30])

    assert suggest_subscriptions(db_session, user.id, NOW) == []


def test_an_inactive_subscription_does_not_cover_a_merchant(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, name="netflix", active=False)
    _charges(db_session, user, account, date(2026, 10, 5), [30, 30])

    assert len(suggest_subscriptions(db_session, user.id, NOW)) == 1


def test_a_subscription_name_must_match_on_a_word_boundary(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, name="net")
    _charges(db_session, user, account, date(2026, 10, 5), [30, 30])

    assert len(suggest_subscriptions(db_session, user.id, NOW)) == 1


def test_other_users_charges_never_produce_suggestions(
    db_session: Session, user: User, other_user: User
) -> None:
    other_account = make_account(other_user)
    db_session.add(other_account)
    db_session.flush()
    _charges(db_session, other_user, other_account, date(2026, 10, 5), [30, 30])

    assert suggest_subscriptions(db_session, user.id, NOW) == []


# --- suggestions over HTTP (AC-3, AC-4) --------------------------------------------------


def test_suggestions_require_auth(db_client: TestClient) -> None:
    assert db_client.get(f"{SUBSCRIPTIONS}/suggestions").status_code == 401


def test_accepting_a_suggestion_through_post_makes_it_disappear(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
) -> None:
    today = date.today()
    _charges(db_session, user, account, today - timedelta(days=3), [30, 30], merchant="Netflix")

    [suggestion] = db_client.get(f"{SUBSCRIPTIONS}/suggestions", headers=auth_headers).json()
    created = db_client.post(
        SUBSCRIPTIONS,
        json={k: suggestion[k] for k in ("name", "amount", "cadence", "next_due")},
        headers=auth_headers,
    )

    assert created.status_code == 201, created.text
    assert db_client.get(f"{SUBSCRIPTIONS}/suggestions", headers=auth_headers).json() == []


# --- flags (AC-2, AC-3) ------------------------------------------------------------------


def test_a_healthy_subscription_has_no_flags(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, amount=Decimal("-15.49"), next_due=date(2026, 11, 5))
    _charges(db_session, user, account, date(2026, 10, 5), [30], subscription_id=sub.id)

    flags = _flags(db_session, user, sub)

    assert flags.price_change is None
    assert flags.missed is False
    assert flags.dormant is False


def test_price_change_reports_old_and_new_amounts(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, amount=Decimal("-15.49"), next_due=date(2026, 11, 5))
    _charges(
        db_session,
        user,
        account,
        date(2026, 10, 5),
        [30],
        ["-15.49", "-17.99"],
        subscription_id=sub.id,
    )

    flags = _flags(db_session, user, sub)

    assert flags.price_change is not None
    assert (flags.price_change.old, flags.price_change.new) == (
        Decimal("-15.49"),
        Decimal("-17.99"),
    )


def test_a_change_within_one_percent_is_not_a_price_change(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, amount=Decimal("-100.00"), next_due=date(2026, 11, 5))
    _charges(db_session, user, account, date(2026, 10, 5), [], ["-100.99"], subscription_id=sub.id)

    assert _flags(db_session, user, sub).price_change is None


def test_missed_when_next_due_is_over_a_week_past_with_no_charge_since(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=TODAY - timedelta(days=8))
    _charges(db_session, user, account, date(2026, 9, 10), [], subscription_id=sub.id)

    flags = _flags(db_session, user, sub)

    assert flags.missed is True
    assert flags.dormant is False


def test_not_missed_within_the_seven_day_grace(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=TODAY - timedelta(days=7))
    _charges(db_session, user, account, date(2026, 9, 10), [], subscription_id=sub.id)

    assert _flags(db_session, user, sub).missed is False


def test_not_missed_when_a_charge_arrived_since_the_due_date(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=TODAY - timedelta(days=10))
    _charges(db_session, user, account, TODAY - timedelta(days=9), [], subscription_id=sub.id)

    assert _flags(db_session, user, sub).missed is False


def test_dormant_with_no_charge_in_two_cycles(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=date(2026, 11, 5))
    _charges(db_session, user, account, date(2026, 8, 10), [], subscription_id=sub.id)

    assert _flags(db_session, user, sub).dormant is True


def test_a_charge_inside_two_cycles_is_not_dormant(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=date(2026, 11, 5))
    _charges(db_session, user, account, date(2026, 8, 16), [], subscription_id=sub.id)

    assert _flags(db_session, user, sub).dormant is False


def test_a_never_charged_old_subscription_is_dormant(db_session: Session, user: User) -> None:
    sub = _sub(db_session, user)

    assert _flags(db_session, user, sub).dormant is True


def test_an_inactive_subscription_is_never_flagged(db_session: Session, user: User) -> None:
    sub = _sub(db_session, user, active=False, next_due=date(2026, 1, 1))

    flags = _flags(db_session, user, sub)

    assert (flags.price_change, flags.missed, flags.dormant) == (None, False, False)


def test_other_users_charges_never_produce_flags(
    db_session: Session, user: User, other_user: User
) -> None:
    sub = _sub(db_session, user, amount=Decimal("-15.49"), next_due=date(2026, 11, 5))
    other_account = make_account(other_user)
    db_session.add(other_account)
    db_session.flush()
    _charges(
        db_session,
        other_user,
        other_account,
        date(2026, 10, 5),
        [],
        ["-99.00"],
        subscription_id=sub.id,
    )

    flags = _flags(db_session, user, sub)

    assert flags.price_change is None


def test_flags_use_a_fixed_number_of_queries(
    db_session: Session, user: User, account: Account
) -> None:
    subs = [_sub(db_session, user, name=f"sub {i}", next_due=date(2026, 11, 5)) for i in range(6)]
    for sub in subs:
        _charges(db_session, user, account, date(2026, 10, 5), [], subscription_id=sub.id)
    statements: list[str] = []
    bind = db_session.get_bind()

    def record(conn, cursor, statement, *rest):  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", record)
    try:
        subscription_flags(db_session, user.id, subs, NOW)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert len(statements) == 1


def test_list_carries_flags(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
) -> None:
    today = date.today()
    sub = _sub(db_session, user, amount=Decimal("-15.49"), next_due=today + timedelta(days=20))
    _charges(
        db_session,
        user,
        account,
        today - timedelta(days=10),
        [],
        ["-17.99"],
        subscription_id=sub.id,
    )

    [item] = db_client.get(SUBSCRIPTIONS, headers=auth_headers).json()["items"]

    assert item["flags"] == {
        "price_change": {"old": "-15.49", "new": "-17.99"},
        "missed": False,
        "dormant": False,
    }


# --- review follow-ups (PR #142) ---------------------------------------------------------


def test_an_unrelated_charge_at_the_same_merchant_does_not_hide_a_series(
    db_session: Session, user: User, account: Account
) -> None:
    rows = _charges(db_session, user, account, date(2026, 10, 5), [30, 31])
    _charges(db_session, user, account, date(2026, 10, 10), [], ["-42.00"])

    [suggestion] = suggest_subscriptions(db_session, user.id, NOW)

    assert suggestion.charge_count == 3
    assert suggestion.transaction_ids == [row.id for row in rows]


def test_two_plans_at_one_merchant_are_suggested_separately(
    db_session: Session, user: User, account: Account
) -> None:
    _charges(db_session, user, account, date(2026, 10, 5), [30, 31])
    _charges(db_session, user, account, date(2026, 10, 9), [30, 30], ["-42.00"] * 3)

    suggestions = suggest_subscriptions(db_session, user.id, NOW)

    assert sorted(s.amount for s in suggestions) == [Decimal("-42.00"), Decimal("-15.49")]


def test_a_series_that_stopped_over_a_week_before_its_next_due_is_not_suggested(
    db_session: Session, user: User, account: Account
) -> None:
    _charges(db_session, user, account, date(2025, 12, 5), [30, 31])

    assert suggest_subscriptions(db_session, user.id, NOW) == []


def test_a_series_within_the_grace_of_its_next_due_is_still_suggested(
    db_session: Session, user: User, account: Account
) -> None:
    _charges(db_session, user, account, date(2026, 9, 9), [30, 31])  # next_due 2026-10-09

    assert len(suggest_subscriptions(db_session, user.id, NOW)) == 1


def test_per_charge_tokens_in_the_description_do_not_split_a_merchant(
    db_session: Session, user: User, account: Account
) -> None:
    descriptions = ["NETFLIX.COM 12345", "NETFLIX.COM #REF9 67890", "NETFLIX.COM 10/05 24680"]
    rows = [
        make_transaction(
            user,
            account,
            posted_date=day,
            amount=Decimal("-15.49"),
            description_raw=description,
        )
        for day, description in zip(
            [date(2026, 8, 5), date(2026, 9, 5), date(2026, 10, 5)], descriptions, strict=True
        )
    ]
    db_session.add_all(rows)
    db_session.flush()

    [suggestion] = suggest_subscriptions(db_session, user.id, NOW)

    assert suggestion.charge_count == 3


def test_patching_a_dormant_subscription_returns_computed_flags(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
) -> None:
    sub = _sub(db_session, user, next_due=None)

    response = db_client.patch(
        f"{SUBSCRIPTIONS}/{sub.id}", json={"name": "Renamed"}, headers=auth_headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["flags"]["dormant"] is True
