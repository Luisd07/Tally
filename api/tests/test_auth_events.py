"""Acceptance tests for #22: `GET /api/auth/events`.

Lists every `auth_events` row, including ones with `user_id = NULL`, instead of
scoping to the caller's `user_id` the way every other endpoint does. See
`app.services.auth_events` for why.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import tokens
from app.config import Settings
from app.models import User
from app.models.enums import AuthEventKind
from tests.factories import make_auth_event

EVENTS = "/api/auth/events"


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


# --- 401s ---------------------------------------------------------------------------


def test_requires_a_token(client: TestClient) -> None:
    r = client.get(EVENTS)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_rejects_an_expired_token(client: TestClient, settings: Settings) -> None:
    expired = tokens.create_access_token(
        uuid.uuid4(), settings, now=datetime.now(UTC) - timedelta(hours=1)
    )
    r = client.get(EVENTS, headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


# --- listing --------------------------------------------------------------------------


def test_list_includes_failed_sign_ins(
    db_client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    """The issue's acceptance criterion: a failed sign-in from an unknown credential
    (no `user_id`) still shows up in the feed.
    """
    failed = make_auth_event(kind=AuthEventKind.sign_in_failed, user_id=None)
    _persist(db_session, failed)

    response = db_client.get(EVENTS, headers=auth_headers)

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert str(failed.id) in ids


def test_list_includes_events_with_no_user(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    mine = make_auth_event(kind=AuthEventKind.sign_in, user_id=user.id)
    unowned = make_auth_event(kind=AuthEventKind.enroll_failed, user_id=None)
    _persist(db_session, mine, unowned)

    response = db_client.get(EVENTS, headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(mine.id), str(unowned.id)}


def test_list_returns_no_store(
    db_client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_auth_event())
    response = db_client.get(EVENTS, headers=auth_headers)
    assert response.headers["cache-control"] == "no-store"


def test_list_orders_newest_first_breaking_ties_by_id_descending(
    db_client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    tied_at = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    rows = [make_auth_event(created_at=tied_at) for _ in range(3)]
    older = make_auth_event(created_at=tied_at - timedelta(days=1))
    _persist(db_session, *rows, older)

    response = db_client.get(EVENTS, headers=auth_headers)

    ids = [item["id"] for item in response.json()["items"]]
    expected = [str(row.id) for row in sorted(rows, key=lambda r: r.id, reverse=True)]
    expected.append(str(older.id))
    assert ids == expected


def test_list_filters_by_kind(
    db_client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    sign_in = make_auth_event(kind=AuthEventKind.sign_in)
    sign_in_failed = make_auth_event(kind=AuthEventKind.sign_in_failed)
    _persist(db_session, sign_in, sign_in_failed)

    response = db_client.get(EVENTS, params={"kind": "sign_in_failed"}, headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(sign_in_failed.id)}


def test_list_rejects_an_unknown_kind(
    db_client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    response = db_client.get(EVENTS, params={"kind": "nope"}, headers=auth_headers)
    assert response.status_code == 422


def test_list_paging_returns_every_row_exactly_once_with_ties_on_created_at(
    db_client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    tied_at = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    rows = [make_auth_event(created_at=tied_at) for _ in range(5)]
    _persist(db_session, *rows)
    expected_ids = {str(row.id) for row in rows}

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        response = db_client.get(EVENTS, params=params, headers=auth_headers)
        page = response.json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert sorted(seen) == sorted(expected_ids)
    assert len(seen) == len(set(seen))


def test_list_rejects_an_invalid_cursor(
    db_client: TestClient, db_session: Session, auth_headers: dict[str, str]
) -> None:
    response = db_client.get(EVENTS, params={"cursor": "not-base64!!"}, headers=auth_headers)
    assert response.status_code == 422


@pytest.mark.parametrize("limit", ["0", "101"])
def test_list_limit_validation(
    db_client: TestClient, db_session: Session, auth_headers: dict[str, str], limit: str
) -> None:
    response = db_client.get(EVENTS, params={"limit": limit}, headers=auth_headers)
    assert response.status_code == 422
