"""App Attest (#75): attesting a device key, then checking its assertions on sign-in and refresh.

Follows Apple's "Validating apps that connect to your server"
(https://developer.apple.com/documentation/devicecheck/validating-apps-that-connect-to-your-server).
The app fetches a one-time challenge from `GET /api/auth/attest/challenge` before each step:

- Attestation: `POST /api/auth/attest` with the key identifier and attestation object.
- Assertion: sign-in and refresh carry three headers, `X-App-Attest-Key-Id` (the key
  identifier, base64), `X-App-Attest-Assertion` (the assertion, base64) and
  `X-App-Attest-Challenge` (the challenge id). The app signs clientData = the exact request
  body bytes followed by the raw challenge bytes.
"""

import base64
import hashlib
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from webauthn.helpers import parse_cbor, validate_certificate_chain
from webauthn.helpers.exceptions import WebAuthnException

from app.auth.errors import CeremonyFailed
from app.config import Settings
from app.models import AppAttestChallenge, AppAttestKey
from app.models.enums import AppAttestEnvironment

CHALLENGE_TTL = timedelta(minutes=5)
CHALLENGE_BYTES = 32

# Apple App Attestation Root CA, from Apple's Private PKI repository
# (https://www.apple.com/certificateauthority/private/). Looked up when verifying, so tests
# can swap in their own root.
ROOT_CA_PEM = (Path(__file__).parent / "apple_app_attestation_root_ca.pem").read_bytes()
NONCE_OID = x509.ObjectIdentifier("1.2.840.113635.100.8.2")
# DER prefix of the nonce extension: SEQUENCE { [1] EXPLICIT OCTET STRING (32 bytes) }.
# DER is canonical, so comparing the whole value to prefix + nonce is an exact decode.
NONCE_DER_PREFIX = bytes.fromhex("3024a1220420")
AAGUIDS = {
    AppAttestEnvironment.development: b"appattestdevelop",
    AppAttestEnvironment.production: b"appattest" + bytes(7),
}

KEY_ID_HEADER = "X-App-Attest-Key-Id"
ASSERTION_HEADER = "X-App-Attest-Assertion"
CHALLENGE_HEADER = "X-App-Attest-Challenge"

# Authenticator data layout: rpIdHash (32), flags (1), counter (4), then for attestation
# aaguid (16), credentialId length (2) and credentialId.
RP_ID_HASH_END = 32
COUNTER_START, COUNTER_END = 33, 37
AAGUID_END = 53
CRED_ID_LEN_END = 55


@dataclass(frozen=True)
class AttestationAttempt:
    """The request values `verify_attestation` needs, still base64 as the app sent them."""

    challenge_id: uuid.UUID
    key_id: str
    attestation_object: str


@dataclass(frozen=True)
class PresentedAssertion:
    """An assertion lifted from the request headers, decoded but not yet verified."""

    key_id: bytes
    assertion: bytes
    challenge_id: uuid.UUID


def _environment(settings: Settings) -> AppAttestEnvironment:
    return AppAttestEnvironment(settings.app_attest_environment)


def _app_id_hash(settings: Settings) -> bytes:
    return hashlib.sha256(f"{settings.apns_team_id}.{settings.apns_bundle_id}".encode()).digest()


