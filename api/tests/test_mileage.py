import importlib.util
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import APP_TIMEZONE
from app.models import MileageRate, MileageTrip, OdometerReading, SavedTrip, User
from app.services.mileage import rate_in_force
from tests.conftest import API_DIR
from tests.factories import (
    make_mileage_rate,
    make_mileage_trip,
    make_odometer_reading,
    make_saved_trip,
)

MILEAGE = "/api/mileage"
TRIPS = f"{MILEAGE}/trips"
SAVED = f"{MILEAGE}/saved-trips"
ODOMETER = f"{MILEAGE}/odometer"
RATES = f"{MILEAGE}/rates"


def _trip_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"date": "2026-10-01", "miles": "24.6", "purpose": "Client visit"}
    body.update(overrides)
    return body


def _saved_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"name": "Office to track", "miles": "31.2", "purpose": "Setup"}
    body.update(overrides)
    return body


def _add[T](db_session: Session, row: T) -> T:
    db_session.add(row)
    db_session.flush()
    return row


# --- rate_in_force -------------------------------------------------------------------


def _rates() -> list[MileageRate]:
    return [
        MileageRate(effective_from=date(2026, 7, 1), rate=Decimal("0.7600")),
        MileageRate(effective_from=date(2025, 1, 1), rate=Decimal("0.7000")),
        MileageRate(effective_from=date(2026, 1, 1), rate=Decimal("0.7250")),
    ]


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        (date(2025, 1, 1), Decimal("0.7000")),
        (date(2025, 12, 31), Decimal("0.7000")),
        (date(2026, 1, 1), Decimal("0.7250")),
        (date(2026, 6, 30), Decimal("0.7250")),
        (date(2026, 7, 1), Decimal("0.7600")),
        (date(2027, 3, 1), Decimal("0.7600")),
    ],
)
def test_rate_in_force_picks_the_latest_rate_starting_on_or_before_the_date(
    on: date, expected: Decimal
) -> None:
    rate = rate_in_force(_rates(), on)

    assert rate is not None
    assert rate.rate == expected


def test_rate_in_force_is_none_before_the_first_rate() -> None:
    assert rate_in_force(_rates(), date(2024, 12, 31)) is None


def test_rate_in_force_is_none_without_rates() -> None:
    assert rate_in_force([], date(2026, 1, 1)) is None


# --- seed migration ------------------------------------------------------------------


def _seed_migration() -> ModuleType:
    (path,) = (API_DIR / "alembic" / "versions").glob("*_seed_irs_mileage_rates_*.py")
    spec = importlib.util.spec_from_file_location("seed_irs_mileage_rates", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_migration_gives_an_existing_user_the_irs_rates(
    db_session: Session, user: User
) -> None:
    db_session.execute(text(_seed_migration().SEED_SQL))

    rows = db_session.scalars(
        select(MileageRate)
        .where(MileageRate.user_id == user.id)
        .order_by(MileageRate.effective_from)
    ).all()
    assert [(row.effective_from, row.rate) for row in rows] == [
        (date(2025, 1, 1), Decimal("0.7000")),
        (date(2026, 1, 1), Decimal("0.7250")),
        (date(2026, 7, 1), Decimal("0.7600")),
    ]


def test_seed_migration_keeps_a_rate_the_user_already_has(db_session: Session, user: User) -> None:
    _add(db_session, make_mileage_rate(user, effective_from=date(2026, 1, 1), rate=Decimal("0.8")))

    db_session.execute(text(_seed_migration().SEED_SQL))

    rows = db_session.scalars(select(MileageRate).where(MileageRate.user_id == user.id)).all()
    assert len(rows) == 3
    assert {row.effective_from: row.rate for row in rows}[date(2026, 1, 1)] == Decimal("0.8000")


# --- trips ---------------------------------------------------------------------------


def test_create_trip_returns_201_with_decimal_strings(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        TRIPS,
        json=_trip_body(start_location="Home", end_location="Daytona"),
        headers=auth_headers,
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["date"] == "2026-10-01"
    assert body["miles"] == "24.6"
    assert body["purpose"] == "Client visit"
    assert body["start_location"] == "Home"
    assert body["end_location"] == "Daytona"
    assert "user_id" not in body


def test_create_trip_is_idempotent_on_a_repeated_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    trip_id = str(uuid.uuid4())
    first = db_client.post(TRIPS, json=_trip_body(id=trip_id), headers=auth_headers)
    second = db_client.post(TRIPS, json=_trip_body(id=trip_id, miles="99.9"), headers=auth_headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["miles"] == "24.6"
    assert len(db_session.scalars(select(MileageTrip)).all()) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"miles": "0"},
        {"miles": "-3.0"},
        {"miles": "12.25"},
        {"purpose": ""},
        {"purpose": "   "},
        {"purpose": None},
    ],
)
def test_create_trip_rejects_invalid_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str], overrides: dict[str, object]
) -> None:
    r = db_client.post(TRIPS, json=_trip_body(**overrides), headers=auth_headers)

    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"


