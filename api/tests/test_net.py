"""`client_ip`: the one place that turns a proxy-supplied host into a validated IP (issue #21)."""

from collections import Counter

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuthEvent
from app.models.enums import AuthEventKind
from app.net import client_ip


def _request(host: str | None) -> Request:
    scope = {"type": "http", "client": (host, 12345) if host is not None else None, "headers": []}
    return Request(scope)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("203.0.113.5", "203.0.113.5"),
        ("2001:db8::1", "2001:db8::1"),
        ("fe80::1%eth0", "fe80::1"),  # zone id stripped
        ("not-an-ip", None),
        ("a" * 46, None),  # too long to be a valid IP at all
        (None, None),  # no client in the ASGI scope
        ("testclient", None),  # TestClient's default host isn't an IP
    ],
)
def test_client_ip(host: str | None, expected: str | None) -> None:
    assert client_ip(_request(host)) == expected


def test_malformed_forwarded_ip_stores_null_and_does_not_500(
    db_client: TestClient, db_session: Session, app: FastAPI
) -> None:
    garbage = TestClient(app, client=("garbage-forwarded-for-value", 12345))
    r = garbage.post("/api/auth/enroll/options", json={"code": "AAAA-BBBB-CCCC"})
    assert r.status_code == 401

    event = db_session.scalars(select(AuthEvent)).one()
    assert event.ip is None
    assert Counter([(event.kind, event.user_id)]) == Counter([(AuthEventKind.enroll_failed, None)])
