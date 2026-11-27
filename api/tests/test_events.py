import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Event, IncomeEntry, Transaction, User
from tests.factories import make_account, make_event, make_income_entry, make_transaction

EVENTS = "/api/events"


def _event_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Daytona 200",
        "start_date": "2026-03-07",
        "end_date": "2026-03-08",
    }
    body.update(overrides)
    return body


def test_event_round_trips(db_session: Session, user: User) -> None:
    event = make_event(
        user,
        name="Daytona 200",
        series="AMA Supersport",
        start_date=date(2026, 3, 7),
        end_date=date(2026, 3, 8),
        location="Daytona Beach, FL",
    )
    db_session.add(event)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Event, event.id)
    assert found.name == "Daytona 200"
    assert found.series == "AMA Supersport"
    assert found.start_date == date(2026, 3, 7)
    assert found.end_date == date(2026, 3, 8)
    assert found.location == "Daytona Beach, FL"


def test_event_rejects_end_date_before_start_date(db_session: Session, user: User) -> None:
    event = make_event(user, start_date=date(2026, 3, 8), end_date=date(2026, 3, 7))
    db_session.add(event)
    with pytest.raises(IntegrityError, match="ck_events_end_date_after_start_date"):
        db_session.flush()


def test_deleting_an_event_nulls_transaction_event_id(db_session: Session, user: User) -> None:
    account = make_account(user)
    event = make_event(user)
    db_session.add_all([account, event])
    db_session.flush()
    transaction = make_transaction(user, account, event_id=event.id)
    db_session.add(transaction)
    db_session.flush()

    db_session.delete(event)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Transaction, transaction.id)
    assert found.event_id is None


def test_deleting_an_event_nulls_income_entry_event_id(db_session: Session, user: User) -> None:
    event = make_event(user)
    db_session.add(event)
    db_session.flush()
    entry = make_income_entry(user, event_id=event.id)
    db_session.add(entry)
    db_session.flush()

    db_session.delete(event)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(IncomeEntry, entry.id)
    assert found.event_id is None


def test_event_index_exists(db_session: Session) -> None:
    indexes = inspect(db_session.connection()).get_indexes("events")
    assert {"name": "ix_events_user_id_start_date", "column_names": ["user_id", "start_date"]} in [
        {"name": ix["name"], "column_names": ix["column_names"]} for ix in indexes
    ]


