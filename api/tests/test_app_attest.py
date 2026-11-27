"""App Attest attestation and assertions on sign-in and refresh (#75).

Everything runs through real crypto from `tests/softattest.py`: a fake root CA the server
is pointed at in place of Apple's, and P-256 device keys. Nothing in the verification is mocked.
"""

import base64
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import app_attest, tokens
from app.config import Settings
from app.main import create_app
from app.models import AppAttestChallenge, AppAttestKey, AuthEvent, Passkey, RefreshToken, User
from app.models.enums import AppAttestEnvironment, AuthEventKind
from tests.softattest import AAGUID_DEVELOPMENT, FakeAppleCA, SoftAppAttest
from tests.softauthn import SoftAuthenticator

CHALLENGE = "/api/auth/attest/challenge"
ATTEST = "/api/auth/attest"
SIGNIN_OPTIONS = "/api/auth/signin/options"
SIGNIN_VERIFY = "/api/auth/signin/verify"
REFRESH = "/api/auth/refresh"

TEAM_ID = "ABCDE12345"
BUNDLE_ID = "com.example.tally"
APP_ID = f"{TEAM_ID}.{BUNDLE_ID}"


@pytest.fixture
def attest_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"apns_team_id": TEAM_ID, "apns_bundle_id": BUNDLE_ID})


@pytest.fixture
def app(attest_settings: Settings) -> FastAPI:
    return create_app(attest_settings)


@pytest.fixture
def ca(monkeypatch: pytest.MonkeyPatch) -> FakeAppleCA:
    """Our own root CA, trusted by the server in place of Apple's."""
    fake = FakeAppleCA()
    monkeypatch.setattr(app_attest, "ROOT_CA_PEM", fake.root_pem)
    return fake


@pytest.fixture
def device(ca: FakeAppleCA) -> SoftAppAttest:
    return SoftAppAttest(ca, APP_ID)


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


def _require(app: FastAPI, required: bool) -> None:
    app.state.settings = app.state.settings.model_copy(update={"app_attest_required": required})


def _events(db_session: Session) -> Counter[tuple[AuthEventKind, uuid.UUID | None]]:
    return Counter((e.kind, e.user_id) for e in db_session.scalars(select(AuthEvent)))


def _keys(db_session: Session) -> list[AppAttestKey]:
    return list(db_session.scalars(select(AppAttestKey)))


def _assert_refused(r: Response) -> None:
    assert r.status_code == 401
    error = r.json()["error"]
    assert (error["code"], error["message"]) == ("unauthorized", "Sign-in required.")


def _challenge(db_client: TestClient) -> tuple[str, bytes]:
    r = db_client.get(CHALLENGE)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    return body["challenge_id"], base64.b64decode(body["challenge"])


def _post_attestation(
    db_client: TestClient, device: SoftAppAttest, challenge_id: str, attestation: str
) -> Response:
    return db_client.post(
        ATTEST,
        json={
            "challenge_id": challenge_id,
            "key_id": device.key_id,
            "attestation_object": attestation,
        },
    )


def _attest(db_client: TestClient, device: SoftAppAttest, **kwargs: Any) -> Response:
    challenge_id, challenge = _challenge(db_client)
    return _post_attestation(db_client, device, challenge_id, device.attest(challenge, **kwargs))


@pytest.fixture
def attested(db_client: TestClient, device: SoftAppAttest) -> SoftAppAttest:
    r = _attest(db_client, device)
    assert r.status_code == 201, r.text
    return device


