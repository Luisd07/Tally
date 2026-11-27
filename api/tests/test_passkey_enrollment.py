import threading
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, text, update
from sqlalchemy.orm import Session
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from app.auth.enrollment import issue_code
from app.auth.errors import CeremonyFailed
from app.auth.events import USER_AGENT_MAX, ClientInfo, record
from app.auth.passkeys import NewChallenge, _store_challenge, consume_challenge
from app.config import Settings
from app.models import AuthEvent, EnrollmentCode, Passkey, User, WebAuthnChallenge
from app.models.enums import AuthEventKind, ChallengePurpose
from tests.locks import wait_until_blocked_on_lock
from tests.softauthn import FLAG_AT, FLAG_UP, SoftAuthenticator

OPTIONS = "/api/auth/enroll/options"
VERIFY = "/api/auth/enroll/verify"


@pytest.fixture
def code(db_session: Session, user: User) -> str:
    return issue_code(db_session, user.id, datetime.now(UTC))


def _events(db_session: Session) -> Counter[tuple[AuthEventKind, uuid.UUID | None]]:
    # Unordered: created_at is the same for every row inside the test's one transaction.
    return Counter((e.kind, e.user_id) for e in db_session.scalars(select(AuthEvent)))


def _start(db_client: TestClient, code: str) -> dict[str, Any]:
    r = db_client.post(OPTIONS, json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()


def _assert_refused(r: Any) -> None:
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert r.json()["error"]["message"] == "Sign-in required."


def _enroll(
    db_client: TestClient, code: str, authenticator: SoftAuthenticator
) -> tuple[dict[str, Any], Any]:
    """Run a full enrollment ceremony and return its options and the verify response."""
    started = _start(db_client, code)
    r = db_client.post(
        VERIFY,
        json={
            "challenge_id": started["challenge_id"],
            "credential": authenticator.register(started["options"]),
            "device_name": "Luis's iPhone",
        },
        headers={"User-Agent": "Tally/1.0 iOS"},
    )
    return started["options"], r


def test_options_require_discoverable_uv_credential(
    db_client: TestClient, user: User, code: str, settings: Settings
) -> None:
    options = _start(db_client, code)["options"]
    assert options["rp"] == {"id": settings.webauthn_rp_id, "name": settings.webauthn_rp_name}
    assert base64url_to_bytes(options["user"]["id"]) == user.id.bytes
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"


def test_valid_enrollment_stores_passkey(
    db_client: TestClient,
    db_session: Session,
    user: User,
    code: str,
    authenticator: SoftAuthenticator,
) -> None:
    _, r = _enroll(db_client, code, authenticator)
    assert r.status_code == 201, r.text
    assert r.json()["device_name"] == "Luis's iPhone"

    passkey = db_session.scalars(select(Passkey)).one()
    assert str(passkey.id) == r.json()["id"]
    assert passkey.user_id == user.id
    assert passkey.credential_id == authenticator.credential_id
    assert passkey.public_key == authenticator.cose_public_key
    assert passkey.sign_count == 0


def test_enrollment_records_event(
    db_client: TestClient,
    db_session: Session,
    user: User,
    code: str,
    authenticator: SoftAuthenticator,
) -> None:
    _, r = _enroll(db_client, code, authenticator)
    assert r.status_code == 201, r.text
    assert _events(db_session) == Counter([(AuthEventKind.enroll, user.id)])
    event = db_session.scalars(select(AuthEvent)).one()
    # TestClient's "testclient" host isn't a real IP; client_ip() turns it into NULL.
    assert event.ip is None
    assert event.user_agent == "Tally/1.0 iOS"


def test_enrollment_consumes_challenge(
    db_client: TestClient,
    db_session: Session,
    code: str,
    authenticator: SoftAuthenticator,
) -> None:
    _, r = _enroll(db_client, code, authenticator)
    assert r.status_code == 201, r.text
    assert db_session.scalars(select(WebAuthnChallenge)).all() == []


def test_unknown_code_is_refused(db_client: TestClient, db_session: Session, code: str) -> None:
    _assert_refused(db_client.post(OPTIONS, json={"code": "AAAA-BBBB-CCCC"}))
    assert _events(db_session) == Counter([(AuthEventKind.enroll_failed, None)])
    assert db_session.scalars(select(WebAuthnChallenge)).all() == []


def test_expired_code_is_refused(db_client: TestClient, db_session: Session, code: str) -> None:
    db_session.execute(
        update(EnrollmentCode).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    _assert_refused(db_client.post(OPTIONS, json={"code": code}))
    assert _events(db_session) == Counter([(AuthEventKind.enroll_failed, None)])


def test_reused_code_is_refused(db_client: TestClient, db_session: Session, code: str) -> None:
    _start(db_client, code)
    _assert_refused(db_client.post(OPTIONS, json={"code": code}))
    assert _events(db_session) == Counter([(AuthEventKind.enroll_failed, None)])


def test_missing_code_is_a_validation_error(db_client: TestClient) -> None:
    assert db_client.post(OPTIONS, json={}).status_code == 422
    assert db_client.post(OPTIONS, json={"code": ""}).status_code == 422


def test_replayed_challenge_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    code: str,
    authenticator: SoftAuthenticator,
) -> None:
    started = _start(db_client, code)
    body = {
        "challenge_id": started["challenge_id"],
        "credential": authenticator.register(started["options"]),
    }
    assert db_client.post(VERIFY, json=body).status_code == 201
    _assert_refused(db_client.post(VERIFY, json=body))

    assert len(db_session.scalars(select(Passkey)).all()) == 1
    assert _events(db_session) == Counter(
        [
            (AuthEventKind.enroll, user.id),
            (AuthEventKind.enroll_failed, None),
        ]
    )


def test_failed_verification_still_uses_up_the_challenge(
    db_client: TestClient,
    db_session: Session,
    user: User,
    code: str,
    authenticator: SoftAuthenticator,
) -> None:
    started = _start(db_client, code)
    bad = authenticator.register(started["options"], origin="https://evil.example")
    good = authenticator.register(started["options"])
    challenge_id = started["challenge_id"]

    _assert_refused(db_client.post(VERIFY, json={"challenge_id": challenge_id, "credential": bad}))
    _assert_refused(db_client.post(VERIFY, json={"challenge_id": challenge_id, "credential": good}))
    assert db_session.scalars(select(Passkey)).all() == []
    assert _events(db_session) == Counter(
        [
            (AuthEventKind.enroll_failed, user.id),
            (AuthEventKind.enroll_failed, None),
        ]
    )


def test_expired_challenge_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    code: str,
    authenticator: SoftAuthenticator,
) -> None:
    started = _start(db_client, code)
    db_session.execute(
        update(WebAuthnChallenge).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    body = {
        "challenge_id": started["challenge_id"],
        "credential": authenticator.register(started["options"]),
    }
    _assert_refused(db_client.post(VERIFY, json=body))
    assert db_session.scalars(select(Passkey)).all() == []
    assert _events(db_session) == Counter([(AuthEventKind.enroll_failed, user.id)])


@pytest.mark.parametrize(
    "tamper",
    [
        {"challenge": bytes_to_base64url(b"some other challenge")},
        {"origin": "https://evil.example"},
        {"flags": FLAG_UP | FLAG_AT},  # no user verification
    ],
    ids=["wrong-challenge", "wrong-origin", "not-user-verified"],
)
def test_bad_registration_response_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    code: str,
    authenticator: SoftAuthenticator,
    tamper: dict[str, Any],
) -> None:
    started = _start(db_client, code)
    credential = authenticator.register(started["options"], **tamper)
    r = db_client.post(
        VERIFY, json={"challenge_id": started["challenge_id"], "credential": credential}
    )
    _assert_refused(r)
    assert db_session.scalars(select(Passkey)).all() == []
    assert _events(db_session) == Counter([(AuthEventKind.enroll_failed, user.id)])


