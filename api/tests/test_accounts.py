import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import Account, BalanceSnapshot, User
from app.models.enums import AccountKind, Bucket, InsightKind
from app.services.accounts import (
    CardLogTotals,
    _classify,
    get_interest_summary,
    get_payoff_plan,
    is_interest_or_fee,
    latest_balances,
    list_cards,
    list_loans,
)
from app.services.insights import detect_insights
from tests.factories import make_snapshot, make_transaction

ACCOUNTS = "/api/accounts"


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"name": "Everyday Checking", "kind": "checking"}
    body.update(overrides)
    return body


def _account(user: User, **kwargs: object) -> Account:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Everyday Checking",
        "kind": AccountKind.checking,
    }
    defaults.update(kwargs)
    return Account(**defaults)


def test_list_returns_only_own_accounts(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    mine = _account(user, name="Zeta Checking")
    also_mine = _account(user, name="Alpha Savings", kind=AccountKind.cash)
    theirs = _account(other, name="Their Checking")
    db_session.add_all([mine, also_mine, theirs])
    db_session.flush()

    r = db_client.get(ACCOUNTS, headers=auth_headers)

    assert r.status_code == 200
    names = [row["name"] for row in r.json()]
    assert names == ["Alpha Savings", "Zeta Checking"]


def test_list_filters_by_active(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    active = _account(user, name="Active One", active=True)
    inactive = _account(user, name="Closed One", active=False)
    db_session.add_all([active, inactive])
    db_session.flush()

    only_active = db_client.get(ACCOUNTS, params={"active": "true"}, headers=auth_headers)
    only_inactive = db_client.get(ACCOUNTS, params={"active": "false"}, headers=auth_headers)

    assert [row["name"] for row in only_active.json()] == ["Active One"]
    assert [row["name"] for row in only_inactive.json()] == ["Closed One"]


def test_create_returns_201_with_the_created_account(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        ACCOUNTS,
        json=_body(
            kind="credit_card",
            institution="Chase",
            last4="1234",
            due_day=15,
            planned_payment="100.00",
            apr="24.99",
            promo_apr_end="2027-01-01",
        ),
        headers=auth_headers,
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Everyday Checking"
    assert body["kind"] == "credit_card"
    assert body["institution"] == "Chase"
    assert body["last4"] == "1234"
    assert body["due_day"] == 15
    assert body["planned_payment"] == "100.00"
    assert body["apr"] == "24.99"
    assert body["promo_apr_end"] == "2027-01-01"
    assert body["active"] is True
    assert body["simplefin_account_id"] is None
    assert "user_id" not in body


def test_create_is_idempotent_on_a_repeated_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account_id = str(uuid.uuid4())
    first = db_client.post(ACCOUNTS, json=_body(id=account_id), headers=auth_headers)
    second = db_client.post(
        ACCOUNTS, json=_body(id=account_id, name="A Totally Different Name"), headers=auth_headers
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["name"] == "Everyday Checking"
    assert len(db_session.scalars(select(Account)).all()) == 1


def test_create_with_another_users_id_is_a_conflict(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = _account(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.post(ACCOUNTS, json=_body(id=str(theirs.id)), headers=auth_headers)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_patch_updates_only_given_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = _account(user, institution="Chase", last4="1234", due_day=10)
    db_session.add(account)
    db_session.flush()

    r = db_client.patch(f"{ACCOUNTS}/{account.id}", json={"due_day": 20}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert body["due_day"] == 20
    assert body["institution"] == "Chase"
    assert body["last4"] == "1234"
    assert body["name"] == "Everyday Checking"


def test_patch_on_missing_id_is_404(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    r = db_client.patch(f"{ACCOUNTS}/{uuid.uuid4()}", json={"due_day": 5}, headers=auth_headers)
    assert r.status_code == 404


def test_patch_on_another_users_id_is_404(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = _account(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.patch(f"{ACCOUNTS}/{theirs.id}", json={"due_day": 5}, headers=auth_headers)

    assert r.status_code == 404


def test_decimal_amounts_round_trip_exactly(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(ACCOUNTS, json=_body(planned_payment="-1234.56"), headers=auth_headers)
    assert r.status_code == 201, r.text
    assert r.json()["planned_payment"] == "-1234.56"


VALIDATION_CASES = [
    {"due_day": 0},
    {"due_day": 32},
    {"apr": "100.01"},
    {"apr": "-1"},
    {"last4": "12a4"},
    {"last4": "123"},
    {"name": "x" * 101},
    {"name": ""},
    {"kind": "savings"},
]
VALIDATION_IDS = [
    "due-day-zero",
    "due-day-32",
    "apr-over-100",
    "apr-negative",
    "last4-not-digits",
    "last4-too-short",
    "name-too-long",
    "name-empty",
    "unknown-kind",
]


@pytest.mark.parametrize("overrides", VALIDATION_CASES, ids=VALIDATION_IDS)
def test_create_validation_rejects_bad_input(
    db_client: TestClient, user: User, auth_headers: dict[str, str], overrides: dict[str, Any]
) -> None:
    r = db_client.post(ACCOUNTS, json=_body(**overrides), headers=auth_headers)
    assert r.status_code == 422


@pytest.mark.parametrize("overrides", VALIDATION_CASES, ids=VALIDATION_IDS)
def test_patch_validation_rejects_bad_input(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    overrides: dict[str, Any],
) -> None:
    account = _account(user)
    db_session.add(account)
    db_session.flush()

    r = db_client.patch(f"{ACCOUNTS}/{account.id}", json=overrides, headers=auth_headers)

    assert r.status_code == 422


@pytest.mark.parametrize("field", ["name", "kind", "active"])
def test_patch_null_on_required_field_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    account = _account(user)
    db_session.add(account)
    db_session.flush()

    r = db_client.patch(f"{ACCOUNTS}/{account.id}", json={field: None}, headers=auth_headers)

    assert r.status_code == 422


@pytest.mark.parametrize("field", ["institution", "last4", "due_day", "planned_payment", "apr"])
def test_patch_null_on_nullable_field_clears_it(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    account = _account(
        user, institution="Chase", last4="1234", due_day=10, planned_payment="50.00", apr="10.00"
    )
    db_session.add(account)
    db_session.flush()

    r = db_client.patch(f"{ACCOUNTS}/{account.id}", json={field: None}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()[field] is None


def test_create_rejects_unknown_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(ACCOUNTS, json=_body(simplefin_account_id="sneaky"), headers=auth_headers)
    assert r.status_code == 422


# -- Cards screen (#62) -----------------------------------------------------------


def test_cards_picks_latest_balance_without_a_query_per_card(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    sapphire = _account(user, name="Sapphire", kind=AccountKind.credit_card)
    freedom = _account(user, name="Freedom", kind=AccountKind.credit_card)
    no_history = _account(user, name="NoHistory", kind=AccountKind.credit_card)
    db_session.add_all([sapphire, freedom, no_history])
    db_session.flush()
    db_session.add_all(
        [
            make_snapshot(user, sapphire, date=date(2026, 8, 1), balance=Decimal("-200.00")),
            make_snapshot(user, sapphire, date=date(2026, 10, 1), balance=Decimal("-150.25")),
            make_snapshot(user, sapphire, date=date(2026, 9, 1), balance=Decimal("-180.00")),
            make_snapshot(user, freedom, date=date(2026, 9, 15), balance=Decimal("-300.00")),
            make_snapshot(user, freedom, date=date(2026, 9, 20), balance=Decimal("-350.00")),
        ]
    )
    db_session.flush()

    statements: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    conn = db_session.connection()
    event.listen(conn, "before_cursor_execute", _capture)
    try:
        r = db_client.get(f"{ACCOUNTS}/cards", headers=auth_headers)
    finally:
        event.remove(conn, "before_cursor_execute", _capture)

    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {row["account"]["id"]: row for row in body}
    assert len(body) == 3

    assert by_id[str(sapphire.id)]["balance"] == "-150.25"
    assert by_id[str(sapphire.id)]["balance_date"] == "2026-10-01"
    assert by_id[str(sapphire.id)]["source_hint"] == "manual"

    assert by_id[str(freedom.id)]["balance"] == "-350.00"
    assert by_id[str(freedom.id)]["balance_date"] == "2026-09-20"

    assert by_id[str(no_history.id)]["balance"] is None
    assert by_id[str(no_history.id)]["balance_date"] is None

    snapshot_queries = [s for s in statements if "balance_snapshots" in s]
    # One for the latest balances, one for the interest check's prior-month balances;
    # neither grows with the number of cards.
    assert len(snapshot_queries) == 2, snapshot_queries


def test_cards_excludes_non_card_accounts(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    checking = _account(user, name="Everyday Checking", kind=AccountKind.checking)
    card = _account(user, name="Freedom", kind=AccountKind.credit_card)
    db_session.add_all([checking, card])
    db_session.flush()

    r = db_client.get(f"{ACCOUNTS}/cards", headers=auth_headers)

    assert r.status_code == 200
    names = [row["account"]["name"] for row in r.json()]
    assert names == ["Freedom"]


def test_cards_hides_inactive_by_default(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    active = _account(user, name="Active Card", kind=AccountKind.credit_card, active=True)
    closed = _account(user, name="Closed Card", kind=AccountKind.credit_card, active=False)
    db_session.add_all([active, closed])
    db_session.flush()

    default = db_client.get(f"{ACCOUNTS}/cards", headers=auth_headers)
    with_inactive = db_client.get(
        f"{ACCOUNTS}/cards", params={"include_inactive": "true"}, headers=auth_headers
    )

    assert [row["account"]["name"] for row in default.json()] == ["Active Card"]
    assert sorted(row["account"]["name"] for row in with_inactive.json()) == [
        "Active Card",
        "Closed Card",
    ]


def test_cards_with_no_snapshot_has_null_balance(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _account(user, name="Freedom", kind=AccountKind.credit_card)
    db_session.add(card)
    db_session.flush()

    r = db_client.get(f"{ACCOUNTS}/cards", headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert body[0]["balance"] is None
    assert body[0]["balance_date"] is None


def test_cards_source_hint_is_synced_for_simplefin_accounts(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _account(
        user, name="Chase Card", kind=AccountKind.credit_card, simplefin_account_id="ACT-1"
    )
    db_session.add(card)
    db_session.flush()

    r = db_client.get(f"{ACCOUNTS}/cards", headers=auth_headers)

    assert r.json()[0]["source_hint"] == "synced"


def test_cards_route_is_not_captured_by_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """`GET /cards` must resolve to the cards list, not be swallowed by a `/{id}...`
    pattern that would try (and fail) to parse "cards" as a UUID, and a real id must
    still resolve its own `/{id}/balances` route rather than the literal `/cards` one.
    """
    card = _account(user, name="Freedom", kind=AccountKind.credit_card)
    db_session.add(card)
    db_session.flush()

    cards = db_client.get(f"{ACCOUNTS}/cards", headers=auth_headers)
    history = db_client.get(f"{ACCOUNTS}/{card.id}/balances", headers=auth_headers)

    assert cards.status_code == 200
    assert isinstance(cards.json(), list)
    assert history.status_code == 200
    assert history.json() == []


# -- Manual balances (#62) ---------------------------------------------------------


def _card(user: User, **kwargs: object) -> Account:
    defaults: dict[str, object] = {"kind": AccountKind.credit_card}
    defaults.update(kwargs)
    return _account(user, **defaults)


def test_put_balance_creates_then_updates_same_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user)
    db_session.add(card)
    db_session.flush()

    created = db_client.put(
        f"{ACCOUNTS}/{card.id}/balances/2026-10-01",
        json={"balance": "-500.00"},
        headers=auth_headers,
    )
    updated = db_client.put(
        f"{ACCOUNTS}/{card.id}/balances/2026-10-01",
        json={"balance": "-525.50"},
        headers=auth_headers,
    )

    assert created.status_code == 200, created.text
    assert created.json()["balance"] == "-500.00"
    assert updated.status_code == 200
    assert updated.json()["balance"] == "-525.50"
    assert updated.json()["id"] == created.json()["id"]

    rows = db_session.scalars(select(BalanceSnapshot).where(BalanceSnapshot.account_id == card.id))
    assert len(list(rows)) == 1


def test_put_balance_on_synced_account_is_409(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user, simplefin_account_id="ACT-1")
    db_session.add(card)
    db_session.flush()

    r = db_client.put(
        f"{ACCOUNTS}/{card.id}/balances/2026-10-01",
        json={"balance": "-500.00"},
        headers=auth_headers,
    )

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_put_balance_on_non_card_account_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    checking = _account(user, kind=AccountKind.checking)
    db_session.add(checking)
    db_session.flush()

    r = db_client.put(
        f"{ACCOUNTS}/{checking.id}/balances/2026-10-01",
        json={"balance": "500.00"},
        headers=auth_headers,
    )

    assert r.status_code == 422


def test_put_balance_on_another_users_account_is_404(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = _card(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.put(
        f"{ACCOUNTS}/{theirs.id}/balances/2026-10-01",
        json={"balance": "-500.00"},
        headers=auth_headers,
    )

    assert r.status_code == 404


def test_put_balance_rejects_dates_out_of_range(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user)
    db_session.add(card)
    db_session.flush()

    too_early = db_client.put(
        f"{ACCOUNTS}/{card.id}/balances/1999-12-31",
        json={"balance": "-500.00"},
        headers=auth_headers,
    )
    in_the_future = db_client.put(
        f"{ACCOUNTS}/{card.id}/balances/2999-01-01",
        json={"balance": "-500.00"},
        headers=auth_headers,
    )

    assert too_early.status_code == 422
    assert in_the_future.status_code == 422


def test_put_balance_preserves_decimal_sign(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user)
    db_session.add(card)
    db_session.flush()

    r = db_client.put(
        f"{ACCOUNTS}/{card.id}/balances/2026-10-01",
        json={"balance": "-1234.56"},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["balance"] == "-1234.56"


def test_balance_history_orders_newest_first_and_filters_by_range(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user)
    db_session.add(card)
    db_session.flush()
    db_session.add_all(
        [
            make_snapshot(user, card, date=date(2026, 8, 1), balance=Decimal("-100.00")),
            make_snapshot(user, card, date=date(2026, 9, 1), balance=Decimal("-120.00")),
            make_snapshot(user, card, date=date(2026, 10, 1), balance=Decimal("-140.00")),
        ]
    )
    db_session.flush()

    full = db_client.get(f"{ACCOUNTS}/{card.id}/balances", headers=auth_headers)
    ranged = db_client.get(
        f"{ACCOUNTS}/{card.id}/balances",
        params={"from": "2026-09-01", "to": "2026-09-30"},
        headers=auth_headers,
    )

    assert full.status_code == 200
    assert [row["date"] for row in full.json()] == ["2026-10-01", "2026-09-01", "2026-08-01"]
    assert ranged.status_code == 200
    assert [row["date"] for row in ranged.json()] == ["2026-09-01"]


def test_balance_history_is_bounded_to_400_rows(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user)
    db_session.add(card)
    db_session.flush()
    db_session.add_all(
        [make_snapshot(user, card, date=date(2020, 1, 1) + timedelta(days=i)) for i in range(401)]
    )
    db_session.flush()

    r = db_client.get(f"{ACCOUNTS}/{card.id}/balances", headers=auth_headers)

    assert r.status_code == 200
    assert len(r.json()) == 400


def test_balance_history_on_another_users_account_is_404(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = _card(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.get(f"{ACCOUNTS}/{theirs.id}/balances", headers=auth_headers)

    assert r.status_code == 404


def test_latest_balances_on_or_before_bounds_the_snapshot(db_session: Session, user: User) -> None:
    card = _account(user, name="Freedom", kind=AccountKind.credit_card)
    db_session.add(card)
    db_session.flush()
    db_session.add_all(
        [
            make_snapshot(user, card, date=date(2026, 9, 1), balance=Decimal("-180.00")),
            make_snapshot(user, card, date=date(2026, 10, 1), balance=Decimal("-150.25")),
        ]
    )
    db_session.flush()

    result = latest_balances(db_session, user.id, [card.id], on_or_before=date(2026, 9, 30))

    assert result[card.id].balance == Decimal("-180.00")
    assert result[card.id].date == date(2026, 9, 1)


# -- Monthly card log (#63) ---------------------------------------------------------


def test_card_log_reconciles_a_full_month(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user, name="Freedom")
    db_session.add(card)
    db_session.flush()
    db_session.add_all(
        [
            make_snapshot(user, card, date=date(2026, 9, 30), balance=Decimal("-500.00")),
            make_snapshot(user, card, date=date(2026, 10, 31), balance=Decimal("-415.25")),
            make_transaction(
                user,
                card,
                posted_date=date(2026, 10, 5),
                amount=Decimal("-50.00"),
                description_raw="HERTZ RENTAL CAR",
            ),
            make_transaction(
                user,
                card,
                posted_date=date(2026, 10, 10),
                amount=Decimal("-75.25"),
                description_raw="WHOLE FOODS",
            ),
            make_transaction(
                user,
                card,
                posted_date=date(2026, 10, 15),
                amount=Decimal("200.00"),
                description_raw="PAYMENT - THANK YOU",
                bucket=Bucket.transfer,
            ),
            make_transaction(
                user,
                card,
                posted_date=date(2026, 10, 20),
                amount=Decimal("-15.00"),
                description_raw="INTEREST CHARGE",
            ),
            make_transaction(
                user,
                card,
                posted_date=date(2026, 10, 25),
                amount=Decimal("25.00"),
                description_raw="REFUND - AMAZON",
            ),
        ]
    )
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{card.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["month"] == "2026-10"
    assert body["starting_balance"] == "-500.00"
    assert body["starting_balance_date"] == "2026-09-30"
    assert body["new_charges"] == "-125.25"
    assert body["payments"] == "200.00"
    assert body["interest_and_fees"] == "-15.00"
    assert body["other"] == "25.00"
    assert body["ending_balance"] == "-415.25"
    assert body["ending_balance_date"] == "2026-10-31"
    assert body["computed_ending"] == "-415.25"
    assert body["difference"] == "0.00"


def test_card_log_with_missing_snapshot_gives_nulls(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user, name="Freedom")
    db_session.add(card)
    db_session.flush()
    db_session.add(
        make_transaction(user, card, posted_date=date(2026, 10, 5), amount=Decimal("-50.00"))
    )
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{card.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["starting_balance"] is None
    assert body["starting_balance_date"] is None
    assert body["ending_balance"] is None
    assert body["ending_balance_date"] is None
    assert body["computed_ending"] is None
    assert body["difference"] is None
    assert body["new_charges"] == "-50.00"


def test_card_log_with_only_a_fresh_ending_snapshot_leaves_starting_and_difference_null(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """UB-4: the half of the missing-snapshot space the other test can't cover --
    an ending snapshot exists (dated inside the month) but nothing qualifies as a
    starting one. `computed_ending`/`difference` need `starting_balance`, so both
    stay `None` even though `ending_balance` is known.

    The inverse (`starting_balance` known, `ending_balance` genuinely `None`) can't
    happen given how `_month_end_balances` bounds its two lookups: any snapshot
    that qualifies as `on_or_before=previous_month_end` also qualifies as
    `on_or_before=this_month_end`, since the previous month's end is always before
    this month's. A stale (not missing) ending snapshot is covered by
    `test_card_log_with_a_stale_ending_snapshot_leaves_difference_null` instead.
    """
    card = _card(user, name="Freedom")
    db_session.add(card)
    db_session.flush()
    db_session.add(make_snapshot(user, card, date=date(2026, 10, 20), balance=Decimal("-50.00")))
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{card.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["starting_balance"] is None
    assert body["starting_balance_date"] is None
    assert body["ending_balance"] == "-50.00"
    assert body["ending_balance_date"] == "2026-10-20"
    assert body["computed_ending"] is None
    assert body["difference"] is None


def test_card_log_with_a_stale_ending_snapshot_leaves_difference_null(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """#63 review UB-5/MV-2: the only snapshot on file is from September, so
    `_month_end_balances` reuses it for both the starting and the (stale) ending
    lookup when October is requested. The dates expose the staleness; `difference`
    stays `None` instead of reporting a false gap equal to the month's activity.
    """
    card = _card(user, name="Freedom")
    db_session.add(card)
    db_session.flush()
    db_session.add(make_snapshot(user, card, date=date(2026, 9, 15), balance=Decimal("-300.00")))
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{card.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["starting_balance"] == "-300.00"
    assert body["starting_balance_date"] == "2026-09-15"
    assert body["ending_balance"] == "-300.00"
    assert body["ending_balance_date"] == "2026-09-15"
    assert body["computed_ending"] == "-300.00"
    assert body["difference"] is None


def test_card_log_excludes_split_parent(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user, name="Freedom")
    db_session.add(card)
    db_session.flush()
    parent = make_transaction(user, card, posted_date=date(2026, 10, 5), amount=Decimal("-100.00"))
    db_session.add(parent)
    db_session.flush()
    db_session.add(
        make_transaction(
            user, card, posted_date=date(2026, 10, 5), amount=Decimal("-40.00"), parent_id=parent.id
        )
    )
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{card.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )

    assert r.status_code == 200
    assert r.json()["new_charges"] == "-40.00"


def test_card_log_excludes_pending(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user, name="Freedom")
    db_session.add(card)
    db_session.flush()
    db_session.add_all(
        [
            make_transaction(user, card, posted_date=date(2026, 10, 5), amount=Decimal("-50.00")),
            make_transaction(
                user, card, posted_date=date(2026, 10, 6), amount=Decimal("-999.00"), pending=True
            ),
        ]
    )
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{card.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )

    assert r.status_code == 200
    assert r.json()["new_charges"] == "-50.00"


def test_card_log_month_boundaries_including_january_rollover(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(user, name="Freedom")
    db_session.add(card)
    db_session.flush()
    db_session.add_all(
        [
            make_snapshot(user, card, date=date(2025, 12, 31), balance=Decimal("-100.00")),
            make_snapshot(user, card, date=date(2026, 1, 31), balance=Decimal("-160.00")),
            # Just outside the window on either side.
            make_transaction(
                user, card, posted_date=date(2025, 12, 31), amount=Decimal("-1000.00")
            ),
            make_transaction(user, card, posted_date=date(2026, 2, 1), amount=Decimal("-2000.00")),
            # Inside the window, at both edges.
            make_transaction(user, card, posted_date=date(2026, 1, 1), amount=Decimal("-30.00")),
            make_transaction(user, card, posted_date=date(2026, 1, 31), amount=Decimal("-30.00")),
        ]
    )
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{card.id}/card-log", params={"month": "2026-01"}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["starting_balance"] == "-100.00"
    assert body["starting_balance_date"] == "2025-12-31"
    assert body["ending_balance"] == "-160.00"
    assert body["ending_balance_date"] == "2026-01-31"
    assert body["new_charges"] == "-60.00"
    assert body["computed_ending"] == "-160.00"
    assert body["difference"] == "0.00"


def test_card_log_on_non_card_account_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    checking = _account(user, kind=AccountKind.checking)
    db_session.add(checking)
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{checking.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )

    assert r.status_code == 422


def test_card_log_on_another_users_account_is_404(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = _card(other, name="Their Card")
    db_session.add(theirs)
    db_session.flush()

    r = db_client.get(
        f"{ACCOUNTS}/{theirs.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )

    assert r.status_code == 404


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        # Positives: real fee/interest lines, including the issue's examples.
        ("INTEREST CHARGE", True),
        ("interest charge", True),
        ("LATE FEE", True),
        ("ANNUAL FEE", True),
        ("FINANCE CHARGE", True),
        ("  finance   charge  ", True),
        ("FOREIGN TRANSACTION FEE", True),
        ("LATE PAYMENT FEE", True),
        ("INTEREST CHARGED ON PURCHASES", True),
        # Negatives: ordinary purchases. #63 review UB-1/MV-1 -- a bare "FEE"
        # substring match wrongly fired on all of these.
        ("HERTZ RENTAL CAR", False),
        ("WHOLE FOODS", False),
        ("STARBUCKS COFFEE #123", False),
        ("DUNKIN COFFEE", False),
        ("PETCO FEED STORE", False),
        ("TOFFEE SHOP", False),
        ("FEEDBACK LLC", False),
    ],
)
def test_is_interest_or_fee_keyword_heuristic(description: str, expected: bool) -> None:
    assert is_interest_or_fee(description) is expected


def test_classify_is_a_pure_function_over_rows() -> None:
    """#63 review UB-2: the classification rules, unit-tested without a session."""
    totals = _classify(
        [
            (Decimal("-50.00"), Bucket.personal, "HERTZ RENTAL CAR"),
            (Decimal("-75.25"), Bucket.personal, "WHOLE FOODS"),
            (Decimal("200.00"), Bucket.transfer, "PAYMENT - THANK YOU"),
            (Decimal("-15.00"), Bucket.personal, "INTEREST CHARGE"),
            (Decimal("25.00"), Bucket.personal, "REFUND - AMAZON"),
        ]
    )

    assert totals == CardLogTotals(
        new_charges=Decimal("-125.25"),
        payments=Decimal("200.00"),
        interest_and_fees=Decimal("-15.00"),
        other=Decimal("25.00"),
    )
    assert totals.total == Decimal("84.75")


# -- Loans (#255) -------------------------------------------------------------------


def test_patch_sets_kind_to_loan_and_is_user_scoped(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    mine = _account(user, name="Car", kind=AccountKind.credit_card)
    theirs = _account(other_user, name="Theirs", kind=AccountKind.credit_card)
    db_session.add_all([mine, theirs])
    db_session.flush()

    r = db_client.patch(f"{ACCOUNTS}/{mine.id}", json={"kind": "loan"}, headers=auth_headers)
    other = db_client.patch(f"{ACCOUNTS}/{theirs.id}", json={"kind": "loan"}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()["kind"] == "loan"
    assert other.status_code == 404
    db_session.refresh(theirs)
    assert theirs.kind == AccountKind.credit_card


def test_loans_endpoint_lists_active_loans_with_a_payoff(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    loan = _account(
        user, name="Car", kind=AccountKind.loan, apr=Decimal("5.00"), planned_payment=Decimal(500)
    )
    card = _account(user, name="Freedom", kind=AccountKind.credit_card)
    closed = _account(user, name="Old loan", kind=AccountKind.loan, active=False)
    theirs = _account(other_user, name="Theirs", kind=AccountKind.loan)
    db_session.add_all([loan, card, closed, theirs])
    db_session.flush()
    db_session.add(make_snapshot(user, loan, date=date(2026, 10, 1), balance=Decimal("-10000.00")))
    db_session.add(make_snapshot(user, card, date=date(2026, 10, 1), balance=Decimal("-50.00")))
    db_session.flush()

    r = db_client.get(f"{ACCOUNTS}/loans", headers=auth_headers)

    assert r.status_code == 200
    [row] = r.json()
    assert row["account"]["id"] == str(loan.id)
    assert Decimal(row["owed"]) == Decimal("10000.00")
    assert row["months_to_payoff"] > 20
    assert row["payoff_month"] is not None
    assert Decimal(row["total_interest"]) > 0


@pytest.mark.parametrize("balance", ["-4000.00", "4000.00"])
def test_loan_owed_is_the_absolute_balance_in_either_sign(
    db_session: Session, user: User, balance: str
) -> None:
    loan = _account(user, name="Car", kind=AccountKind.loan, planned_payment=Decimal(500))
    db_session.add(loan)
    db_session.flush()
    db_session.add(make_snapshot(user, loan, date=date(2026, 10, 1), balance=Decimal(balance)))
    db_session.flush()

    [row] = list_loans(db_session, user.id, datetime(2026, 10, 16, 5, 0, tzinfo=UTC))

    assert row.owed == Decimal("4000.00")
    assert row.months_to_payoff == 8


def test_loan_payoff_honors_a_zero_percent_promo(db_session: Session, user: User) -> None:
    now = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)
    interest = []
    for promo_end in (None, date(2027, 10, 1)):
        db_session.query(Account).filter(Account.user_id == user.id).delete()
        loan = _account(
            user,
            name="Car",
            kind=AccountKind.loan,
            apr=Decimal("12.00"),
            planned_payment=Decimal(500),
            promo_apr_end=promo_end,
        )
        db_session.add(loan)
        db_session.flush()
        db_session.add(
            make_snapshot(user, loan, date=date(2026, 10, 1), balance=Decimal("-4000.00"))
        )
        db_session.flush()
        [row] = list_loans(db_session, user.id, now)
        interest.append(row.total_interest)

    assert interest[0] is not None and interest[1] is not None
    assert interest[1] < interest[0]


def test_a_loan_stays_out_of_cards_interest_payoff_and_promo_pace(
    db_session: Session, user: User
) -> None:
    loan = _account(
        user,
        name="Car",
        kind=AccountKind.loan,
        apr=Decimal("5.00"),
        planned_payment=Decimal(100),
        promo_apr_end=date(2027, 4, 1),
        due_day=15,
    )
    db_session.add(loan)
    db_session.flush()
    db_session.add(make_snapshot(user, loan, date=date(2026, 10, 1), balance=Decimal("-5000.00")))
    db_session.flush()
    now = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)

    assert list_cards(db_session, user.id, True) == []
    assert get_interest_summary(db_session, user.id).cards == {}
    assert get_payoff_plan(db_session, user.id, None, now).cards == []
    found = detect_insights(db_session, user.id, now)
    assert [i for i in found if i.kind == InsightKind.promo_pace] == []
