"""Acceptance tests for #44: subscriptions and fixed-bill endpoints.

Account/category rows are built directly in these tests (not through `/api/accounts` or
`/api/categories`, which #31 owns).
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Subscription, User
from app.models.enums import Cadence, SubscriptionKind
from app.services.subscriptions import _PERIODS_PER_YEAR
from tests.factories import make_account, make_category, make_subscription

SUBSCRIPTIONS = "/api/subscriptions"


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Spotify",
        "amount": "-11.99",
        "cadence": "monthly",
    }
    body.update(overrides)
    return body


# --- POST /api/subscriptions -------------------------------------------------------------


def test_create_returns_201_with_the_created_subscription(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(SUBSCRIPTIONS, json=_body(), headers=auth_headers)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Spotify"
    assert body["amount"] == "-11.99"
    assert body["cadence"] == "monthly"
    assert body["kind"] == "subscription"
    assert body["active"] is True
    assert "user_id" not in body


def test_create_is_idempotent_on_a_repeated_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    sub_id = str(uuid.uuid4())
    first = db_client.post(SUBSCRIPTIONS, json=_body(id=sub_id), headers=auth_headers)
    second = db_client.post(
        SUBSCRIPTIONS, json=_body(id=sub_id, name="A Totally Different Name"), headers=auth_headers
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(db_session.scalars(select(Subscription)).all()) == 1


def test_create_with_another_users_id_is_a_conflict(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = make_subscription(other_user)
    _persist(db_session, theirs)

    r = db_client.post(SUBSCRIPTIONS, json=_body(id=str(theirs.id)), headers=auth_headers)

    assert r.status_code == 409


def test_create_with_another_users_account_id_returns_404(
    db_client: TestClient,
    db_session: Session,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)

    r = db_client.post(
        SUBSCRIPTIONS, json=_body(account_id=str(other_account.id)), headers=auth_headers
    )

    assert r.status_code == 404


def test_create_with_another_users_category_id_returns_404(
    db_client: TestClient,
    db_session: Session,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_category = make_category(other_user)
    _persist(db_session, other_category)

    r = db_client.post(
        SUBSCRIPTIONS, json=_body(category_id=str(other_category.id)), headers=auth_headers
    )

    assert r.status_code == 404


@pytest.mark.parametrize("field", ["name", "amount", "cadence"])
def test_create_rejects_null_on_a_required_field(
    db_client: TestClient, user: User, auth_headers: dict[str, str], field: str
) -> None:
    r = db_client.post(SUBSCRIPTIONS, json=_body(**{field: None}), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_a_positive_amount(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(SUBSCRIPTIONS, json=_body(amount="11.99"), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_a_zero_amount(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(SUBSCRIPTIONS, json=_body(amount="0.00"), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_end_date_before_start_date(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        SUBSCRIPTIONS,
        json=_body(start_date="2026-06-01", end_date="2026-01-01"),
        headers=auth_headers,
    )
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [
        {
            "type": "value_error",
            "loc": ["body"],
            "msg": "Value error, end_date must be on or after start_date",
        }
    ]


def test_create_rejects_unknown_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(SUBSCRIPTIONS, json=_body(sneaky="x"), headers=auth_headers)
    assert r.status_code == 422


def test_create_accepts_and_returns_business_pct_and_match_text(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        SUBSCRIPTIONS,
        json=_body(business_pct=40, match_text="APPLE.COM/BILL"),
        headers=auth_headers,
    )

    assert r.status_code == 201, r.text
    assert r.json()["business_pct"] == 40
    assert r.json()["match_text"] == "APPLE.COM/BILL"


def test_business_pct_and_match_text_default_to_null(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    body = db_client.post(SUBSCRIPTIONS, json=_body(), headers=auth_headers).json()

    assert body["business_pct"] is None
    assert body["match_text"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"business_pct": -1},
        {"business_pct": 101},
        {"match_text": ""},
        {"match_text": "x" * 201},
        {"match_text": "   "},
    ],
)
def test_create_rejects_an_invalid_business_pct_or_match_text(
    db_client: TestClient, user: User, auth_headers: dict[str, str], overrides: dict[str, object]
) -> None:
    r = db_client.post(SUBSCRIPTIONS, json=_body(**overrides), headers=auth_headers)
    assert r.status_code == 422


def test_patch_sets_and_clears_business_pct_and_match_text(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    sub = make_subscription(user, business_pct=25, match_text="OLD")
    _persist(db_session, sub)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{sub.id}",
        json={"business_pct": 60, "match_text": "NEW"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["business_pct"], r.json()["match_text"]) == (60, "NEW")

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{sub.id}",
        json={"business_pct": None, "match_text": None},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["business_pct"], r.json()["match_text"]) == (None, None)


def test_patch_rejects_an_out_of_range_business_pct(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    sub = make_subscription(user)
    _persist(db_session, sub)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{sub.id}", json={"business_pct": 101}, headers=auth_headers
    )
    assert r.status_code == 422


def test_patch_rejects_a_match_text_that_normalizes_to_empty(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    sub = make_subscription(user)
    _persist(db_session, sub)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{sub.id}", json={"match_text": "   "}, headers=auth_headers
    )
    assert r.status_code == 422


# --- GET /api/subscriptions ---------------------------------------------------------------


def test_list_excludes_inactive_by_default(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    active = make_subscription(user, name="Active", active=True)
    inactive = make_subscription(user, name="Inactive", active=False)
    _persist(db_session, active, inactive)

    r = db_client.get(SUBSCRIPTIONS, headers=auth_headers)

    ids = {item["id"] for item in r.json()["items"]}
    assert ids == {str(active.id)}


def test_list_include_inactive_returns_both(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    active = make_subscription(user, name="Active", active=True)
    inactive = make_subscription(user, name="Inactive", active=False)
    _persist(db_session, active, inactive)

    r = db_client.get(SUBSCRIPTIONS, params={"include_inactive": "true"}, headers=auth_headers)

    ids = {item["id"] for item in r.json()["items"]}
    assert ids == {str(active.id), str(inactive.id)}


def test_list_excludes_another_users_subscriptions(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    mine = make_subscription(user)
    theirs = make_subscription(other_user)
    _persist(db_session, mine, theirs)

    r = db_client.get(SUBSCRIPTIONS, headers=auth_headers)

    ids = {item["id"] for item in r.json()["items"]}
    assert ids == {str(mine.id)}


def test_list_sorts_by_next_due_with_nulls_last(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    no_due_date = make_subscription(user, name="No due date", next_due=None)
    later = make_subscription(user, name="Later", next_due=date(2026, 12, 1))
    sooner = make_subscription(user, name="Sooner", next_due=date(2026, 11, 1))
    _persist(db_session, no_due_date, later, sooner)

    r = db_client.get(SUBSCRIPTIONS, headers=auth_headers)

    names = [item["name"] for item in r.json()["items"]]
    assert names == ["Sooner", "Later", "No due date"]


def test_totals_split_by_kind_with_mixed_cadences(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    weekly_sub = make_subscription(
        user, name="Weekly", amount=Decimal("-10.00"), cadence=Cadence.weekly
    )
    quarterly_sub = make_subscription(
        user, name="Quarterly", amount=Decimal("-30.00"), cadence=Cadence.quarterly
    )
    car_bill = make_subscription(
        user,
        name="Car payment",
        amount=Decimal("-400.00"),
        cadence=Cadence.monthly,
        kind=SubscriptionKind.bill,
    )
    _persist(db_session, weekly_sub, quarterly_sub, car_bill)

    r = db_client.get(SUBSCRIPTIONS, headers=auth_headers)

    totals = r.json()["totals"]
    assert totals["subscription"]["monthly"] == "-53.33"
    assert totals["subscription"]["annual"] == "-640.00"
    assert totals["bill"]["monthly"] == "-400.00"
    assert totals["bill"]["annual"] == "-4800.00"


def test_totals_include_a_yearly_cadence_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    yearly_sub = make_subscription(
        user, name="Yearly", amount=Decimal("-120.00"), cadence=Cadence.yearly
    )
    _persist(db_session, yearly_sub)

    r = db_client.get(SUBSCRIPTIONS, headers=auth_headers)

    totals = r.json()["totals"]
    assert totals["subscription"]["monthly"] == "-10.00"
    assert totals["subscription"]["annual"] == "-120.00"


def test_totals_rounding_tie_is_half_even_and_normalizes_negative_zero(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """A yearly -0.06 is a monthly -0.005, a tie under HALF_EVEN (0 is the even digit),
    so it quantizes to -0.00 and is then normalized to 0.00 rather than shown signed.
    """
    tie_sub = make_subscription(user, name="Tie", amount=Decimal("-0.06"), cadence=Cadence.yearly)
    _persist(db_session, tie_sub)

    r = db_client.get(SUBSCRIPTIONS, headers=auth_headers)

    assert r.json()["totals"]["subscription"]["monthly"] == "0.00"


def test_totals_round_the_sum_once_not_each_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """Three yearly -0.04 rows sum to a monthly -0.01. Rounding each row's -0.04/12
    first would give -0.00 per row and a total of 0.00, which this rules out.
    """
    rows = [
        make_subscription(user, name=f"Row {i}", amount=Decimal("-0.04"), cadence=Cadence.yearly)
        for i in range(3)
    ]
    _persist(db_session, *rows)

    r = db_client.get(SUBSCRIPTIONS, headers=auth_headers)

    assert r.json()["totals"]["subscription"]["monthly"] == "-0.01"


def test_totals_exclude_inactive_rows_even_when_listed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    active = make_subscription(user, name="Active", amount=Decimal("-10.00"), active=True)
    inactive = make_subscription(user, name="Inactive", amount=Decimal("-20.00"), active=False)
    _persist(db_session, active, inactive)

    r = db_client.get(SUBSCRIPTIONS, params={"include_inactive": "true"}, headers=auth_headers)

    totals = r.json()["totals"]
    assert Decimal(totals["subscription"]["monthly"]) == Decimal("-10.00")


def test_totals_are_zero_with_no_subscriptions(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.get(SUBSCRIPTIONS, headers=auth_headers)

    totals = r.json()["totals"]
    assert totals["subscription"] == {"monthly": "0.00", "annual": "0.00"}
    assert totals["bill"] == {"monthly": "0.00", "annual": "0.00"}


def test_periods_per_year_covers_every_cadence() -> None:
    assert set(_PERIODS_PER_YEAR) == set(Cadence)


# --- PATCH /api/subscriptions/{id} --------------------------------------------------------


def test_patch_partial_update_only_touches_given_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    subscription = make_subscription(user, name="Spotify", active=True)
    _persist(db_session, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}", json={"active": False}, headers=auth_headers
    )

    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False
    assert body["name"] == "Spotify"


def test_patch_clears_account_id_with_explicit_null(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    subscription = make_subscription(user, account_id=account.id)
    _persist(db_session, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}", json={"account_id": None}, headers=auth_headers
    )

    assert r.status_code == 200
    assert r.json()["account_id"] is None


@pytest.mark.parametrize("field", ["name", "amount", "cadence", "kind", "bucket", "active"])
def test_patch_null_on_required_field_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    subscription = make_subscription(user)
    _persist(db_session, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}", json={field: None}, headers=auth_headers
    )

    assert r.status_code == 422


def test_patch_rejects_a_positive_amount(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    subscription = make_subscription(user)
    _persist(db_session, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}", json={"amount": "11.99"}, headers=auth_headers
    )

    assert r.status_code == 422


def test_patch_rejects_unknown_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    subscription = make_subscription(user)
    _persist(db_session, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}", json={"sneaky": "x"}, headers=auth_headers
    )

    assert r.status_code == 422


def test_patch_account_id_must_be_the_users_own(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    subscription = make_subscription(user)
    _persist(db_session, other_account, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}",
        json={"account_id": str(other_account.id)},
        headers=auth_headers,
    )

    assert r.status_code == 404


def test_patch_category_id_must_be_the_users_own(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_category = make_category(other_user)
    subscription = make_subscription(user)
    _persist(db_session, other_category, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}",
        json={"category_id": str(other_category.id)},
        headers=auth_headers,
    )

    assert r.status_code == 404


def test_patch_on_another_users_row_404s_identically_to_missing(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = make_subscription(other_user)
    _persist(db_session, theirs)

    missing = db_client.patch(
        f"{SUBSCRIPTIONS}/{uuid.uuid4()}", json={"active": False}, headers=auth_headers
    )
    someone_elses = db_client.patch(
        f"{SUBSCRIPTIONS}/{theirs.id}", json={"active": False}, headers=auth_headers
    )

    assert missing.status_code == 404
    assert someone_elses.status_code == 404
    missing_body, someone_elses_body = missing.json(), someone_elses.json()
    del missing_body["error"]["request_id"]
    del someone_elses_body["error"]["request_id"]
    assert missing_body == someone_elses_body


def test_patch_rejects_end_date_before_existing_start_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    subscription = make_subscription(user, start_date=date(2026, 6, 1))
    _persist(db_session, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}",
        json={"end_date": "2026-01-01"},
        headers=auth_headers,
    )

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [
        {
            "type": "value_error",
            "loc": ["body"],
            "msg": "Value error, end_date must be on or after start_date",
        }
    ]


def test_patch_rejects_start_date_after_existing_end_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    subscription = make_subscription(user, end_date=date(2026, 1, 1))
    _persist(db_session, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}",
        json={"start_date": "2026-06-01"},
        headers=auth_headers,
    )

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [
        {
            "type": "value_error",
            "loc": ["body"],
            "msg": "Value error, end_date must be on or after start_date",
        }
    ]


def test_patch_allows_moving_both_dates_together(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    subscription = make_subscription(user, start_date=date(2026, 1, 1), end_date=date(2026, 6, 1))
    _persist(db_session, subscription)

    r = db_client.patch(
        f"{SUBSCRIPTIONS}/{subscription.id}",
        json={"start_date": "2026-07-01", "end_date": "2026-12-01"},
        headers=auth_headers,
    )

    assert r.status_code == 200