def _signed_post(
    db_client: TestClient,
    path: str,
    payload: dict[str, Any],
    device: SoftAppAttest | None,
    **assertion_kwargs: Any,
) -> Response:
    """POST `payload` as exact JSON bytes, with an assertion over them when `device` is set."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if device is not None:
        challenge_id, challenge = _challenge(db_client)
        headers |= {
            app_attest.KEY_ID_HEADER: device.key_id,
            app_attest.ASSERTION_HEADER: device.assertion(body, challenge, **assertion_kwargs),
            app_attest.CHALLENGE_HEADER: challenge_id,
        }
    return db_client.post(path, content=body, headers=headers)


def _sign_in(
    db_client: TestClient,
    authenticator: SoftAuthenticator,
    user: User,
    device: SoftAppAttest | None,
    **assertion_kwargs: Any,
) -> Response:
    started = db_client.post(SIGNIN_OPTIONS).json()
    credential = authenticator.sign_in(started["options"], user_handle=user.id.bytes)
    payload = {"challenge_id": started["challenge_id"], "credential": credential}
    return _signed_post(db_client, SIGNIN_VERIFY, payload, device, **assertion_kwargs)


def _refresh_token(db_session: Session, user: User, settings: Settings) -> str:
    return tokens.issue_refresh_token(db_session, user.id, settings, datetime.now(UTC))


def _refresh(
    db_client: TestClient, token: str, device: SoftAppAttest | None, **assertion_kwargs: Any
) -> Response:
    return _signed_post(db_client, REFRESH, {"refresh_token": token}, device, **assertion_kwargs)


# Attestation


def test_committed_root_is_apples_app_attestation_root_ca() -> None:
    root = x509.load_pem_x509_certificate(app_attest.ROOT_CA_PEM)
    assert root.subject.rfc4514_string() == (
        "ST=California,O=Apple Inc.,CN=Apple App Attestation Root CA"
    )
    assert root.fingerprint(hashes.SHA256()).hex() == (
        "1cb9823ba28ba6ad2d33a006941de2ae4f513ef1d4e831b9f7e0fa7b6242c932"
    )


def test_valid_attestation_stores_the_key(
    db_client: TestClient, db_session: Session, device: SoftAppAttest
) -> None:
    r = _attest(db_client, device)

    assert r.status_code == 201, r.text
    assert r.json()["key_id"] == device.key_id
    [key] = _keys(db_session)
    assert key.key_id == device.key_id_bytes
    assert key.public_key == device.public_point
    assert key.sign_count == 0
    assert key.environment == AppAttestEnvironment.production
    assert key.user_id is None
    assert _events(db_session) == Counter({(AuthEventKind.attest, None): 1})


def test_development_key_needs_the_development_environment(
    app: FastAPI, db_client: TestClient, db_session: Session, ca: FakeAppleCA
) -> None:
    device = SoftAppAttest(ca, APP_ID, aaguid=AAGUID_DEVELOPMENT)
    _assert_refused(_attest(db_client, device))

    app.state.settings = app.state.settings.model_copy(
        update={"app_attest_environment": "development"}
    )
    assert _attest(db_client, device).status_code == 201
    [key] = _keys(db_session)
    assert key.environment == AppAttestEnvironment.development


@pytest.mark.parametrize(
    "breakage",
    [
        pytest.param({"nonce_challenge": b"another challenge"}, id="wrong-nonce"),
        pytest.param({"app_id": "ZZZZZ99999.com.example.other"}, id="wrong-rp-id"),
        pytest.param({"counter": 1}, id="non-zero-counter"),
        pytest.param({"aaguid": AAGUID_DEVELOPMENT}, id="wrong-aaguid"),
    ],
)
def test_attestation_failing_a_check_is_refused(
    db_client: TestClient, db_session: Session, device: SoftAppAttest, breakage: dict[str, Any]
) -> None:
    _assert_refused(_attest(db_client, device, **breakage))

    assert _keys(db_session) == []
    assert _events(db_session) == Counter({(AuthEventKind.attest_failed, None): 1})


def test_attestation_from_an_untrusted_ca_is_refused(
    db_client: TestClient, db_session: Session, device: SoftAppAttest
) -> None:
    _assert_refused(_attest(db_client, device, ca=FakeAppleCA()))
    assert _keys(db_session) == []


def test_attestation_with_a_key_id_for_another_key_is_refused(
    db_client: TestClient, db_session: Session, device: SoftAppAttest, ca: FakeAppleCA
) -> None:
    challenge_id, challenge = _challenge(db_client)
    other = SoftAppAttest(ca, APP_ID)
    _assert_refused(_post_attestation(db_client, other, challenge_id, device.attest(challenge)))
    assert _keys(db_session) == []


def test_attestation_challenge_works_once(
    db_client: TestClient, db_session: Session, device: SoftAppAttest, ca: FakeAppleCA
) -> None:
    challenge_id, challenge = _challenge(db_client)
    r = _post_attestation(db_client, device, challenge_id, device.attest(challenge))
    assert r.status_code == 201, r.text
    other = SoftAppAttest(ca, APP_ID)

    _assert_refused(_post_attestation(db_client, other, challenge_id, other.attest(challenge)))
    assert [key.key_id for key in _keys(db_session)] == [device.key_id_bytes]


def test_attesting_the_same_key_twice_is_refused(
    db_client: TestClient, attested: SoftAppAttest
) -> None:
    _assert_refused(_attest(db_client, attested))


def test_malformed_attestation_is_refused(db_client: TestClient, device: SoftAppAttest) -> None:
    challenge_id, _ = _challenge(db_client)
    _assert_refused(_post_attestation(db_client, device, challenge_id, "bm90IGNib3I="))
    challenge_id, _ = _challenge(db_client)
    _assert_refused(_post_attestation(db_client, device, challenge_id, "not base64!"))


def _expire_challenges(db_session: Session) -> None:
    for row in db_session.scalars(select(AppAttestChallenge)):
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()


def test_expired_attestation_challenge_is_refused_and_consumed(
    db_client: TestClient, db_session: Session, device: SoftAppAttest
) -> None:
    challenge_id, challenge = _challenge(db_client)
    _expire_challenges(db_session)

    _assert_refused(_post_attestation(db_client, device, challenge_id, device.attest(challenge)))
    assert _keys(db_session) == []
    assert db_session.scalars(select(AppAttestChallenge)).all() == []


def test_non_ascii_key_id_in_attestation_is_refused(
    db_client: TestClient, db_session: Session, device: SoftAppAttest
) -> None:
    challenge_id, challenge = _challenge(db_client)
    r = db_client.post(
        ATTEST,
        json={
            "challenge_id": challenge_id,
            "key_id": "\u00e9",
            "attestation_object": device.attest(challenge),
        },
    )

    _assert_refused(r)
    assert _events(db_session)[(AuthEventKind.attest_failed, None)] == 1


# Assertions


def test_sign_in_with_a_valid_assertion_binds_the_key(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
    attested: SoftAppAttest,
) -> None:
    r = _sign_in(db_client, authenticator, user, attested)

    assert r.status_code == 200, r.text
    [key] = _keys(db_session)
    assert (key.user_id, key.sign_count) == (user.id, 1)
    assert _events(db_session)[(AuthEventKind.sign_in, user.id)] == 1


def test_refresh_with_a_valid_assertion_is_accepted(
    db_client: TestClient,
    db_session: Session,
    user: User,
    settings: Settings,
    attested: SoftAppAttest,
) -> None:
    r = _refresh(db_client, _refresh_token(db_session, user, settings), attested)

    assert r.status_code == 200, r.text
    [key] = _keys(db_session)
    assert (key.user_id, key.sign_count) == (user.id, 1)


def test_replayed_counter_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    settings: Settings,
    attested: SoftAppAttest,
) -> None:
    assert _refresh(db_client, _refresh_token(db_session, user, settings), attested).is_success
    token = _refresh_token(db_session, user, settings)

    _assert_refused(_refresh(db_client, token, attested, counter_step=0))
    assert _keys(db_session)[0].sign_count == 1
    assert _events(db_session)[(AuthEventKind.refresh_failed, None)] == 0
    assert _events(db_session)[(AuthEventKind.refresh_failed, user.id)] == 1


def test_assertion_with_the_wrong_signature_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
    attested: SoftAppAttest,
) -> None:
    other_key = ec.generate_private_key(ec.SECP256R1())

    _assert_refused(_sign_in(db_client, authenticator, user, attested, signing_key=other_key))
    assert db_session.scalars(select(RefreshToken)).all() == []
    assert _events(db_session) == Counter(
        {(AuthEventKind.attest, None): 1, (AuthEventKind.sign_in_failed, None): 1}
    )


def test_assertion_over_another_body_is_refused(
    db_client: TestClient, attested: SoftAppAttest
) -> None:
    challenge_id, challenge = _challenge(db_client)
    signed = json.dumps({"refresh_token": "a" * 64}).encode()
    sent = json.dumps({"refresh_token": "b" * 64}).encode()
    headers = {
        "Content-Type": "application/json",
        app_attest.KEY_ID_HEADER: attested.key_id,
        app_attest.ASSERTION_HEADER: attested.assertion(signed, challenge),
        app_attest.CHALLENGE_HEADER: challenge_id,
    }
    _assert_refused(db_client.post(REFRESH, content=sent, headers=headers))


def test_assertion_with_the_wrong_rp_id_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    settings: Settings,
    attested: SoftAppAttest,
) -> None:
    token = _refresh_token(db_session, user, settings)
    _assert_refused(_refresh(db_client, token, attested, app_id="ZZZZZ99999.com.example.other"))


def test_assertion_from_an_unknown_key_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    settings: Settings,
    device: SoftAppAttest,
) -> None:
    _assert_refused(_refresh(db_client, _refresh_token(db_session, user, settings), device))
    assert _events(db_session) == Counter({(AuthEventKind.refresh_failed, None): 1})


def test_assertion_challenge_works_once(
    db_client: TestClient,
    db_session: Session,
    user: User,
    settings: Settings,
    attested: SoftAppAttest,
) -> None:
    challenge_id, challenge = _challenge(db_client)

    def post(token: str) -> Response:
        body = json.dumps({"refresh_token": token}).encode()
        headers = {
            "Content-Type": "application/json",
            app_attest.KEY_ID_HEADER: attested.key_id,
            app_attest.ASSERTION_HEADER: attested.assertion(body, challenge),
            app_attest.CHALLENGE_HEADER: challenge_id,
        }
        return db_client.post(REFRESH, content=body, headers=headers)

    assert post(_refresh_token(db_session, user, settings)).status_code == 200
    _assert_refused(post(_refresh_token(db_session, user, settings)))


def test_expired_assertion_challenge_is_refused_and_consumed(
    db_client: TestClient,
    db_session: Session,
    user: User,
    settings: Settings,
    attested: SoftAppAttest,
) -> None:
    token = _refresh_token(db_session, user, settings)
    challenge_id, challenge = _challenge(db_client)
    _expire_challenges(db_session)
    body = json.dumps({"refresh_token": token}).encode()
    headers = {
        "Content-Type": "application/json",
        app_attest.KEY_ID_HEADER: attested.key_id,
        app_attest.ASSERTION_HEADER: attested.assertion(body, challenge),
        app_attest.CHALLENGE_HEADER: challenge_id,
    }

    _assert_refused(db_client.post(REFRESH, content=body, headers=headers))
    assert db_session.scalars(select(AppAttestChallenge)).all() == []
    assert _keys(db_session)[0].sign_count == 0


def test_non_ascii_assertion_headers_are_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
    settings: Settings,
) -> None:
    headers = {
        app_attest.KEY_ID_HEADER.encode(): b"\xffAAA",
        app_attest.ASSERTION_HEADER.encode(): b"AAAA",
        app_attest.CHALLENGE_HEADER.encode(): str(uuid.uuid4()).encode(),
    }
    token = _refresh_token(db_session, user, settings)
    _assert_refused(db_client.post(REFRESH, json={"refresh_token": token}, headers=headers))

    started = db_client.post(SIGNIN_OPTIONS).json()
    credential = authenticator.sign_in(started["options"], user_handle=user.id.bytes)
    payload = {"challenge_id": started["challenge_id"], "credential": credential}
    _assert_refused(db_client.post(SIGNIN_VERIFY, json=payload, headers=headers))

    events = _events(db_session)
    assert events[(AuthEventKind.refresh_failed, None)] == 1
    assert events[(AuthEventKind.sign_in_failed, None)] == 1


def test_incomplete_assertion_headers_are_refused(
    db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    token = _refresh_token(db_session, user, settings)
    r = db_client.post(
        REFRESH, json={"refresh_token": token}, headers={app_attest.KEY_ID_HEADER: "AAAA"}
    )
    _assert_refused(r)


def test_key_bound_to_another_user_is_refused(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    settings: Settings,
    attested: SoftAppAttest,
) -> None:
    assert _refresh(db_client, _refresh_token(db_session, user, settings), attested).is_success

    other_token = _refresh_token(db_session, other_user, settings)

    _assert_refused(_refresh(db_client, other_token, attested))
    assert _keys(db_session)[0].user_id == user.id
    # Refused before rotating, so the other user's token is neither revoked nor replaced.
    stored = db_session.scalars(
        select(RefreshToken).where(RefreshToken.user_id == other_user.id)
    ).one()
    assert stored.revoked_at is None
    assert _refresh(db_client, other_token, None).status_code == 200


# APP_ATTEST_REQUIRED


def test_required_sign_in_without_assertion_is_refused(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    _require(app, True)

    _assert_refused(_sign_in(db_client, authenticator, user, None))
    assert db_session.scalars(select(RefreshToken)).all() == []
    assert _events(db_session) == Counter({(AuthEventKind.sign_in_failed, None): 1})


def test_required_refresh_without_assertion_is_refused(
    app: FastAPI, db_client: TestClient, db_session: Session, user: User, settings: Settings
) -> None:
    _require(app, True)
    token = _refresh_token(db_session, user, settings)

    _assert_refused(_refresh(db_client, token, None))
    # Refused before rotating, so the token is still good once the app sends an assertion.
    stored = db_session.scalars(select(RefreshToken)).one()
    assert stored.revoked_at is None
    assert _events(db_session) == Counter({(AuthEventKind.refresh_failed, None): 1})


def test_required_sign_in_and_refresh_with_assertion_are_accepted(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
    attested: SoftAppAttest,
) -> None:
    _require(app, True)

    r = _sign_in(db_client, authenticator, user, attested)
    assert r.status_code == 200, r.text
    r = _refresh(db_client, r.json()["refresh_token"], attested)
    assert r.status_code == 200, r.text
    assert _keys(db_session)[0].sign_count == 2


def test_not_required_allows_sign_in_and_refresh_without_assertion(
    db_client: TestClient,
    db_session: Session,
    user: User,
    passkey: Passkey,
    authenticator: SoftAuthenticator,
) -> None:
    r = _sign_in(db_client, authenticator, user, None)
    assert r.status_code == 200, r.text
    r = _refresh(db_client, r.json()["refresh_token"], None)
    assert r.status_code == 200, r.text
