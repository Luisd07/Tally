import threading
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, delete, select, text, update
from sqlalchemy.orm import Session
from webauthn.helpers import bytes_to_base64url

from app.auth import tokens
from app.auth.enrollment import issue_code
from app.auth.errors import CeremonyFailed
from app.auth.passkeys import (
    RelyingParty,
    SignInAttempt,
    finish_sign_in,
    start_sign_in,
)
from app.config import Settings
from app.models import AuthEvent, Passkey, RefreshToken, User, WebAuthnChallenge
from app.models.enums import AuthEventKind
from tests.locks import wait_until_blocked_on_lock
from tests.softauthn import FLAG_UP, SoftAuthenticator

OPTIONS = "/api/auth/signin/options"
VERIFY = "/api/auth/signin/verify"


@pytest.fixture
def passkey(db_session: Session, user: User, authenticator: SoftAuthenticator) -> Passkey:
    row = Passkey(
        user_id=user.id,
        credential_id=authenticator.credential_id,
        public_key=authenticator.cose_public_key,
        sign_count=0,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _events(db_session: Session) -> Counter[tuple[AuthEventKind, uuid.UUID | None]]:
    # Unordered: created_at is the same for every row inside the test's one transaction.
    return Counter((e.kind, e.user_id) for e in db_session.scalars(select(AuthEvent)))


def _start(db_client: TestClient) -> dict[str, Any]:
    r = db_client.post(OPTIONS)
    assert r.status_code == 200, r.text
    return r.json()


def _verify(db_client: TestClient, challenge_id: str, credential: dict[str, Any]) -> Response:
    return db_client.post(VERIFY, json={"challenge_id": challenge_id, "credential": credential})


def _sign_in(
    db_client: TestClient, authenticator: SoftAuthenticator, user: User, **kwargs: Any
) -> Response:
    started = _start(db_client)
    credential = authenticator.sign_in(started["options"], user_handle=user.id.bytes, **kwargs)
    return _verify(db_client, started["challenge_id"], credential)


def _assert_refused(r: Response) -> None:
    assert r.status_code == 401
    error = r.json()["error"]
    assert (error["code"], error["message"]) == ("unauthorized", "Sign-in required.")


def _assert_no_tokens(db_session: Session) -> None:
    assert db_session.scalars(select(RefreshToken)).all() == []


def test_options_ask_for_any_discoverable_passkey(
    db_client: TestClient, db_session: Session, settings: Settings
) -> None:
    started = _start(db_client)
    options = started["options"]
    assert options["rpId"] == settings.webauthn_rp_id
    assert options["allowCredentials"] == []
    assert options["userVerification"] == "required"
    assert db_session.get_one(WebAuthnChallenge, uuid.UUID(started["challenge_id"])).user_id is None


def test_valid_sign_in_returns_tokens(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
    settings: Settings,
) -> None:
    before = datetime.now(UTC)
    r = _sign_in(db_client, authenticator, user)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()

    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_minutes * 60
    assert tokens.decode_access_token(body["access_token"], settings) == user.id

    stored = db_session.scalars(select(RefreshToken)).one()
    assert stored.user_id == user.id
    assert stored.token_hash == tokens.hash_refresh_token(body["refresh_token"])
    assert stored.revoked_at is None
    lifetime = timedelta(days=settings.refresh_token_days)
    assert before + lifetime <= stored.expires_at <= datetime.now(UTC) + lifetime

    db_session.refresh(passkey)
    assert passkey.sign_count == 1
    assert _events(db_session) == Counter([(AuthEventKind.sign_in, user.id)])
    assert db_session.scalars(select(WebAuthnChallenge)).all() == []


def test_each_sign_in_starts_a_new_token_family(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    first = _sign_in(db_client, authenticator, user).json()
    second = _sign_in(db_client, authenticator, user).json()
    assert first["refresh_token"] != second["refresh_token"]

    families = db_session.scalars(select(RefreshToken.family_id)).all()
    assert len(families) == len(set(families)) == 2
    db_session.refresh(passkey)
    assert passkey.sign_count == 2


def test_passkey_without_a_counter_signs_in_repeatedly(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    for _ in range(2):
        assert _sign_in(db_client, authenticator, user, counter_step=0).status_code == 200
    db_session.refresh(passkey)
    assert passkey.sign_count == 0


def test_sign_in_without_user_handle_is_accepted(
    db_client: TestClient, user: User, passkey: Passkey, authenticator: SoftAuthenticator
) -> None:
    started = _start(db_client)
    credential = authenticator.sign_in(started["options"], user_handle=None)
    assert _verify(db_client, started["challenge_id"], credential).status_code == 200


def test_replayed_challenge_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    started = _start(db_client)
    credential = authenticator.sign_in(started["options"], user_handle=user.id.bytes)
    assert _verify(db_client, started["challenge_id"], credential).status_code == 200
    _assert_refused(_verify(db_client, started["challenge_id"], credential))

    assert len(db_session.scalars(select(RefreshToken)).all()) == 1
    assert _events(db_session) == Counter(
        [(AuthEventKind.sign_in, user.id), (AuthEventKind.sign_in_failed, None)]
    )


def test_bad_signature_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    impostor_key = ec.generate_private_key(ec.SECP256R1())
    _assert_refused(_sign_in(db_client, authenticator, user, signing_key=impostor_key))

    _assert_no_tokens(db_session)
    db_session.refresh(passkey)
    assert passkey.sign_count == 0
    assert _events(db_session) == Counter([(AuthEventKind.sign_in_failed, user.id)])


def test_unknown_credential_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    settings: Settings,
) -> None:
    stranger = SoftAuthenticator(settings.webauthn_rp_id, settings.webauthn_origin)
    _assert_refused(_sign_in(db_client, stranger, user))

    _assert_no_tokens(db_session)
    assert _events(db_session) == Counter([(AuthEventKind.sign_in_failed, None)])


@pytest.mark.parametrize(
    "tamper",
    [
        {"challenge": bytes_to_base64url(b"some other challenge")},
        {"origin": "https://evil.example"},
        {"flags": FLAG_UP},  # no user verification
        {"counter_step": 0},  # counter went backwards: a cloned authenticator
    ],
    ids=["wrong-challenge", "wrong-origin", "not-user-verified", "stale-counter"],
)
def test_bad_assertion_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
    tamper: dict[str, Any],
) -> None:
    passkey.sign_count = 5
    authenticator.sign_count = 5
    db_session.flush()

    _assert_refused(_sign_in(db_client, authenticator, user, **tamper))
    _assert_no_tokens(db_session)
    db_session.refresh(passkey)
    assert passkey.sign_count == 5
    assert _events(db_session) == Counter([(AuthEventKind.sign_in_failed, user.id)])


def test_mismatched_user_handle_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    started = _start(db_client)
    credential = authenticator.sign_in(started["options"], user_handle=uuid.uuid4().bytes)
    _assert_refused(_verify(db_client, started["challenge_id"], credential))
    _assert_no_tokens(db_session)
    assert _events(db_session) == Counter([(AuthEventKind.sign_in_failed, user.id)])


@pytest.mark.parametrize(
    "credential",
    [
        {"id": "nope"},
        {
            "id": "AA",
            "rawId": "AA",
            "type": "public-key",
            "response": {
                "clientDataJSON": "AA",
                "authenticatorData": "AA",
                "signature": "AA",
                "userHandle": "a",  # not valid base64url
            },
        },
    ],
    ids=["missing-fields", "bad-base64"],
)
def test_malformed_assertion_is_refused(
    db_client: TestClient, db_session: Session, credential: dict[str, Any]
) -> None:
    started = _start(db_client)
    _assert_refused(_verify(db_client, started["challenge_id"], credential))
    assert _events(db_session) == Counter([(AuthEventKind.sign_in_failed, None)])


def test_expired_challenge_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    started = _start(db_client)
    db_session.execute(
        update(WebAuthnChallenge).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    credential = authenticator.sign_in(started["options"], user_handle=user.id.bytes)
    _assert_refused(_verify(db_client, started["challenge_id"], credential))
    _assert_no_tokens(db_session)
    assert _events(db_session) == Counter([(AuthEventKind.sign_in_failed, None)])


def test_enrollment_challenge_cannot_sign_in(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    code = issue_code(db_session, user.id, datetime.now(UTC))
    enrolling = db_client.post("/api/auth/enroll/options", json={"code": code}).json()
    credential = authenticator.sign_in(enrolling["options"], user_handle=user.id.bytes)
    _assert_refused(_verify(db_client, enrolling["challenge_id"], credential))
    _assert_no_tokens(db_session)


def test_enroll_then_sign_in(
    db_client: TestClient,
    db_session: Session,
    user: User,
    authenticator: SoftAuthenticator,
    settings: Settings,
) -> None:
    code = issue_code(db_session, user.id, datetime.now(UTC))
    enrolling = db_client.post("/api/auth/enroll/options", json={"code": code}).json()
    r = db_client.post(
        "/api/auth/enroll/verify",
        json={
            "challenge_id": enrolling["challenge_id"],
            "credential": authenticator.register(enrolling["options"]),
        },
    )
    assert r.status_code == 201, r.text

    r = _sign_in(db_client, authenticator, user)
    assert r.status_code == 200, r.text
    assert tokens.decode_access_token(r.json()["access_token"], settings) == user.id
    assert _events(db_session) == Counter(
        [(AuthEventKind.enroll, user.id), (AuthEventKind.sign_in, user.id)]
    )


def test_concurrent_sign_ins_let_only_one_pass_the_counter_check(
    engine: Engine, settings: Settings, authenticator: SoftAuthenticator
) -> None:
    """Needs committed rows, so it runs outside the rolled-back test transaction.

    Two sessions race to sign in with assertions that carry the same counter value. The
    first to reach the counter check holds the passkey row's lock until it commits; the
    second then sees the now-current counter and is refused as stale. If `with_for_update`
    is dropped, the second session reads the stale counter concurrently and also succeeds.
    """
    user_id = uuid.uuid4()
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Test User"))
        setup.flush()
        setup.add(
            Passkey(
                user_id=user_id,
                credential_id=authenticator.credential_id,
                public_key=authenticator.cose_public_key,
                sign_count=5,
            )
        )
        setup.commit()
    authenticator.sign_count = 5

    rp = RelyingParty.from_settings(settings)
    now = datetime.now(UTC)

    try:
        with Session(engine) as first, Session(engine) as second:
            pending_a = start_sign_in(first, rp, now)
            credential_a = authenticator.sign_in(
                pending_a.options, user_handle=user_id.bytes, counter_step=1
            )
            finish_sign_in(first, rp, SignInAttempt(pending_a.challenge_id, credential_a), now)

            authenticator.sign_count = 5  # same starting point, so the next assertion matches
            pending_b = start_sign_in(second, rp, now)
            credential_b = authenticator.sign_in(
                pending_b.options, user_handle=user_id.bytes, counter_step=1
            )

            second_pid = second.scalar(text("SELECT pg_backend_pid()"))
            assert second_pid is not None
            errors: list[BaseException] = []

            def race() -> None:
                try:
                    finish_sign_in(
                        second, rp, SignInAttempt(pending_b.challenge_id, credential_b), now
                    )
                except CeremonyFailed as exc:
                    errors.append(exc)

            racer = threading.Thread(target=race)
            racer.start()
            wait_until_blocked_on_lock(engine, second_pid)
            first.commit()
            racer.join(timeout=10)

            assert len(errors) == 1
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(Passkey).where(Passkey.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