def test_wrong_rp_id_is_refused(
    db_client: TestClient, db_session: Session, code: str, settings: Settings
) -> None:
    started = _start(db_client, code)
    other_site = SoftAuthenticator("evil.example", settings.webauthn_origin)
    body = {
        "challenge_id": started["challenge_id"],
        "credential": other_site.register(started["options"]),
    }
    _assert_refused(db_client.post(VERIFY, json=body))
    assert db_session.scalars(select(Passkey)).all() == []


def test_malformed_credential_is_refused(
    db_client: TestClient, db_session: Session, user: User, code: str
) -> None:
    started = _start(db_client, code)
    body = {"challenge_id": started["challenge_id"], "credential": {"id": "nope"}}
    _assert_refused(db_client.post(VERIFY, json=body))
    assert _events(db_session) == Counter([(AuthEventKind.enroll_failed, user.id)])


def test_unknown_challenge_is_refused(
    db_client: TestClient, db_session: Session, authenticator: SoftAuthenticator
) -> None:
    body = {
        "challenge_id": str(uuid.uuid4()),
        "credential": authenticator.register({"challenge": bytes_to_base64url(b"x" * 32)}),
    }
    _assert_refused(db_client.post(VERIFY, json=body))
    assert _events(db_session) == Counter([(AuthEventKind.enroll_failed, None)])


