"""POST /api/sync/simplefin/claim: exchange a setup token for an Access URL, store
it encrypted, and never say it out loud. See `tests/test_simplefin.py` for the
lower-level `claim`/`fetch` behavior this endpoint builds on.
"""

import base64
import io
import socket

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import get_simplefin_transport
from app.models import SimplefinConnection, User
from app.services import simplefin
from tests.factories import make_simplefin_connection

CLAIM = "/api/sync/simplefin/claim"
CLAIM_URL = "https://bridge.simplefin.org/simplefin/claim/demo-token"
SETUP_TOKEN = base64.b64encode(CLAIM_URL.encode()).decode()
ACCESS_URL = "https://demo-user:demo-pass@bridge.simplefin.org/simplefin"
PUBLIC_SOCKADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF guard resolves the claim/Access URL host via `socket.getaddrinfo`;
    stub it to a fixed public address so these tests never touch real DNS.
    """
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: PUBLIC_SOCKADDR)


def _success_transport(access_url: str = ACCESS_URL) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, text=access_url))


def _use_transport(app: FastAPI, transport: httpx.MockTransport | None) -> None:
    """Override the `SimplefinTransport` dependency so `claim_connection` answers
    through a MockTransport instead of the real network.
    """
    app.dependency_overrides[get_simplefin_transport] = lambda: transport or _success_transport()


def test_claim_stores_the_url_encrypted_and_never_returns_it(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    settings: Settings,
) -> None:
    _use_transport(app, None)

    r = db_client.post(CLAIM, json={"setup_token": SETUP_TOKEN}, headers=auth_headers)

    assert r.status_code == 201
    body = r.json()
    assert body["host"] == "bridge.simplefin.org"
    assert ACCESS_URL not in r.text
    assert "demo-pass" not in r.text

    row = db_session.scalars(
        select(SimplefinConnection).where(SimplefinConnection.user_id == user.id)
    ).one()
    assert simplefin.decrypt_access_url(settings, row.access_url_encrypted) == ACCESS_URL
    assert ACCESS_URL.encode() not in row.access_url_encrypted


def test_claim_403_becomes_a_clear_4xx(
    app: FastAPI, db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    _use_transport(app, httpx.MockTransport(lambda request: httpx.Response(403)))

    r = db_client.post(CLAIM, json={"setup_token": SETUP_TOKEN}, headers=auth_headers)

    assert r.status_code == 409
    body = r.json()["error"]["message"].lower()
    assert "compromised" in body
    assert "disable" in body


def test_claim_bad_base64_is_a_422(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    r = db_client.post(CLAIM, json={"setup_token": "not valid base64!!"}, headers=auth_headers)

    assert r.status_code == 422


def test_claim_non_https_claim_url_is_refused(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    token = base64.b64encode(b"http://bridge.simplefin.org/simplefin/claim/demo").decode()

    r = db_client.post(CLAIM, json={"setup_token": token}, headers=auth_headers)

    assert r.status_code == 422


def test_claim_network_error_is_a_502(
    app: FastAPI, db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _use_transport(app, httpx.MockTransport(handler))

    r = db_client.post(CLAIM, json={"setup_token": SETUP_TOKEN}, headers=auth_headers)

    assert r.status_code == 502
    assert r.json()["error"]["code"] == "bad_gateway"


def test_claim_server_error_is_a_502(
    app: FastAPI, db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    _use_transport(app, httpx.MockTransport(lambda request: httpx.Response(500)))

    r = db_client.post(CLAIM, json={"setup_token": SETUP_TOKEN}, headers=auth_headers)

    assert r.status_code == 502
    assert r.json()["error"]["code"] == "bad_gateway"


def test_reclaim_replaces_the_stored_url_and_bumps_updated_at(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    settings: Settings,
) -> None:
    other_access_url = "https://second-user:second-pass@bridge.simplefin.org/simplefin"

    _use_transport(app, _success_transport())
    first = db_client.post(CLAIM, json={"setup_token": SETUP_TOKEN}, headers=auth_headers)
    assert first.status_code == 201
    first_row = db_session.scalars(
        select(SimplefinConnection).where(SimplefinConnection.user_id == user.id)
    ).one()
    first_updated_at = first_row.updated_at

    _use_transport(app, _success_transport(other_access_url))
    second = db_client.post(CLAIM, json={"setup_token": SETUP_TOKEN}, headers=auth_headers)
    assert second.status_code == 201

    db_session.expunge_all()  # claim_connection upserts through Core; refetch past the identity map
    rows = db_session.scalars(
        select(SimplefinConnection).where(SimplefinConnection.user_id == user.id)
    ).all()
    assert len(rows) == 1
    assert simplefin.decrypt_access_url(settings, rows[0].access_url_encrypted) == other_access_url
    assert rows[0].updated_at > first_updated_at


def test_claim_is_scoped_to_the_claiming_user(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = make_simplefin_connection(other_user)
    db_session.add(theirs)
    db_session.flush()

    _use_transport(app, None)
    r = db_client.post(CLAIM, json={"setup_token": SETUP_TOKEN}, headers=auth_headers)
    assert r.status_code == 201

    rows = db_session.scalars(select(SimplefinConnection)).all()
    assert {row.user_id for row in rows} == {user.id, other_user.id}


def test_claim_logs_never_contain_the_url_or_its_credentials(
    app: FastAPI, db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    _use_transport(app, None)
    sink = io.StringIO()
    handler_id = logger.add(sink, level="DEBUG")
    try:
        db_client.post(CLAIM, json={"setup_token": SETUP_TOKEN}, headers=auth_headers)
    finally:
        logger.remove(handler_id)

    output = sink.getvalue()
    assert ACCESS_URL not in output
    assert "demo-pass" not in output
    assert SETUP_TOKEN not in output
    assert CLAIM_URL not in output
    assert "demo-token" not in output
