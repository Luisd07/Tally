"""Test fixtures.

Tests that only touch HTTP use `client`. It needs no database, because the engine never
connects until a query runs. Tests that touch the database use `db_session` or `db_client`;
those need Postgres at TEST_DATABASE_URL. Locally:

    docker compose -f docker-compose.yml -f compose.dev.yml up -d db

The test database is dropped, re-created and migrated to head once per run, and every
test runs inside a transaction that is rolled back afterwards.
"""

import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from app.auth import tokens
from app.auth.ratelimit import storage as rate_limit_storage
from app.config import Settings
from app.db import get_db, make_engine
from app.main import create_app
from app.models import User
from app.services import ai
from tests.softauthn import SoftAuthenticator

API_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://tally:change-me@127.0.0.1:5433/tally_test"
)


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> None:
    """Every test starts with empty rate-limit counters, shared process-wide otherwise."""
    rate_limit_storage.reset()


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test resolves an outside host: every non-loopback name answers with a fixed
    public address. Tests that probe the resolver override `socket.getaddrinfo` themselves.
    """
    real = socket.getaddrinfo
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    def resolve(host: object, *args: Any, **kwargs: Any) -> Any:
        if host in (None, "localhost", "127.0.0.1", "::1"):
            return real(host, *args, **kwargs)
        return public

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture(autouse=True)
def _no_real_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test builds a real Anthropic client. Tests that exercise AI stub `make_client`."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tests must stub ai.make_client")

    monkeypatch.setattr(ai, "make_client", refuse)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        environment="test",
        log_level="WARNING",
        database_url=SecretStr(TEST_DATABASE_URL),
        anthropic_api_key=SecretStr(""),
        anthropic_federation_rule_id="",
        anthropic_organization_id="",
        jwt_signing_key=SecretStr("test-signing-key-" + "x" * 32),
        simplefin_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _recreate_database(url: str) -> None:
    target = make_url(url)
    admin = make_engine_for(target.set(database="postgres").render_as_string(hide_password=False))
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    except OperationalError as exc:
        pytest.fail(
            f"Test database unreachable at {target.render_as_string()}. Start it with:\n"
            "  docker compose -f docker-compose.yml -f compose.dev.yml up -d db\n"
            f"({exc.orig})",
            pytrace=False,
        )
    finally:
        admin.dispose()


def make_engine_for(url: str) -> Engine:
    return make_engine(Settings(environment="test", database_url=SecretStr(url))).execution_options(
        isolation_level="AUTOCOMMIT"
    )


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    _recreate_database(TEST_DATABASE_URL)
    engine = make_engine(settings)
    cfg = Config(API_DIR / "alembic.ini")
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    with engine.connect() as conn:
        outer = conn.begin()
        session = Session(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            session.close()
            outer.rollback()


@pytest.fixture
def db_client(app: FastAPI, db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def user(db_session: Session) -> User:
    u = User(id=uuid.uuid4(), display_name="Test User")
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def auth_headers(user: User, settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.create_access_token(user.id, settings)}"}


@pytest.fixture
def other_user(db_session: Session) -> User:
    """A second user, for tests that check a row owned by someone else."""
    u = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def authenticator(settings: Settings) -> SoftAuthenticator:
    """A software passkey for the test settings' RP ID and origin."""
    return SoftAuthenticator(settings.webauthn_rp_id, settings.webauthn_origin)