def test_already_enrolled_credential_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    authenticator: SoftAuthenticator,
) -> None:
    statuses = []
    for _ in range(2):
        started = _start(db_client, issue_code(db_session, user.id, datetime.now(UTC)))
        body = {
            "challenge_id": started["challenge_id"],
            "credential": authenticator.register(started["options"]),
        }
        statuses.append(db_client.post(VERIFY, json=body).status_code)
    assert statuses == [201, 401]
    assert len(db_session.scalars(select(Passkey)).all()) == 1


def test_re_enrollment_attaches_to_the_same_user(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    for _ in range(2):
        started = _start(db_client, issue_code(db_session, user.id, datetime.now(UTC)))
        assert base64url_to_bytes(started["options"]["user"]["id"]) == user.id.bytes
        device = SoftAuthenticator(settings.webauthn_rp_id, settings.webauthn_origin)
        body = {
            "challenge_id": started["challenge_id"],
            "credential": device.register(started["options"]),
        }
        assert db_client.post(VERIFY, json=body).status_code == 201

    assert [p.user_id for p in db_session.scalars(select(Passkey))] == [user.id, user.id]
    assert len(db_session.scalars(select(User)).all()) == 1


def test_long_user_agent_is_truncated(
    db_client: TestClient, db_session: Session, code: str
) -> None:
    db_client.post(OPTIONS, json={"code": "AAAA-BBBB-CCCC"}, headers={"User-Agent": "x" * 5000})
    event = db_session.scalars(select(AuthEvent)).one()
    assert event.user_agent == "x" * USER_AGENT_MAX


def test_record_truncates_long_user_agent(db_session: Session) -> None:
    """No HTTP round trip: `record` does the truncation, not the router."""
    client = ClientInfo(ip="203.0.113.5", user_agent="x" * 5000)
    record(db_session, client, AuthEventKind.enroll_failed)
    event = db_session.scalars(select(AuthEvent)).one()
    assert event.user_agent == "x" * USER_AGENT_MAX
    assert event.ip == "203.0.113.5"


def test_storing_a_challenge_clears_expired_ones(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    old = _store_challenge(
        db_session, NewChallenge(ChallengePurpose.registration, b"old", user.id), now
    )
    later = now + timedelta(minutes=6)
    new = _store_challenge(
        db_session, NewChallenge(ChallengePurpose.registration, b"new", user.id), later
    )
    assert db_session.scalars(select(WebAuthnChallenge.id)).all() == [new]
    assert old != new


def test_challenge_only_works_for_its_purpose(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    new_challenge = NewChallenge(ChallengePurpose.registration, b"c", user.id)
    challenge_id = _store_challenge(db_session, new_challenge, now)
    with pytest.raises(CeremonyFailed):
        consume_challenge(db_session, challenge_id, ChallengePurpose.authentication, now)
    assert consume_challenge(db_session, challenge_id, ChallengePurpose.registration, now)


def test_concurrent_consumes_succeed_only_once(engine: Engine) -> None:
    """Needs committed rows, so it runs outside the rolled-back test transaction."""
    now = datetime.now(UTC)
    with Session(engine) as setup:
        challenge_id = _store_challenge(
            setup, NewChallenge(ChallengePurpose.authentication, b"c", None), now
        )
        setup.commit()

    try:
        with Session(engine) as first, Session(engine) as second:
            consume_challenge(first, challenge_id, ChallengePurpose.authentication, now)

            second_pid = second.scalar(text("SELECT pg_backend_pid()"))
            assert second_pid is not None
            errors: list[BaseException] = []

            def race() -> None:
                try:
                    consume_challenge(second, challenge_id, ChallengePurpose.authentication, now)
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
            cleanup.execute(delete(WebAuthnChallenge).where(WebAuthnChallenge.id == challenge_id))
            cleanup.commit()
