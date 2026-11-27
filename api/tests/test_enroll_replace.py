"""Re-enrollment revokes old passkeys: scripts/enroll.py --replace (issue #94)."""

import os
import threading
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import scripts.enroll as enroll_script
from app.auth import events, tokens
from app.auth.enrollment import issue_code_for_owner, revoke_credentials
from app.config import Settings
from app.models import AuthEvent, EnrollmentCode, Passkey, RefreshToken, User
from app.models.enums import AuthEventKind
from tests.locks import wait_until_blocked_on_lock
from tests.softauthn import SoftAuthenticator

REFRESH = "/api/auth/refresh"
SIGNIN_OPTIONS = "/api/auth/signin/options"
SIGNIN_VERIFY = "/api/auth/signin/verify"


def _passkey(user: User, authenticator: SoftAuthenticator) -> Passkey:
    return Passkey(
        user_id=user.id,
        credential_id=authenticator.credential_id,
        public_key=authenticator.cose_public_key,
        sign_count=0,
    )


def _row(db_session: Session, token: str) -> RefreshToken:
    return db_session.scalars(
        select(RefreshToken).where(RefreshToken.token_hash == tokens.hash_refresh_token(token))
    ).one()


def _assert_unauthorized(status_code: int, body: dict) -> None:
    assert status_code == 401
    error = body["error"]
    assert (error["code"], error["message"]) == ("unauthorized", "Sign-in required.")


def test_revoke_credentials_deletes_passkeys_and_revokes_tokens(
    db_session: Session, user: User, settings: Settings, authenticator: SoftAuthenticator
) -> None:
    now = datetime.now(UTC)
    db_session.add(_passkey(user, authenticator))
    token = tokens.issue_refresh_token(db_session, user.id, settings, now)
    db_session.flush()

    passkeys_deleted, tokens_revoked = revoke_credentials(db_session, user.id, now)

    assert (passkeys_deleted, tokens_revoked) == (1, 1)
    assert db_session.scalars(select(Passkey)).all() == []
    assert _row(db_session, token).revoked_at == now


def test_other_users_credentials_are_untouched(
    db_session: Session, user: User, settings: Settings, authenticator: SoftAuthenticator
) -> None:
    now = datetime.now(UTC)
    other = User(display_name="Someone Else")
    db_session.add(other)
    db_session.flush()
    db_session.add(_passkey(other, authenticator))
    other_token = tokens.issue_refresh_token(db_session, other.id, settings, now)
    db_session.flush()

    passkeys_deleted, tokens_revoked = revoke_credentials(db_session, user.id, now)

    assert (passkeys_deleted, tokens_revoked) == (0, 0)
    assert db_session.scalars(select(Passkey)).one().user_id == other.id
    assert _row(db_session, other_token).revoked_at is None


def test_without_replace_nothing_is_revoked(
    db_session: Session, user: User, settings: Settings, authenticator: SoftAuthenticator
) -> None:
    """`issue_code_for_owner` has no revocation path at all: it only ever issues a code."""
    now = datetime.now(UTC)
    db_session.add(_passkey(user, authenticator))
    token = tokens.issue_refresh_token(db_session, user.id, settings, now)
    db_session.flush()

    issue_code_for_owner(db_session, "Ignored", now)

    assert len(db_session.scalars(select(Passkey)).all()) == 1
    assert _row(db_session, token).revoked_at is None


