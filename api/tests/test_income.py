import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IncomeEntry, User
from app.models.enums import IncomeType
from tests.factories import make_account, make_event, make_income_entry, make_transaction

INCOME = "/api/income"


def _income_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "date": "2026-10-01",
        "type": "event_pay",
        "expected": "250.00",
    }
    body.update(overrides)
    return body


def test_create_returns_201_with_the_created_entry(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        INCOME,
        json=_income_body(description="October race pay", received="250.00"),
        headers=auth_headers,
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["date"] == "2026-10-01"
    assert body["type"] == "event_pay"
    assert body["description"] == "October race pay"
    assert body["expected"] == "250.00"
    assert body["received"] == "250.00"
    assert "user_id" not in body


def test_create_is_idempotent_on_a_repeated_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    income_id = str(uuid.uuid4())
    first = db_client.post(INCOME, json=_income_body(id=income_id), headers=auth_headers)
    second = db_client.post(
        INCOME,
        json=_income_body(id=income_id, expected="999.00"),
        headers=auth_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["expected"] == "250.00"
    assert len(db_session.scalars(select(IncomeEntry)).all()) == 1


def test_create_with_another_users_id_is_a_conflict(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = make_income_entry(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.post(INCOME, json=_income_body(id=str(theirs.id)), headers=auth_headers)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_create_rejects_another_users_transaction_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    other_account = make_account(other)
    db_session.add(other_account)
    db_session.flush()
    theirs = make_transaction(other, other_account)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.post(
        INCOME, json=_income_body(transaction_id=str(theirs.id)), headers=auth_headers
    )

    assert r.status_code == 404


def test_create_rejects_another_users_event_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = make_event(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.post(INCOME, json=_income_body(event_id=str(theirs.id)), headers=auth_headers)

    assert r.status_code == 404


def test_create_rejects_missing_transaction_id(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        INCOME, json=_income_body(transaction_id=str(uuid.uuid4())), headers=auth_headers
    )

    assert r.status_code == 404


def test_create_rejects_null_on_required_field(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(INCOME, json=_income_body(date=None), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_negative_expected(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(INCOME, json=_income_body(expected="-1.00"), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_negative_received(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        INCOME, json=_income_body(expected=None, received="-1.00"), headers=auth_headers
    )
    assert r.status_code == 422


def test_create_rejects_neither_expected_nor_received(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(INCOME, json=_income_body(expected=None), headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_unknown_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(INCOME, json=_income_body(sneaky="nope"), headers=auth_headers)
    assert r.status_code == 422


def test_patch_updates_only_given_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user, description="October race pay", expected=Decimal("250.00"))
    db_session.add(entry)
    db_session.flush()

    r = db_client.patch(f"{INCOME}/{entry.id}", json={"received": "250.00"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert body["received"] == "250.00"
    assert body["expected"] == "250.00"
    assert body["description"] == "October race pay"


def test_patch_on_missing_and_another_users_id_are_identical_404s(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = make_income_entry(other)
    db_session.add(theirs)
    db_session.flush()

    missing = db_client.patch(
        f"{INCOME}/{uuid.uuid4()}", json={"expected": "1.00"}, headers=auth_headers
    )
    someone_elses = db_client.patch(
        f"{INCOME}/{theirs.id}", json={"expected": "1.00"}, headers=auth_headers
    )

    assert missing.status_code == 404
    assert someone_elses.status_code == 404
    missing_body, someone_elses_body = missing.json(), someone_elses.json()
    del missing_body["error"]["request_id"]
    del someone_elses_body["error"]["request_id"]
    assert missing_body == someone_elses_body


def test_patch_rejects_another_users_transaction_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user)
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add_all([entry, other])
    db_session.flush()
    other_account = make_account(other)
    db_session.add(other_account)
    db_session.flush()
    theirs = make_transaction(other, other_account)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.patch(
        f"{INCOME}/{entry.id}", json={"transaction_id": str(theirs.id)}, headers=auth_headers
    )

    assert r.status_code == 404


def test_patch_rejects_another_users_event_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user)
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add_all([entry, other])
    db_session.flush()
    theirs = make_event(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.patch(
        f"{INCOME}/{entry.id}", json={"event_id": str(theirs.id)}, headers=auth_headers
    )

    assert r.status_code == 404


def test_patch_null_on_required_field_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user)
    db_session.add(entry)
    db_session.flush()

    r = db_client.patch(f"{INCOME}/{entry.id}", json={"date": None}, headers=auth_headers)

    assert r.status_code == 422


def test_patch_clearing_the_last_amount_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user, expected=Decimal("250.00"), received=None)
    db_session.add(entry)
    db_session.flush()

    r = db_client.patch(f"{INCOME}/{entry.id}", json={"expected": None}, headers=auth_headers)

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"] == [
        {
            "type": "value_error",
            "loc": ["body"],
            "msg": "Value error, expected or received must be set",
        }
    ]


def test_patch_clearing_one_side_is_allowed_when_the_other_is_set(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user, expected=Decimal("250.00"), received=Decimal("250.00"))
    db_session.add(entry)
    db_session.flush()

    r = db_client.patch(f"{INCOME}/{entry.id}", json={"expected": None}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()["expected"] is None
    assert r.json()["received"] == "250.00"


def test_patch_rejects_negative_amount(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user)
    db_session.add(entry)
    db_session.flush()

    r = db_client.patch(f"{INCOME}/{entry.id}", json={"received": "-1.00"}, headers=auth_headers)

    assert r.status_code == 422


def test_list_filters_by_month(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    in_month = make_income_entry(user, date=date(2026, 10, 15))
    other_month = make_income_entry(user, date=date(2026, 11, 1))
    db_session.add_all([in_month, other_month])
    db_session.flush()

    r = db_client.get(INCOME, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert [row["id"] for row in body["items"]] == [str(in_month.id)]


def test_list_filters_by_month_across_the_december_rollover(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    in_month = make_income_entry(user, date=date(2026, 12, 15))
    next_year = make_income_entry(user, date=date(2027, 1, 1))
    db_session.add_all([in_month, next_year])
    db_session.flush()

    r = db_client.get(INCOME, params={"month": "2026-12"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert [row["id"] for row in body["items"]] == [str(in_month.id)]


def test_list_month_out_of_range_is_422_not_500(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = db_client.get(INCOME, params={"month": "0000-12"}, headers=auth_headers)
    assert r.status_code == 422


def test_list_rejects_badly_formatted_month(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = db_client.get(INCOME, params={"month": "2026-13"}, headers=auth_headers)
    assert r.status_code == 422


def test_list_filters_by_type(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    event_pay = make_income_entry(user, type=IncomeType.event_pay)
    remote_hours = make_income_entry(user, type=IncomeType.remote_hours)
    db_session.add_all([event_pay, remote_hours])
    db_session.flush()

    r = db_client.get(INCOME, params={"type": "remote_hours"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert [row["id"] for row in body["items"]] == [str(remote_hours.id)]


def test_list_filters_by_event(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    event = make_event(user)
    db_session.add(event)
    db_session.flush()
    linked = make_income_entry(user, event_id=event.id)
    unlinked = make_income_entry(user)
    db_session.add_all([linked, unlinked])
    db_session.flush()

    r = db_client.get(INCOME, params={"event_id": str(event.id)}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert [row["id"] for row in body["items"]] == [str(linked.id)]


def test_list_orders_by_date_desc_then_id_desc(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    earlier = make_income_entry(user, date=date(2026, 10, 1))
    later = make_income_entry(user, date=date(2026, 10, 15))
    db_session.add_all([earlier, later])
    db_session.flush()

    r = db_client.get(INCOME, headers=auth_headers)

    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["items"]]
    assert ids == [str(later.id), str(earlier.id)]


def test_list_totals_sum_the_filtered_set_with_null_as_zero(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    db_session.add_all(
        [
            make_income_entry(
                user, date=date(2026, 10, 1), expected=Decimal("100.00"), received=Decimal("90.00")
            ),
            make_income_entry(
                user, date=date(2026, 10, 2), expected=Decimal("50.00"), received=None
            ),
            make_income_entry(
                user, date=date(2026, 11, 1), expected=Decimal("999.00"), received=Decimal("999.00")
            ),
        ]
    )
    db_session.flush()

    r = db_client.get(INCOME, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    totals = r.json()["totals"]
    assert totals["expected"] == "150.00"
    assert totals["received"] == "90.00"


def test_list_totals_are_zero_with_no_matching_rows(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = db_client.get(INCOME, headers=auth_headers)

    assert r.status_code == 200
    totals = r.json()["totals"]
    assert totals["expected"] == "0.00"
    assert totals["received"] == "0.00"


def test_list_only_returns_the_callers_own_entries(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    mine = make_income_entry(user)
    theirs = make_income_entry(other, expected=Decimal("999.00"))
    db_session.add_all([mine, theirs])
    db_session.flush()

    r = db_client.get(INCOME, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert [row["id"] for row in body["items"]] == [str(mine.id)]
    assert body["totals"]["expected"] == "250.00"
