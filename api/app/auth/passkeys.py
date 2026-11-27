"""Passkey enrollment and sign-in, verified with the `webauthn` package."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json_dict, parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.registration.verify_registration_response import VerifiedRegistration

from app.auth.enrollment import redeem_code
from app.auth.errors import CeremonyFailed
from app.config import Settings
from app.models import Passkey, User, WebAuthnChallenge
from app.models.enums import ChallengePurpose

CHALLENGE_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class RelyingParty:
    """The three `Settings` fields a WebAuthn ceremony needs, bundled so callers pass one."""

    id: str
    name: str
    origin: str

    @classmethod
    def from_settings(cls, settings: Settings) -> RelyingParty:
        return cls(settings.webauthn_rp_id, settings.webauthn_rp_name, settings.webauthn_origin)


@dataclass
class PendingCeremony:
    challenge_id: uuid.UUID
    options: dict[str, Any]


@dataclass(frozen=True)
class RegistrationAttempt:
    """The request values `finish_enrollment` needs to verify and store a credential."""

    challenge_id: uuid.UUID
    credential: dict[str, Any]
    device_name: str | None


@dataclass(frozen=True)
class NewChallenge:
    """The values that make up a challenge row, as opposed to when it's stored."""

    purpose: ChallengePurpose
    challenge: bytes
    user_id: uuid.UUID | None


@dataclass(frozen=True)
class SignInAttempt:
    """The request values `finish_sign_in` needs to verify an assertion."""

    challenge_id: uuid.UUID
    credential: dict[str, Any]


def _store_challenge(db: Session, new: NewChallenge, now: datetime) -> uuid.UUID:
    # Abandoned ceremonies leave rows behind; clear the expired ones on the way in.
    db.execute(delete(WebAuthnChallenge).where(WebAuthnChallenge.expires_at <= now))
    row = WebAuthnChallenge(
        purpose=new.purpose,
        challenge=new.challenge,
        user_id=new.user_id,
        expires_at=now + CHALLENGE_TTL,
    )
    db.add(row)
    db.flush()
    return row.id


def consume_challenge(
    db: Session, challenge_id: uuid.UUID, purpose: ChallengePurpose, now: datetime
) -> WebAuthnChallenge:
    """Delete the challenge and return it, or raise if it's unknown, already used or expired.

    DELETE ... RETURNING takes the row lock, so of two requests racing with the same
    challenge only one gets the row back; the other waits, then matches nothing.
    """
    row = db.execute(
        delete(WebAuthnChallenge)
        .where(WebAuthnChallenge.id == challenge_id, WebAuthnChallenge.purpose == purpose)
        .returning(WebAuthnChallenge)
    ).scalar_one_or_none()
    if row is None:
        raise CeremonyFailed("unknown or used challenge")
    if row.expires_at <= now:
        raise CeremonyFailed("expired challenge", row.user_id)
    return row


def start_enrollment(db: Session, rp: RelyingParty, code: str, now: datetime) -> PendingCeremony:
    """Redeem the enrollment code and return registration options for its user."""
    user_id = redeem_code(db, code, now)
    if user_id is None:
        raise CeremonyFailed("unknown, expired or used enrollment code")
    user = db.get_one(User, user_id)
    options = generate_registration_options(
        rp_id=rp.id,
        rp_name=rp.name,
        # The same handle on every enrollment, so a new passkey replaces the old one on
        # an authenticator that still has it.
        user_id=user.id.bytes,
        user_name=user.display_name,
        timeout=int(CHALLENGE_TTL.total_seconds() * 1000),
        # Discoverable credentials, so sign-in needs no username or credential list.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    new_challenge = NewChallenge(ChallengePurpose.registration, options.challenge, user.id)
    challenge_id = _store_challenge(db, new_challenge, now)
    return PendingCeremony(challenge_id, options_to_json_dict(options))


def _verify_registration(
    pending: WebAuthnChallenge, credential: dict[str, Any], rp: RelyingParty
) -> VerifiedRegistration:
    """Run py_webauthn's verifier, translating its exception into CeremonyFailed."""
    try:
        return verify_registration_response(
            credential=credential,
            expected_challenge=pending.challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            require_user_verification=True,
        )
    except WebAuthnException as exc:
        raise CeremonyFailed(f"registration not verified: {exc}", pending.user_id) from exc


def _ensure_credential_is_new(db: Session, credential_id: bytes, user_id: uuid.UUID) -> None:
    taken = db.scalar(select(Passkey.id).where(Passkey.credential_id == credential_id))
    if taken is not None:
        raise CeremonyFailed("credential already enrolled", user_id)


def finish_enrollment(
    db: Session, rp: RelyingParty, attempt: RegistrationAttempt, now: datetime
) -> Passkey:
    """Verify the authenticator's registration response and store the new passkey."""
    pending = consume_challenge(db, attempt.challenge_id, ChallengePurpose.registration, now)
    if pending.user_id is None:
        raise CeremonyFailed("registration challenge without a user")
    verified = _verify_registration(pending, attempt.credential, rp)
    _ensure_credential_is_new(db, verified.credential_id, pending.user_id)
    passkey = Passkey(
        user_id=pending.user_id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        device_name=attempt.device_name,
    )
    db.add(passkey)
    db.flush()
    return passkey


def start_sign_in(db: Session, rp: RelyingParty, now: datetime) -> PendingCeremony:
    """Authentication options with an empty allowCredentials: the passkey says who it is."""
    options = generate_authentication_options(
        rp_id=rp.id,
        timeout=int(CHALLENGE_TTL.total_seconds() * 1000),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    new_challenge = NewChallenge(ChallengePurpose.authentication, options.challenge, None)
    challenge_id = _store_challenge(db, new_challenge, now)
    return PendingCeremony(challenge_id, options_to_json_dict(options))


def _parse_assertion(credential: dict[str, Any]) -> AuthenticationCredential:
    """Run py_webauthn's parser, translating its exception into CeremonyFailed."""
    try:
        return parse_authentication_credential_json(credential)
    except (WebAuthnException, ValueError) as exc:  # ValueError: bad base64 in userHandle
        raise CeremonyFailed(f"malformed assertion: {exc}") from exc


def _locked_passkey(db: Session, parsed: AuthenticationCredential) -> Passkey:
    """Look up the assertion's passkey with its row locked, so two sign-ins with the same
    passkey can't both pass the counter check, and check the (optional) user handle."""
    passkey = db.scalars(
        select(Passkey).where(Passkey.credential_id == parsed.raw_id).with_for_update()
    ).one_or_none()
    if passkey is None:
        raise CeremonyFailed("unknown credential")
    user_handle = parsed.response.user_handle
    if user_handle is not None and user_handle != passkey.user_id.bytes:
        raise CeremonyFailed("user handle doesn't match the credential", passkey.user_id)
    return passkey


def finish_sign_in(db: Session, rp: RelyingParty, attempt: SignInAttempt, now: datetime) -> Passkey:
    """Verify the authenticator's assertion and record its new signature counter."""
    pending = consume_challenge(db, attempt.challenge_id, ChallengePurpose.authentication, now)
    parsed = _parse_assertion(attempt.credential)
    passkey = _locked_passkey(db, parsed)
    try:
        verified = verify_authentication_response(
            credential=parsed,
            expected_challenge=pending.challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except WebAuthnException as exc:
        raise CeremonyFailed(f"assertion not verified: {exc}", passkey.user_id) from exc
    passkey.sign_count = verified.new_sign_count
    db.flush()
    return passkey