def test_old_refresh_token_is_rejected_after_replace(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    now = datetime.now(UTC)
    token = tokens.issue_refresh_token(db_session, user.id, settings, now)

    client = events.ClientInfo(ip=None, user_agent="scripts.enroll")
    events.replace_owner_credentials(db_session, client, user.id, now)

    r = db_client.post(REFRESH, json={"refresh_token": token})
    _assert_unauthorized(r.status_code, r.json())
    assert _row(db_session, token).revoked_at is not None


def test_old_passkey_cannot_sign_in_after_replace(
    db_client: TestClient,
    db_session: Session,
    user: User,
    authenticator: SoftAuthenticator,
) -> None:
    now = datetime.now(UTC)
    db_session.add(_passkey(user, authenticator))
    db_session.flush()

    client = events.ClientInfo(ip=None, user_agent="scripts.enroll")
    events.replace_owner_credentials(db_session, client, user.id, now)

    started = db_client.post(SIGNIN_OPTIONS).json()
    credential = authenticator.sign_in(started["options"], user_handle=user.id.bytes)
    r = db_client.post(
        SIGNIN_VERIFY, json={"challenge_id": started["challenge_id"], "credential": credential}
    )
    _assert_unauthorized(r.status_code, r.json())
    assert db_session.scalars(select(Passkey)).all() == []


def test_replace_is_atomic_with_code_issuance(engine: Engine, settings: Settings) -> None:
    """Regression: a failure issuing the code must not let the revocation survive either.

    Simulates the failure with a real constraint violation (a bad FK) after
    `revoke_credentials` has already run in the session. Closing the session without
    `commit()`, exactly as `scripts/enroll.py` does when an exception propagates, rolls
    the whole transaction back.
    """
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Test User"))
        setup.flush()
        setup.add(
            Passkey(user_id=user_id, credential_id=os.urandom(32), public_key=b"key", sign_count=0)
        )
        tokens.issue_refresh_token(setup, user_id, settings, now)
        setup.commit()

    try:
        with Session(engine) as db:
            revoke_credentials(db, user_id, now)
            with pytest.raises(IntegrityError):
                db.add(EnrollmentCode(user_id=uuid.uuid4(), code_hash="x" * 64, expires_at=now))
                db.flush()
        # The session closed without committing: nothing above should have landed.
        with Session(engine) as check:
            assert len(check.scalars(select(Passkey).where(Passkey.user_id == user_id)).all()) == 1
            row = check.scalars(select(RefreshToken).where(RefreshToken.user_id == user_id)).one()
            assert row.revoked_at is None
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
            cleanup.execute(delete(Passkey).where(Passkey.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_family_lock_serializes_replace_and_rotation(engine: Engine, settings: Settings) -> None:
    """Regression for the issue's concurrency note.

    T0 -> T1 (committed, same family). A thief rotates T1 to T2 without committing.
    Meanwhile `revoke_credentials` (re-enrollment with --replace) tries to revoke the
    owner's credentials. It must block on the same per-family advisory lock
    `rotate_refresh_token` takes, rather than reading a stale, pre-rotation set of rows;
    once the thief commits, every row in the family, including T2, ends up revoked.
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
            result: list[tuple[int, int]] = []

            def replace() -> None:
                result.append(revoke_credentials(victim, user_id, now))

            racer = threading.Thread(target=replace)
            racer.start()
            wait_until_blocked_on_lock(engine, victim_pid)
            thief.commit()
            racer.join(timeout=10)
            victim.commit()

            assert result == [(0, 1)]  # no passkeys; T2, the only still-live token, revoked

        with Session(engine) as check:
            family_id = _row(check, t2).family_id
            rows = check.scalars(select(RefreshToken).where(RefreshToken.family_id == family_id))
            assert all(row.revoked_at is not None for row in rows)
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_replace_blocks_on_a_concurrent_sign_ins_passkey_lock(
    engine: Engine, settings: Settings, authenticator: SoftAuthenticator
) -> None:
    """Regression for MV-2.

    `revoke_credentials` deletes passkeys before it reads which refresh-token families are
    still live, precisely so this race can't happen: tx A locks the owner's passkey row
    `FOR UPDATE`, the way `finish_sign_in` does, and inserts a new refresh token under a
    fresh family (`issue_session`'s new-family-per-sign-in), without committing. A thread
    running `revoke_credentials` must block on A's row lock at the passkey DELETE, rather
    than reading families first and missing A's not-yet-committed family. Once A commits,
    the delete proceeds and the family lookup that follows sees, and revokes, A's token.
    """
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Test User"))
        setup.flush()
        setup.add(
            Passkey(
                user_id=user_id,
                credential_id=authenticator.credential_id,
                public_key=authenticator.cose_public_key,
                sign_count=0,
            )
        )
        setup.commit()

    try:
        with Session(engine) as signer, Session(engine) as victim:
            signer.scalars(
                select(Passkey).where(Passkey.user_id == user_id).with_for_update()
            ).one()
            new_token = tokens.issue_session(signer, user_id, settings, now).refresh_token

            victim_pid = victim.scalar(text("SELECT pg_backend_pid()"))
            assert victim_pid is not None
            result: list[tuple[int, int]] = []

            def replace() -> None:
                result.append(revoke_credentials(victim, user_id, now))

            racer = threading.Thread(target=replace)
            racer.start()
            wait_until_blocked_on_lock(engine, victim_pid)
            signer.commit()
            racer.join(timeout=10)
            victim.commit()

            assert result == [(1, 1)]

        with Session(engine) as check:
            assert check.scalars(select(Passkey).where(Passkey.user_id == user_id)).all() == []
            assert _row(check, new_token).revoked_at is not None
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
            cleanup.execute(delete(Passkey).where(Passkey.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_main_replace_revokes_and_records_event(
    engine: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script itself: counts printed, an auth event recorded, old credential dead."""
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Existing"))
        setup.flush()
        setup.add(
            Passkey(user_id=user_id, credential_id=os.urandom(32), public_key=b"key", sign_count=0)
        )
        tokens.issue_refresh_token(setup, user_id, settings, now)
        setup.commit()

    monkeypatch.setattr(enroll_script, "get_settings", lambda: settings)
    monkeypatch.setattr(enroll_script, "make_engine", lambda _settings: engine)
    monkeypatch.setattr(engine, "dispose", lambda: None)  # the shared test engine outlives main()
    monkeypatch.setattr("sys.argv", ["enroll.py", "--name", "Ignored", "--replace"])

    try:
        enroll_script.main()
        out = capsys.readouterr().out
        assert "Revoked 1 passkey(s) and 1 refresh token(s)." in out
        assert "Enrollment code:" in out

        with Session(engine) as check:
            assert check.scalars(select(Passkey).where(Passkey.user_id == user_id)).all() == []
            row = check.scalars(select(RefreshToken).where(RefreshToken.user_id == user_id)).one()
            assert row.revoked_at is not None
            event = check.scalars(select(AuthEvent).where(AuthEvent.user_id == user_id)).one()
            assert event.kind == AuthEventKind.passkeys_replaced
            assert event.ip is None
            assert event.user_agent == "scripts.enroll"
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(AuthEvent).where(AuthEvent.user_id == user_id))
            cleanup.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
            cleanup.execute(delete(Passkey).where(Passkey.user_id == user_id))
            cleanup.execute(delete(EnrollmentCode).where(EnrollmentCode.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
