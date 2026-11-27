import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device, User
from app.models.enums import ApnsEnvironment

DEVICES = "/api/devices"
TOKEN = "ab" * 32  # 64 hex chars, like a real APNs token


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"apns_token": TOKEN, "environment": "sandbox"}
    body.update(overrides)
    return body


def test_register_returns_201_on_first_registration(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(DEVICES, json=_body(), headers=auth_headers)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["apns_token"] == TOKEN
    assert body["environment"] == "sandbox"
    assert "user_id" not in body


def test_register_again_returns_200_and_refreshes_the_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    first = db_client.post(DEVICES, json=_body(), headers=auth_headers)
    second = db_client.post(DEVICES, json=_body(environment="production"), headers=auth_headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["environment"] == "production"
    assert len(db_session.scalars(select(Device)).all()) == 1


def test_register_updates_last_seen_at(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    existing = Device(
        user_id=user.id, apns_token=TOKEN, environment=ApnsEnvironment.sandbox, last_seen_at=stale
    )
    db_session.add(existing)
    db_session.flush()

    r = db_client.post(DEVICES, json=_body(), headers=auth_headers)

    assert datetime.fromisoformat(r.json()["last_seen_at"]) > stale


def test_register_reassigns_a_token_from_another_user(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = Device(
        user_id=other.id,
        apns_token=TOKEN,
        environment=ApnsEnvironment.sandbox,
        last_seen_at=datetime.now(UTC),
    )
    db_session.add(theirs)
    db_session.flush()

    r = db_client.post(DEVICES, json=_body(), headers=auth_headers)

    assert r.status_code == 200
    row = db_session.scalars(select(Device).where(Device.apns_token == TOKEN)).one()
    assert row.user_id == user.id
    assert len(db_session.scalars(select(Device)).all()) == 1


VALIDATION_CASES = [
    {"apns_token": "not-hex!"},
    {"apns_token": "x" * 201},
    {"apns_token": ""},
    {"environment": "staging"},
]
VALIDATION_IDS = ["non-hex", "too-long", "empty", "unknown-environment"]


@pytest.mark.parametrize("overrides", VALIDATION_CASES, ids=VALIDATION_IDS)
def test_register_validation_rejects_bad_input(
    db_client: TestClient, user: User, auth_headers: dict[str, str], overrides: dict[str, object]
) -> None:
    r = db_client.post(DEVICES, json=_body(**overrides), headers=auth_headers)
    assert r.status_code == 422


def test_register_rejects_unknown_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(DEVICES, json=_body(sneaky="x"), headers=auth_headers)
    assert r.status_code == 422
