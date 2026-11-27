"""Refresh token rotation, reuse detection and sign-out (issue #19)."""

import contextlib
import secrets
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.orm import Session

from app.auth import tokens
from app.config import Settings
from app.models import AuthEvent, RefreshToken, User
from app.models.enums import AuthEventKind
from tests.locks import wait_until_blocked_on_lock

REFRESH = "/api/auth/refresh"
SIGNOUT = "/api/auth/signout"


def _events(db_session: Session) -> Counter[tuple[AuthEventKind, uuid.UUID | None]]:
    # Unordered: created_at is the same for every row inside the test's one transaction.
    return Counter((e.kind, e.user_id) for e in db_session.scalars(select(AuthEvent)))


def _row(db_session: Session, token: str) -> RefreshToken:
    return db_session.scalars(
        select(RefreshToken).where(RefreshToken.token_hash == tokens.hash_refresh_token(token))
    ).one()


def _assert_unauthorized(status_code: int, body: dict[str, Any]) -> None:
    assert status_code == 401
    error = body["error"]
    assert (error["code"], error["message"]) == ("unauthorized", "Sign-in required.")


def test_refresh_rotates_the_token(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    before = datetime.now(UTC)
    token = tokens.issue_refresh_token(db_session, user.id, settings, before)
    old = _row(db_session, token)

    r = db_client.post(REFRESH, json={"refresh_token": token})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["refresh_token"] != token
    assert tokens.decode_access_token(body["access_token"], settings) == user.id

    db_session.refresh(old)
    assert old.revoked_at is not None

    new = _row(db_session, body["refresh_token"])
    assert new.family_id == old.family_id
    assert new.revoked_at is None
    lifetime = timedelta(days=settings.refresh_token_days)
    assert before + lifetime <= new.expires_at <= datetime.now(UTC) + lifetime
    assert _events(db_session) == Counter([(AuthEventKind.refresh, user.id)])


def test_replayed_refresh_token_revokes_the_whole_family(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    token = tokens.issue_refresh_token(db_session, user.id, settings, datetime.now(UTC))

    first = db_client.post(REFRESH, json={"refresh_token": token})
    assert first.status_code == 200, first.text
    new_token = first.json()["refresh_token"]

    replayed = db_client.post(REFRESH, json={"refresh_token": token})
    _assert_unauthorized(replayed.status_code, replayed.json())

    # The newest token in the family is dead too.
    again = db_client.post(REFRESH, json={"refresh_token": new_token})
    _assert_unauthorized(again.status_code, again.json())

    family_id = _row(db_session, token).family_id
    rows = db_session.scalars(select(RefreshToken).where(RefreshToken.family_id == family_id))
    assert all(row.revoked_at is not None for row in rows)
    assert _events(db_session)[(AuthEventKind.refresh_reuse, user.id)] >= 1


def test_expired_refresh_token_is_rejected(
    db_client: TestClient, db_session: Session, user: User
) -> None:
    token, token_hash = tokens.new_refresh_token()
    db_session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            family_id=uuid.uuid4(),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    db_session.flush()

    r = db_client.post(REFRESH, json={"refresh_token": token})
    _assert_unauthorized(r.status_code, r.json())
    assert _events(db_session) == Counter([(AuthEventKind.refresh_failed, None)])


def test_unknown_refresh_token_is_rejected(db_client: TestClient, db_session: Session) -> None:
    r = db_client.post(REFRESH, json={"refresh_token": secrets.token_urlsafe(48)})
    _assert_unauthorized(r.status_code, r.json())
    assert _events(db_session) == Counter([(AuthEventKind.refresh_failed, None)])


@pytest.mark.parametrize("path", [REFRESH, SIGNOUT])
def test_over_length_refresh_token_is_rejected(client: TestClient, path: str) -> None:
    r = client.post(path, json={"refresh_token": "x" * 129})
    assert r.status_code == 422


def test_signout_revokes_the_family_and_refresh_then_fails(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    token = tokens.issue_refresh_token(db_session, user.id, settings, datetime.now(UTC))

    r = db_client.post(SIGNOUT, json={"refresh_token": token})
    assert r.status_code == 204
    assert r.content == b""

    assert _row(db_session, token).revoked_at is not None
    assert _events(db_session) == Counter([(AuthEventKind.sign_out, user.id)])

    again = db_client.post(REFRESH, json={"refresh_token": token})
    _assert_unauthorized(again.status_code, again.json())


def test_signout_with_unknown_token_is_idempotent(
    db_client: TestClient, db_session: Session
) -> None:
    r = db_client.post(SIGNOUT, json={"refresh_token": secrets.token_urlsafe(48)})
    assert r.status_code == 204
    assert _events(db_session) == Counter()


def test_signout_twice_with_the_same_token_is_idempotent(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    token = tokens.issue_refresh_token(db_session, user.id, settings, datetime.now(UTC))

    first = db_client.post(SIGNOUT, json={"refresh_token": token})
    assert first.status_code == 204
    second = db_client.post(SIGNOUT, json={"refresh_token": token})
    assert second.status_code == 204

    assert _events(db_session) == Counter([(AuthEventKind.sign_out, user.id)])


def test_other_families_are_unaffected_by_reuse(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    now = datetime.now(UTC)
    token_a = tokens.issue_refresh_token(db_session, user.id, settings, now)
    token_b = tokens.issue_refresh_token(db_session, user.id, settings, now)

    db_client.post(REFRESH, json={"refresh_token": token_a})
    db_client.post(REFRESH, json={"refresh_token": token_a})  # replay: revokes family A only

    still_good = db_client.post(REFRESH, json={"refresh_token": token_b})
    assert still_good.status_code == 200, still_good.text


def test_other_families_are_unaffected_by_sign_out(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    now = datetime.now(UTC)
    token_a = tokens.issue_refresh_token(db_session, user.id, settings, now)
    token_b = tokens.issue_refresh_token(db_session, user.id, settings, now)

    r = db_client.post(SIGNOUT, json={"refresh_token": token_a})
    assert r.status_code == 204

    still_good = db_client.post(REFRESH, json={"refresh_token": token_b})
    assert still_good.status_code == 200, still_good.text


def test_concurrent_refresh_lets_only_one_rotation_succeed(
    engine: Engine, settings: Settings
) -> None:
    """Needs committed rows, so it runs outside the rolled-back test transaction.

    Two sessions present the same refresh token at once. The first's UPDATE holds the
    row's lock until it commits; the second then finds the token already revoked. With no
    grace window, that counts as reuse: `rotate_refresh_token` raises `RefreshReused`.
    """
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Test User"))
        setup.flush()
        token = tokens.issue_refresh_token(setup, user_id, settings, now)
        setup.commit()

    try:
        with Session(engine) as first, Session(engine) as second:
            tokens.rotate_refresh_token(first, token, settings, now)

            second_pid = second.scalar(text("SELECT pg_backend_pid()"))
            assert second_pid is not None
            errors: list[BaseException] = []

            def race() -> None:
                try:
                    tokens.rotate_refresh_token(second, token, settings, now)
                except tokens.RefreshReused as exc:
                    errors.append(exc)

            racer = threading.Thread(target=race)
            racer.start()
            wait_until_blocked_on_lock(engine, second_pid)
            first.commit()
            racer.join(timeout=10)

            assert len(errors) == 1
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def _replay_via_refresh(db: Session, token: str, settings: Settings, now: datetime) -> None:
    with contextlib.suppress(tokens.RefreshReused):
        tokens.rotate_refresh_token(db, token, settings, now)


def _replay_via_signout(db: Session, token: str, settings: Settings, now: datetime) -> None:
    tokens.revoke_refresh_token_family(db, token, now)


@pytest.mark.parametrize(
    "replay", [_replay_via_refresh, _replay_via_signout], ids=["refresh", "signout"]
)
def test_family_lock_serializes_rotation_and_revocation(
    engine: Engine,
    settings: Settings,
    replay: Callable[[Session, str, Settings, datetime], None],
) -> None:
    """Regression for MV-1.

    Under READ COMMITTED, a revocation's `UPDATE ... WHERE family_id = :f` only re-checks
    rows that existed when the statement started, so a token a concurrent rotation inserts
    after that start is invisible to it. `pg_advisory_xact_lock` keyed on the family closes
    that window: rotation and revocation take the same lock before touching any row in the
    family, so one blocks until the other commits.

    T0 -> T1 (committed). A thief rotates T1 to T2 without committing. Meanwhile the victim
    replays T0, either through `/refresh` or sign-out (parametrized). The replay must block
    until the thief commits; afterwards every row in the family, including T2, is revoked,
    and T2 can no longer rotate either.
    """
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Test User"))
        setup.flush()
        t0 = tokens.issue_refresh_token(setup, user_id, settings, now)
        setup.commit()
    with Session(engine) as setup:
        t1 = tokens.rotate_refresh_token(setup, t0, settings, now).session.refresh_token
        setup.commit()

    try:
        with Session(engine) as thief, Session(engine) as victim:
            t2 = tokens.rotate_refresh_token(thief, t1, settings, now).session.refresh_token

            victim_pid = victim.scalar(text("SELECT pg_backend_pid()"))
            assert victim_pid is not None

            racer = threading.Thread(target=replay, args=(victim, t0, settings, now))
            racer.start()
            wait_until_blocked_on_lock(engine, victim_pid)
            thief.commit()
            racer.join(timeout=10)
            victim.commit()

        with Session(engine) as check:
            family_id = _row(check, t2).family_id
            rows = check.scalars(select(RefreshToken).where(RefreshToken.family_id == family_id))
            assert all(row.revoked_at is not None for row in rows)

        with Session(engine) as retry, pytest.raises(tokens.RefreshRejected):
            tokens.rotate_refresh_token(retry, t2, settings, datetime.now(UTC))
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_replay_revocation_and_event_are_committed_without_a_shared_session(
    client: TestClient, engine: Engine, settings: Settings
) -> None:
    """Regression for UB-2.

    `db_client` shares one session with the app, so a dropped `db.commit()` in the router
    would still show its effects to `db_session` without the change ever reaching Postgres.
    This goes through the app's real per-request session (plain `client`, default `get_db`)
    and then checks the database from a brand-new session, so a missing commit shows up as
    missing data instead of staying invisible.
    """
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Test User"))
        setup.flush()
        token = tokens.issue_refresh_token(setup, user_id, settings, now)
        setup.commit()

    try:
        first = client.post(REFRESH, json={"refresh_token": token})
        assert first.status_code == 200, first.text

        replayed = client.post(REFRESH, json={"refresh_token": token})
        _assert_unauthorized(replayed.status_code, replayed.json())

        with Session(engine) as fresh:
            family_id = _row(fresh, token).family_id
            rows = fresh.scalars(select(RefreshToken).where(RefreshToken.family_id == family_id))
            assert all(row.revoked_at is not None for row in rows)
            reuse_events = fresh.scalars(
                select(AuthEvent).where(
                    AuthEvent.kind == AuthEventKind.refresh_reuse, AuthEvent.user_id == user_id
                )
            ).all()
            assert len(reuse_events) == 1
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(AuthEvent).where(AuthEvent.user_id == user_id))
            cleanup.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
