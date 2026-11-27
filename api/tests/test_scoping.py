"""Enforces that every data table and query is scoped to a user.

See `.claude/rules/api.md` Data: every data table has `user_id`, and every query
filters on the current user's id.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.deps import DB, CurrentUser
from app.models import Base, Device, User
from app.models.enums import ApnsEnvironment
from app.scoping import get_owned, owned

# Tables without a NOT NULL user_id FK to users.id, and why.
NO_USER_ID_ALLOWLIST = {
    "users": "the user table itself, not a child row",
    "webauthn_challenges": "sign-in doesn't know the user until the passkey answers it",
    "auth_events": "the events feed lists every attempt, including ones with no known user (#22)",
    "app_attest_keys": "attested before sign-in; no user until its first sign-in binds it (#75)",
    "app_attest_challenges": "issued before the app has proven who the user is (#75)",
}


@pytest.mark.parametrize("name", list(Base.metadata.tables))
def test_data_table_has_a_not_null_user_id_fk(name: str) -> None:
    if name in NO_USER_ID_ALLOWLIST:
        pytest.skip(NO_USER_ID_ALLOWLIST[name])
    table = Base.metadata.tables[name]
    column = table.columns.get("user_id")
    assert column is not None, f"{name} has no user_id column"
    assert not column.nullable, f"{name}.user_id must be NOT NULL"
    targets = {fk.target_fullname for fk in column.foreign_keys}
    assert "users.id" in targets, f"{name}.user_id must reference users.id"


def _device(user: User, apns_token: str) -> Device:
    return Device(
        user_id=user.id,
        apns_token=apns_token,
        environment=ApnsEnvironment.sandbox,
        last_seen_at=datetime.now(UTC),
    )


@pytest.fixture
def device_app(app: FastAPI) -> FastAPI:
    router = APIRouter()

    @router.get("/api/_test/devices/{device_id}")
    def get_device(device_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, str]:
        device = get_owned(db, Device, device_id, user.id)
        return {"id": str(device.id)}

    app.include_router(router)
    return app


def test_get_owned_404s_identically_for_missing_and_someone_elses_row(
    device_app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = _device(other, "someone-elses-token")
    db_session.add(theirs)
    db_session.flush()

    missing = db_client.get(f"/api/_test/devices/{uuid.uuid4()}", headers=auth_headers)
    someone_elses = db_client.get(f"/api/_test/devices/{theirs.id}", headers=auth_headers)

    assert missing.status_code == 404
    assert someone_elses.status_code == 404
    missing_body, someone_elses_body = missing.json(), someone_elses.json()
    del missing_body["error"]["request_id"]
    del someone_elses_body["error"]["request_id"]
    assert missing_body == someone_elses_body


def test_get_owned_returns_the_requested_row_among_the_users_own(
    device_app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    mine_first = _device(user, "mine-first")
    mine_second = _device(user, "mine-second")
    theirs = _device(other, "theirs")
    db_session.add_all([mine_first, mine_second, theirs])
    db_session.flush()

    response = db_client.get(f"/api/_test/devices/{mine_second.id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["id"] == str(mine_second.id)
    assert response.json()["id"] != str(mine_first.id)


def test_owned_returns_only_the_current_users_rows(db_session: Session, user: User) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    mine = _device(user, "mine")
    theirs = _device(other, "theirs")
    db_session.add_all([mine, theirs])
    db_session.flush()

    rows = db_session.scalars(owned(Device, user.id)).all()
    assert [row.id for row in rows] == [mine.id]