def test_list_trips_filters_by_year_newest_first(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    older = _add(db_session, make_mileage_trip(user, date=date(2026, 3, 2)))
    newer = _add(db_session, make_mileage_trip(user, date=date(2026, 11, 5)))
    _add(db_session, make_mileage_trip(user, date=date(2025, 12, 31)))

    r = db_client.get(TRIPS, params={"year": 2026}, headers=auth_headers)

    assert r.status_code == 200
    assert [row["id"] for row in r.json()] == [str(newer.id), str(older.id)]


def test_list_trips_without_a_year_lists_every_trip(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _add(db_session, make_mileage_trip(user, date=date(2025, 12, 31)))
    _add(db_session, make_mileage_trip(user, date=date(2026, 1, 1)))

    r = db_client.get(TRIPS, headers=auth_headers)

    assert [row["date"] for row in r.json()] == ["2026-01-01", "2025-12-31"]


def test_update_trip_applies_the_patch(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    trip = _add(db_session, make_mileage_trip(user, start_location="Home"))

    r = db_client.patch(
        f"{TRIPS}/{trip.id}",
        json={"miles": "30.1", "purpose": "Supplier pickup", "start_location": None},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["miles"] == "30.1"
    assert r.json()["purpose"] == "Supplier pickup"
    assert r.json()["start_location"] is None
    assert r.json()["date"] == "2026-10-01"


@pytest.mark.parametrize(
    "patch", [{"miles": "0"}, {"miles": "1.05"}, {"purpose": ""}, {"purpose": None}]
)
def test_update_trip_rejects_invalid_fields(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    patch: dict[str, object],
) -> None:
    trip = _add(db_session, make_mileage_trip(user))

    r = db_client.patch(f"{TRIPS}/{trip.id}", json=patch, headers=auth_headers)

    assert r.status_code == 422, r.text


def test_delete_trip(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    trip = _add(db_session, make_mileage_trip(user))

    r = db_client.delete(f"{TRIPS}/{trip.id}", headers=auth_headers)

    assert r.status_code == 204
    assert db_session.get(MileageTrip, trip.id) is None


def test_another_users_trip_is_not_found(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = _add(db_session, make_mileage_trip(other_user))

    patch = db_client.patch(f"{TRIPS}/{theirs.id}", json={"miles": "1.0"}, headers=auth_headers)
    delete = db_client.delete(f"{TRIPS}/{theirs.id}", headers=auth_headers)
    listed = db_client.get(TRIPS, headers=auth_headers)

    assert patch.status_code == 404
    assert delete.status_code == 404
    assert listed.json() == []
    assert db_session.get(MileageTrip, theirs.id) is not None


# --- saved trips ---------------------------------------------------------------------


def test_create_saved_trip(db_client: TestClient, user: User, auth_headers: dict[str, str]) -> None:
    r = db_client.post(SAVED, json=_saved_body(end_location="Track"), headers=auth_headers)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Office to track"
    assert body["miles"] == "31.2"
    assert body["purpose"] == "Setup"
    assert body["start_location"] is None
    assert body["end_location"] == "Track"


@pytest.mark.parametrize(
    "overrides",
    [{"miles": "0"}, {"miles": "2.25"}, {"purpose": " "}, {"name": ""}],
)
def test_create_saved_trip_rejects_invalid_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str], overrides: dict[str, object]
) -> None:
    r = db_client.post(SAVED, json=_saved_body(**overrides), headers=auth_headers)

    assert r.status_code == 422, r.text


def test_list_saved_trips_by_name(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _add(db_session, make_saved_trip(user, name="Zoo"))
    _add(db_session, make_saved_trip(user, name="Airport"))

    r = db_client.get(SAVED, headers=auth_headers)

    assert [row["name"] for row in r.json()] == ["Airport", "Zoo"]


def test_update_saved_trip(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    saved = _add(db_session, make_saved_trip(user))

    r = db_client.patch(
        f"{SAVED}/{saved.id}", json={"name": "Home to shop", "miles": "8.4"}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Home to shop"
    assert r.json()["miles"] == "8.4"


def test_delete_saved_trip(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    saved = _add(db_session, make_saved_trip(user))

    r = db_client.delete(f"{SAVED}/{saved.id}", headers=auth_headers)

    assert r.status_code == 204
    assert db_session.get(SavedTrip, saved.id) is None


def test_log_saved_trip_creates_a_trip_dated_today(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    saved = _add(db_session, make_saved_trip(user, start_location="Office", end_location="Track"))
    before = datetime.now(UTC).astimezone(APP_TIMEZONE).date()

    r = db_client.post(f"{SAVED}/{saved.id}/log", headers=auth_headers)

    after = datetime.now(UTC).astimezone(APP_TIMEZONE).date()
    assert r.status_code == 201, r.text
    body = r.json()
    assert date.fromisoformat(body["date"]) in {before, after}
    assert body["miles"] == "31.2"
    assert body["purpose"] == "Race day setup"
    assert body["start_location"] == "Office"
    assert body["end_location"] == "Track"
    trip = db_session.get(MileageTrip, uuid.UUID(body["id"]))
    assert trip is not None
    assert trip.user_id == user.id


def test_log_saved_trip_is_idempotent_on_a_repeated_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    saved = _add(db_session, make_saved_trip(user))
    trip_id = str(uuid.uuid4())

    first = db_client.post(f"{SAVED}/{saved.id}/log", json={"id": trip_id}, headers=auth_headers)
    second = db_client.post(f"{SAVED}/{saved.id}/log", json={"id": trip_id}, headers=auth_headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == trip_id
    assert second.json() == first.json()
    assert len(db_session.scalars(select(MileageTrip)).all()) == 1


def test_log_saved_trip_rejects_unknown_body_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    saved = _add(db_session, make_saved_trip(user))

    r = db_client.post(f"{SAVED}/{saved.id}/log", json={"miles": "5.0"}, headers=auth_headers)

    assert r.status_code == 422


def test_another_users_saved_trip_is_not_found(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = _add(db_session, make_saved_trip(other_user))

    patch = db_client.patch(f"{SAVED}/{theirs.id}", json={"name": "Mine"}, headers=auth_headers)
    log = db_client.post(f"{SAVED}/{theirs.id}/log", headers=auth_headers)
    delete = db_client.delete(f"{SAVED}/{theirs.id}", headers=auth_headers)
    listed = db_client.get(SAVED, headers=auth_headers)

    assert patch.status_code == 404
    assert log.status_code == 404
    assert delete.status_code == 404
    assert listed.json() == []
    assert db_session.scalars(select(MileageTrip)).all() == []


# --- odometer ------------------------------------------------------------------------


def test_odometer_is_not_found_until_set(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.get(f"{ODOMETER}/2026", headers=auth_headers)

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_put_odometer_creates_then_replaces_the_years_reading(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    created = db_client.put(
        f"{ODOMETER}/2026", json={"start_reading": "41250.0"}, headers=auth_headers
    )
    replaced = db_client.put(
        f"{ODOMETER}/2026",
        json={"start_reading": "41250.5", "end_reading": "52010.3"},
        headers=auth_headers,
    )
    fetched = db_client.get(f"{ODOMETER}/2026", headers=auth_headers)

    assert created.status_code == 200, created.text
    assert created.json()["start_reading"] == "41250.0"
    assert created.json()["end_reading"] is None
    assert replaced.status_code == 200, replaced.text
    assert fetched.json()["year"] == 2026
    assert fetched.json()["start_reading"] == "41250.5"
    assert fetched.json()["end_reading"] == "52010.3"
    assert len(db_session.scalars(select(OdometerReading)).all()) == 1


def test_put_odometer_can_clear_the_end_reading(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _add(db_session, make_odometer_reading(user, end_reading=Decimal("50000.0")))

    r = db_client.put(f"{ODOMETER}/2026", json={"start_reading": "41250.0"}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()["end_reading"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"start_reading": "5000.0", "end_reading": "4999.9"},
        {"start_reading": "-1"},
        {"start_reading": "100.25"},
        {},
    ],
)
def test_put_odometer_rejects_invalid_readings(
    db_client: TestClient, user: User, auth_headers: dict[str, str], body: dict[str, object]
) -> None:
    r = db_client.put(f"{ODOMETER}/2026", json=body, headers=auth_headers)

    assert r.status_code == 422, r.text


def test_another_users_odometer_is_not_found_and_untouched(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = _add(db_session, make_odometer_reading(other_user))

    fetched = db_client.get(f"{ODOMETER}/2026", headers=auth_headers)
    put = db_client.put(f"{ODOMETER}/2026", json={"start_reading": "1.0"}, headers=auth_headers)

    assert fetched.status_code == 404
    assert put.status_code == 200
    db_session.refresh(theirs)
    assert theirs.start_reading == Decimal("41250.0")


# --- rates ---------------------------------------------------------------------------


def test_create_rate(db_client: TestClient, user: User, auth_headers: dict[str, str]) -> None:
    r = db_client.post(
        RATES, json={"effective_from": "2027-01-01", "rate": "0.78"}, headers=auth_headers
    )

    assert r.status_code == 201, r.text
    assert r.json()["effective_from"] == "2027-01-01"
    assert r.json()["rate"] == "0.7800"


@pytest.mark.parametrize("rate", ["0", "-0.5", "0.12345"])
def test_create_rate_rejects_invalid_rates(
    db_client: TestClient, user: User, auth_headers: dict[str, str], rate: str
) -> None:
    r = db_client.post(
        RATES, json={"effective_from": "2027-01-01", "rate": rate}, headers=auth_headers
    )

    assert r.status_code == 422, r.text


def test_create_rate_on_a_taken_date_is_a_conflict(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _add(db_session, make_mileage_rate(user))

    r = db_client.post(
        RATES, json={"effective_from": "2026-01-01", "rate": "0.70"}, headers=auth_headers
    )

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_another_users_rate_date_does_not_conflict(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    _add(db_session, make_mileage_rate(other_user))

    r = db_client.post(
        RATES, json={"effective_from": "2026-01-01", "rate": "0.70"}, headers=auth_headers
    )

    assert r.status_code == 201


def test_list_rates_oldest_first(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _add(db_session, make_mileage_rate(user, effective_from=date(2026, 7, 1)))
    _add(db_session, make_mileage_rate(user, effective_from=date(2025, 1, 1)))

    r = db_client.get(RATES, headers=auth_headers)

    assert [row["effective_from"] for row in r.json()] == ["2025-01-01", "2026-07-01"]


def test_update_rate(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rate = _add(db_session, make_mileage_rate(user))

    r = db_client.patch(
        f"{RATES}/{rate.id}",
        json={"effective_from": "2026-02-01", "rate": "0.7300"},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["effective_from"] == "2026-02-01"
    assert r.json()["rate"] == "0.7300"


def test_update_rate_onto_a_taken_date_is_a_conflict(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _add(db_session, make_mileage_rate(user, effective_from=date(2026, 1, 1)))
    later = _add(db_session, make_mileage_rate(user, effective_from=date(2026, 7, 1)))

    r = db_client.patch(
        f"{RATES}/{later.id}", json={"effective_from": "2026-01-01"}, headers=auth_headers
    )

    assert r.status_code == 409


def test_delete_rate(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    rate = _add(db_session, make_mileage_rate(user))

    r = db_client.delete(f"{RATES}/{rate.id}", headers=auth_headers)

    assert r.status_code == 204
    assert db_session.get(MileageRate, rate.id) is None


def test_another_users_rate_is_not_found(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = _add(db_session, make_mileage_rate(other_user))

    patch = db_client.patch(f"{RATES}/{theirs.id}", json={"rate": "0.1"}, headers=auth_headers)
    delete = db_client.delete(f"{RATES}/{theirs.id}", headers=auth_headers)
    listed = db_client.get(RATES, headers=auth_headers)

    assert patch.status_code == 404
    assert delete.status_code == 404
    assert listed.json() == []
    assert db_session.get(MileageRate, theirs.id) is not None
