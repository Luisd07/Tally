"""Access tokens (short-lived JWT) and rotating refresh tokens (stored hashed)."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import RefreshToken

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"


def create_access_token(user_id: uuid.UUID, settings: Settings, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "typ": ACCESS_TOKEN_TYPE,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(payload, settings.jwt_signing_key.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> uuid.UUID | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_signing_key.get_secret_value(),
            algorithms=[ALGORITHM],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "typ", "iss", "aud", "iat", "exp"]},
        )
        if payload["typ"] != ACCESS_TOKEN_TYPE:
            return None
        return uuid.UUID(payload["sub"])
    except jwt.PyJWTError, ValueError:
        return None


def new_refresh_token() -> tuple[str, str]:
    """Return (token for the client, hash to store)."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _store_refresh_token(
    db: Session, user_id: uuid.UUID, family_id: uuid.UUID, settings: Settings, now: datetime
) -> str:
    """Add one refresh-token row to `family_id` and return the token for the client."""
    token, token_hash = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            family_id=family_id,
            expires_at=now + timedelta(days=settings.refresh_token_days),
        )
    )
    db.flush()
    return token


def issue_refresh_token(db: Session, user_id: uuid.UUID, settings: Settings, now: datetime) -> str:
    """Store a refresh token that starts a new family (one per sign-in) and return it."""
    return _store_refresh_token(db, user_id, uuid.uuid4(), settings, now)


@dataclass(frozen=True)
class IssuedSession:
    """The tokens a successful sign-in hands back, mapped as-is onto `TokenResponse`."""

    access_token: str
    refresh_token: str
    expires_in: int


def issue_session(
    db: Session,
    user_id: uuid.UUID,
    settings: Settings,
    now: datetime,
    family_id: uuid.UUID | None = None,
) -> IssuedSession:
    """Issue a fresh access token and refresh token, starting a new family unless `family_id`
    continues an existing one (rotation)."""
    return IssuedSession(
        access_token=create_access_token(user_id, settings, now),
        refresh_token=_store_refresh_token(db, user_id, family_id or uuid.uuid4(), settings, now),
        expires_in=settings.access_token_minutes * 60,
    )


class RefreshRejected(Exception):
    """A refresh token could not be rotated or revoked.

    `reason` is for the server log, never the response. `user_id` is set when the token
    could be tied to a user (reuse; see `RefreshReused`), and `None` for an unknown or
    expired token. Any revocation made before raising is flushed but not committed: the
    caller MUST record the event and commit before returning.
    """

    def __init__(self, reason: str, user_id: uuid.UUID | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_id = user_id


class RefreshReused(RefreshRejected):
    """A refresh token was presented after it had already been rotated or signed out.

    By the time this is raised, the whole family's revocation has already been flushed but
    not committed: the caller MUST record the event and commit before returning, so
    revocation and audit event land in one transaction.
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        super().__init__("reuse", user_id)


@dataclass(frozen=True)
class RotatedSession:
    """A rotated refresh token's new session, as-is onto `TokenResponse`, plus whose it is."""

    user_id: uuid.UUID
    session: IssuedSession


def _lock_family(db: Session, family_id: uuid.UUID) -> None:
    """Block until this transaction holds the advisory lock for `family_id`.

    `pg_advisory_xact_lock` takes a bigint key; `hashtextextended` folds the family's UUID
    text into one, with a fixed seed of 0 so every caller derives the same key from the
    same family_id. Every rotation and every revocation takes this lock, before touching
    any row in the family, so a rotation that is still in flight when a revocation starts
    can no longer leave its new token outside the revocation's view. Callers that take more
    than one family lock in the same transaction (`enrollment.revoke_credentials`) must take
    them in ascending family_id order; every other operation here holds at most one lock at
    a time. Postgres releases the lock automatically at commit or rollback.
    """
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(family_id), 0))))


def revoke_family(db: Session, family_id: uuid.UUID, now: datetime) -> int:
    """Revoke every unrevoked token in a family. Returns how many rows changed."""
    _lock_family(db, family_id)
    revoked = db.scalars(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(RefreshToken.id)
    ).all()
    return len(revoked)


def rotate_refresh_token(
    db: Session, token: str, settings: Settings, now: datetime
) -> RotatedSession:
    """Rotate a refresh token: revoke it and issue a new one in the same family.

    Raises `RefreshRejected` if the token is unknown or expired. Raises `RefreshReused`,
    after revoking (but not committing) the whole family, if the token had already been
    rotated or signed out: with no grace window, a concurrent duplicate refresh counts as
    reuse too.
    """
    token_hash = hash_refresh_token(token)
    family_id = db.scalar(
        select(RefreshToken.family_id).where(RefreshToken.token_hash == token_hash)
    )
    if family_id is None:
        raise RefreshRejected("unknown")
    _lock_family(db, family_id)

    rotated = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .values(revoked_at=now)
        .returning(RefreshToken.user_id, RefreshToken.family_id)
    ).one_or_none()
    if rotated is not None:
        user_id, family_id = rotated
        return RotatedSession(user_id, issue_session(db, user_id, settings, now, family_id))

    existing = db.scalars(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).one()
    if existing.revoked_at is None:
        raise RefreshRejected("expired")
    revoke_family(db, existing.family_id, now)
    raise RefreshReused(existing.user_id)


def revoke_refresh_token_family(db: Session, token: str, now: datetime) -> uuid.UUID | None:
    """Revoke the presented token's whole family for sign-out.

    Returns the user_id to log, or None if there's nothing to revoke: an unknown token, or
    one whose family was already fully revoked.
    """
    row = db.execute(
        select(RefreshToken.user_id, RefreshToken.family_id).where(
            RefreshToken.token_hash == hash_refresh_token(token)
        )
    ).first()
    if row is None:
        return None
    user_id, family_id = row
    return user_id if revoke_family(db, family_id, now) else None
