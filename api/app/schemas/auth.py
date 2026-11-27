import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.base import Schema


class EnrollOptionsRequest(Schema):
    code: str = Field(min_length=1, max_length=64)


class CeremonyOptions(Schema):
    """`options` is WebAuthn's own JSON (camelCase, base64url bytes), passed as-is to the
    authenticator. Send `challenge_id` back with the authenticator's response."""

    challenge_id: uuid.UUID
    options: dict[str, Any]


class EnrollVerifyRequest(Schema):
    challenge_id: uuid.UUID
    # WebAuthn's RegistrationResponseJSON, as the authenticator produced it.
    credential: dict[str, Any]
    device_name: str | None = Field(default=None, max_length=100)


class PasskeyOut(Schema):
    id: uuid.UUID
    device_name: str | None
    created_at: datetime


class SignInVerifyRequest(Schema):
    challenge_id: uuid.UUID
    # WebAuthn's AuthenticationResponseJSON, as the authenticator produced it.
    credential: dict[str, Any]


class RefreshRequest(Schema):
    # secrets.token_urlsafe(48) is 64 characters; 128 leaves room without inviting abuse.
    refresh_token: str = Field(min_length=1, max_length=128)


class TokenResponse(Schema):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    # Seconds until the access token expires.
    expires_in: int


class AttestChallenge(Schema):
    """A one-time App Attest challenge. `challenge` is base64; send `challenge_id` back."""

    challenge_id: uuid.UUID
    challenge: str


class AttestRequest(Schema):
    challenge_id: uuid.UUID
    # Base64, as DCAppAttestService returns them. An attestation object is a few KB.
    key_id: str = Field(min_length=1, max_length=64)
    attestation_object: str = Field(min_length=1, max_length=16384)


class AttestedKeyOut(Schema):
    key_id: str
    created_at: datetime