def _b64decode(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise CeremonyFailed(f"{what} is not base64") from exc


def issue_challenge(db: Session, now: datetime) -> AppAttestChallenge:
    """Store a fresh random challenge and return its row."""
    # Abandoned requests leave rows behind; clear the expired ones on the way in.
    db.execute(delete(AppAttestChallenge).where(AppAttestChallenge.expires_at <= now))
    row = AppAttestChallenge(
        challenge=secrets.token_bytes(CHALLENGE_BYTES), expires_at=now + CHALLENGE_TTL
    )
    db.add(row)
    db.flush()
    return row


def _consume_challenge(db: Session, challenge_id: uuid.UUID, now: datetime) -> bytes:
    """Delete the challenge and return its bytes, or raise if unknown, used or expired.

    DELETE ... RETURNING takes the row lock, so of two requests racing with the same
    challenge only one gets the row back; the other waits, then matches nothing.
    """
    row = db.execute(
        delete(AppAttestChallenge)
        .where(AppAttestChallenge.id == challenge_id)
        .returning(AppAttestChallenge)
    ).scalar_one_or_none()
    if row is None:
        raise CeremonyFailed("unknown or used app attest challenge")
    if row.expires_at <= now:
        raise CeremonyFailed("expired app attest challenge")
    return row.challenge


def _decode_attestation(raw: bytes) -> tuple[list[bytes], bytes]:
    """Return `(x5c, authData)` from an `apple-appattest` attestation object."""
    try:
        decoded: Any = parse_cbor(raw)
    except WebAuthnException as exc:
        raise CeremonyFailed("attestation is not CBOR") from exc
    if not isinstance(decoded, dict) or decoded.get("fmt") != "apple-appattest":
        raise CeremonyFailed("attestation is not apple-appattest")
    statement = decoded.get("attStmt")
    auth_data = decoded.get("authData")
    x5c = statement.get("x5c") if isinstance(statement, dict) else None
    if (
        not isinstance(x5c, list)
        or not x5c
        or not all(isinstance(cert, bytes) for cert in x5c)
        or not isinstance(auth_data, bytes)
    ):
        raise CeremonyFailed("attestation is missing x5c or authData")
    return x5c, auth_data


def _verified_credential_key(x5c: list[bytes], nonce: bytes) -> bytes:
    """Check the chain to Apple's root and the nonce, and return the credential certificate's
    public key as an X9.62 uncompressed point."""
    try:
        validate_certificate_chain(x5c=x5c, pem_root_certs_bytes=[ROOT_CA_PEM])
        cred_cert = x509.load_der_x509_certificate(x5c[0])
        extension = cred_cert.extensions.get_extension_for_oid(NONCE_OID).value
    except (WebAuthnException, ValueError, x509.ExtensionNotFound) as exc:
        raise CeremonyFailed(f"attestation certificate refused: {exc}") from exc
    if not isinstance(extension, x509.UnrecognizedExtension):
        raise CeremonyFailed("unexpected nonce extension type")
    if extension.value != NONCE_DER_PREFIX + nonce:
        raise CeremonyFailed("attestation nonce doesn't match")
    public_key = cred_cert.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise CeremonyFailed("credential certificate key is not P-256")
    return public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _check_attested_auth_data(auth_data: bytes, key_id: bytes, settings: Settings) -> None:
    if len(auth_data) < CRED_ID_LEN_END:
        raise CeremonyFailed("attestation authData too short")
    if auth_data[:RP_ID_HASH_END] != _app_id_hash(settings):
        raise CeremonyFailed("attestation rpIdHash doesn't match the App ID")
    if int.from_bytes(auth_data[COUNTER_START:COUNTER_END]) != 0:
        raise CeremonyFailed("attestation counter is not 0")
    if auth_data[COUNTER_END:AAGUID_END] != AAGUIDS[_environment(settings)]:
        raise CeremonyFailed("attestation aaguid doesn't match the environment")
    cred_id_len = int.from_bytes(auth_data[AAGUID_END:CRED_ID_LEN_END])
    if auth_data[CRED_ID_LEN_END : CRED_ID_LEN_END + cred_id_len] != key_id:
        raise CeremonyFailed("attestation credentialId doesn't match the key id")


def verify_attestation(
    db: Session, settings: Settings, attempt: AttestationAttempt, now: datetime
) -> AppAttestKey:
    """Verify an attestation object and store its key, not yet bound to a user."""
    challenge = _consume_challenge(db, attempt.challenge_id, now)
    key_id = _b64decode(attempt.key_id, "key id")
    x5c, auth_data = _decode_attestation(_b64decode(attempt.attestation_object, "attestation"))
    nonce = hashlib.sha256(auth_data + hashlib.sha256(challenge).digest()).digest()
    public_key = _verified_credential_key(x5c, nonce)
    if hashlib.sha256(public_key).digest() != key_id:
        raise CeremonyFailed("key id is not the hash of the credential public key")
    _check_attested_auth_data(auth_data, key_id, settings)
    if db.scalar(select(AppAttestKey.id).where(AppAttestKey.key_id == key_id)) is not None:
        raise CeremonyFailed("key already attested")
    key = AppAttestKey(
        key_id=key_id, public_key=public_key, sign_count=0, environment=_environment(settings)
    )
    db.add(key)
    db.flush()
    return key


def read_assertion_headers(headers: Mapping[str, str]) -> PresentedAssertion | None:
    """Decode the assertion headers. None if the request has none of them; raise if it has
    only some, or any is malformed."""
    values = [headers.get(name) for name in (KEY_ID_HEADER, ASSERTION_HEADER, CHALLENGE_HEADER)]
    if not any(values):
        return None
    key_id, assertion, challenge_id = values
    if not key_id or not assertion or not challenge_id:
        raise CeremonyFailed("incomplete app attest headers")
    try:
        parsed_challenge_id = uuid.UUID(challenge_id)
    except ValueError as exc:
        raise CeremonyFailed("app attest challenge id is not a UUID") from exc
    return PresentedAssertion(
        key_id=_b64decode(key_id, "key id"),
        assertion=_b64decode(assertion, "assertion"),
        challenge_id=parsed_challenge_id,
    )


def _decode_assertion(raw: bytes) -> tuple[bytes, bytes]:
    """Return `(signature, authenticatorData)` from a CBOR assertion."""
    try:
        decoded: Any = parse_cbor(raw)
    except WebAuthnException as exc:
        raise CeremonyFailed("assertion is not CBOR") from exc
    if not isinstance(decoded, dict):
        raise CeremonyFailed("assertion is not a map")
    signature = decoded.get("signature")
    auth_data = decoded.get("authenticatorData")
    if not isinstance(signature, bytes) or not isinstance(auth_data, bytes):
        raise CeremonyFailed("assertion is missing signature or authenticatorData")
    if len(auth_data) < COUNTER_END:
        raise CeremonyFailed("assertion authenticatorData too short")
    return signature, auth_data


def _check_signature(
    key: AppAttestKey,
    settings: Settings,
    signature: bytes,
    auth_data: bytes,
    client_data_hash: bytes,
) -> None:
    nonce = hashlib.sha256(auth_data + client_data_hash).digest()
    public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key.public_key)
    try:
        public_key.verify(signature, nonce, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise CeremonyFailed("assertion signature not valid", key.user_id) from exc
    if auth_data[:RP_ID_HASH_END] != _app_id_hash(settings):
        raise CeremonyFailed("assertion rpIdHash doesn't match the App ID", key.user_id)


def _check_counter(key: AppAttestKey, auth_data: bytes) -> None:
    if int.from_bytes(auth_data[COUNTER_START:COUNTER_END]) <= key.sign_count:
        raise CeremonyFailed("assertion counter did not increase", key.user_id)


def verify_assertion(
    db: Session, settings: Settings, presented: PresentedAssertion, body: bytes, now: datetime
) -> AppAttestKey:
    """Verify an assertion over `body` and store its counter. Returns the key, still locked.

    The key's row is locked `FOR UPDATE` until the caller commits, so two requests with the
    same key can't both pass the counter check.
    """
    challenge = _consume_challenge(db, presented.challenge_id, now)
    key = db.scalars(
        select(AppAttestKey).where(AppAttestKey.key_id == presented.key_id).with_for_update()
    ).one_or_none()
    if key is None:
        raise CeremonyFailed("unknown app attest key")
    if key.environment != _environment(settings):
        raise CeremonyFailed("app attest key is from the other environment", key.user_id)
    signature, auth_data = _decode_assertion(presented.assertion)
    _check_signature(key, settings, signature, auth_data, hashlib.sha256(body + challenge).digest())
    _check_counter(key, auth_data)
    key.sign_count = int.from_bytes(auth_data[COUNTER_START:COUNTER_END])
    db.flush()
    return key


def check_request(
    db: Session, settings: Settings, headers: Mapping[str, str], body: bytes, now: datetime
) -> AppAttestKey | None:
    """Verify the request's assertion, if it carries one. A request without one is refused
    when `app_attest_required` is set, and let through (None) otherwise."""
    presented = read_assertion_headers(headers)
    if presented is None:
        if settings.app_attest_required:
            raise CeremonyFailed("app attest assertion required")
        return None
    return verify_assertion(db, settings, presented, body, now)


def ensure_bindable(key: AppAttestKey, user_id: uuid.UUID) -> None:
    """Refuse if the key already belongs to someone other than `user_id`."""
    if key.user_id is not None and key.user_id != user_id:
        raise CeremonyFailed("app attest key belongs to another user", user_id)


def bind_to_user(db: Session, key: AppAttestKey, user_id: uuid.UUID) -> None:
    """Tie a key to the user it signed in, or refuse if it already belongs to someone else."""
    ensure_bindable(key, user_id)
    if key.user_id is None:
        key.user_id = user_id
        db.flush()