def test_list_returns_only_own_events_ordered_by_start_date_desc(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    earlier = make_event(
        user, name="Sebring 12", start_date=date(2026, 3, 1), end_date=date(2026, 3, 1)
    )
    later = make_event(
        user, name="Daytona 200", start_date=date(2026, 3, 7), end_date=date(2026, 3, 8)
    )
    theirs = make_event(
        other, name="Their Race", start_date=date(2026, 3, 10), end_date=date(2026, 3, 10)
    )
    db_session.add_all([earlier, later, theirs])
    db_session.flush()

    r = db_client.get(EVENTS, headers=auth_headers)

    assert r.status_code == 200
    assert [row["name"] for row in r.json()] == ["Daytona 200", "Sebring 12"]


def test_list_filters_by_year(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    this_year = make_event(
        user, name="2026 Race", start_date=date(2026, 3, 7), end_date=date(2026, 3, 8)
    )
    other_year = make_event(
        user, name="2027 Race", start_date=date(2027, 1, 1), end_date=date(2027, 1, 2)
    )
    db_session.add_all([this_year, other_year])
    db_session.flush()

    r = db_client.get(EVENTS, params={"year": 2026}, headers=auth_headers)

    assert r.status_code == 200
    assert [row["name"] for row in r.json()] == ["2026 Race"]


def test_list_year_out_of_range_is_422_not_500(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = db_client.get(EVENTS, params={"year": 999999999}, headers=auth_headers)
    assert r.status_code == 422


def test_create_returns_201_with_the_created_event(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        EVENTS,
        json=_event_body(series="AMA Supersport", location="Daytona Beach, FL"),
        headers=auth_headers,
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Daytona 200"
    assert body["series"] == "AMA Supersport"
    assert body["start_date"] == "2026-03-07"
    assert body["end_date"] == "2026-03-08"
    assert body["location"] == "Daytona Beach, FL"
    assert "user_id" not in body


def test_create_is_idempotent_on_a_repeated_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    event_id = str(uuid.uuid4())
    first = db_client.post(EVENTS, json=_event_body(id=event_id), headers=auth_headers)
    second = db_client.post(
        EVENTS,
        json=_event_body(id=event_id, name="A Totally Different Name"),
        headers=auth_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["name"] == "Daytona 200"
    assert len(db_session.scalars(select(Event)).all()) == 1


def test_create_with_another_users_id_is_a_conflict(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = make_event(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.post(EVENTS, json=_event_body(id=str(theirs.id)), headers=auth_headers)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_create_rejects_end_date_before_start_date(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        EVENTS,
        json=_event_body(start_date="2026-03-08", end_date="2026-03-07"),
        headers=auth_headers,
    )
    assert r.status_code == 422


def test_create_rejects_unknown_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(EVENTS, json=_event_body(sneaky="nope"), headers=auth_headers)
    assert r.status_code == 422


def test_patch_updates_only_given_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    event = make_event(user, series="AMA Supersport", location="Daytona Beach, FL")
    db_session.add(event)
    db_session.flush()

    r = db_client.patch(
        f"{EVENTS}/{event.id}", json={"location": "Daytona, FL"}, headers=auth_headers
    )

    assert r.status_code == 200
    body = r.json()
    assert body["location"] == "Daytona, FL"
    assert body["series"] == "AMA Supersport"
    assert body["name"] == "Daytona 200"


def test_patch_on_missing_and_another_users_id_are_identical_404s(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = make_event(other)
    db_session.add(theirs)
    db_session.flush()

    missing = db_client.patch(
        f"{EVENTS}/{uuid.uuid4()}", json={"name": "New Name"}, headers=auth_headers
    )
    someone_elses = db_client.patch(
        f"{EVENTS}/{theirs.id}", json={"name": "New Name"}, headers=auth_headers
    )

    assert missing.status_code == 404
    assert someone_elses.status_code == 404
    missing_body, someone_elses_body = missing.json(), someone_elses.json()
    del missing_body["error"]["request_id"]
    del someone_elses_body["error"]["request_id"]
    assert missing_body == someone_elses_body


@pytest.mark.parametrize("field", ["name", "start_date", "end_date"])
def test_patch_null_on_required_field_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    event = make_event(user)
    db_session.add(event)
    db_session.flush()

    r = db_client.patch(f"{EVENTS}/{event.id}", json={field: None}, headers=auth_headers)

    assert r.status_code == 422


@pytest.mark.parametrize("field", ["series", "location"])
def test_patch_null_on_nullable_field_clears_it(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    event = make_event(user, series="AMA Supersport", location="Daytona Beach, FL")
    db_session.add(event)
    db_session.flush()

    r = db_client.patch(f"{EVENTS}/{event.id}", json={field: None}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()[field] is None


def test_patch_rejects_end_date_before_existing_start_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    event = make_event(user, start_date=date(2026, 3, 7), end_date=date(2026, 3, 8))
    db_session.add(event)
    db_session.flush()

    r = db_client.patch(
        f"{EVENTS}/{event.id}", json={"end_date": "2026-03-01"}, headers=auth_headers
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
    event = make_event(user, start_date=date(2026, 3, 7), end_date=date(2026, 3, 8))
    db_session.add(event)
    db_session.flush()

    r = db_client.patch(
        f"{EVENTS}/{event.id}", json={"start_date": "2026-03-20"}, headers=auth_headers
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


def test_patch_allows_both_dates_moved_together(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    event = make_event(user, start_date=date(2026, 3, 7), end_date=date(2026, 3, 8))
    db_session.add(event)
    db_session.flush()

    r = db_client.patch(
        f"{EVENTS}/{event.id}",
        json={"start_date": "2026-04-01", "end_date": "2026-04-02"},
        headers=auth_headers,
    )

    assert r.status_code == 200
    assert r.json()["start_date"] == "2026-04-01"
    assert r.json()["end_date"] == "2026-04-02"


def test_list_includes_costs_and_pay_totals_with_mixed_rows(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    with_rows = make_event(
        user, name="With Rows", start_date=date(2026, 3, 7), end_date=date(2026, 3, 8)
    )
    no_rows = make_event(
        user, name="No Rows", start_date=date(2026, 4, 1), end_date=date(2026, 4, 2)
    )
    db_session.add_all([account, with_rows, no_rows])
    db_session.flush()
    db_session.add_all(
        [
            make_transaction(user, account, event_id=with_rows.id, amount=Decimal("-100.00")),
            make_transaction(user, account, event_id=with_rows.id, amount=Decimal("-50.25")),
            make_income_entry(user, event_id=with_rows.id, received=Decimal("300.00")),
            make_income_entry(
                user, event_id=with_rows.id, received=None, expected=Decimal("50.00")
            ),
        ]
    )
    db_session.flush()

    r = db_client.get(EVENTS, headers=auth_headers)

    assert r.status_code == 200
    by_name = {row["name"]: row for row in r.json()}
    assert by_name["With Rows"]["costs_total"] == "-150.25"
    assert by_name["With Rows"]["pay_total"] == "300.00"
    assert by_name["No Rows"]["costs_total"] == "0.00"
    assert by_name["No Rows"]["pay_total"] == "0.00"


def test_list_totals_ignore_another_users_rows(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    event = make_event(user)
    other_account = make_account(other)
    db_session.add_all([event, other_account])
    db_session.flush()
    # The DB doesn't enforce same-user FKs (api.md); simulate a cross-user row directly
    # to prove the aggregate still scopes by user_id rather than trusting event_id alone.
    theirs = make_transaction(other, other_account, event_id=event.id, amount=Decimal("-999.00"))
    db_session.add(theirs)
    db_session.flush()

    r = db_client.get(EVENTS, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()[0]["costs_total"] == "0.00"


def test_list_costs_exclude_a_split_parent_even_though_its_children_share_the_event(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """#80: a split parent's children inherit its `event_id` by default; the parent
    must still drop out of the event's own cost total, or the charge counts twice.
    """
    account = make_account(user)
    event = make_event(user)
    db_session.add_all([account, event])
    db_session.flush()
    parent = make_transaction(user, account, event_id=event.id, amount=Decimal("-100.00"))
    db_session.add(parent)
    db_session.flush()

    split_response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json={"parts": [{"amount": "-60.00"}, {"amount": "-40.00"}]},
        headers=auth_headers,
    )
    assert split_response.status_code == 200
    assert all(child["event_id"] == str(event.id) for child in split_response.json()["children"])

    r = db_client.get(EVENTS, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()[0]["costs_total"] == "-100.00"
