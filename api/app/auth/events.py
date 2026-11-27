"""Recording sign-ins, refreshes, failures and enrollments to `auth_events`."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.auth import enrollment
from app.auth.errors import CeremonyFailed
from app.models import AuthEvent
from app.models.enums import AuthEventKind

# The User-Agent header is client-controlled; keep enough to recognise the device.
USER_AGENT_MAX = 512


@dataclass(frozen=True)
class ClientInfo:
    """The caller's address and device, as much as `AuthEvent` ever needs of a request."""

    ip: str | None
    user_agent: str | None


def record(
    db: Session, client: ClientInfo, kind: AuthEventKind, user_id: uuid.UUID | None = None
) -> None:
    """Add an event to the session. The caller commits, together with the rest of its work."""
    user_agent = client.user_agent
    db.add(
        AuthEvent(
            user_id=user_id,
            kind=kind,
            ip=client.ip,
            user_agent=user_agent[:USER_AGENT_MAX] if user_agent else None,
        )
    )


def refuse(db: Session, client: ClientInfo, kind: AuthEventKind, exc: CeremonyFailed) -> None:
    """Record a refused ceremony and commit it immediately.

    Committing here, rather than letting the caller roll back, is what keeps a challenge
    that a failed attempt consumed burned instead of usable again.
    """
    record(db, client, kind, exc.user_id)
    db.commit()


def replace_owner_credentials(
    db: Session, client: ClientInfo, user_id: uuid.UUID, now: datetime
) -> tuple[int, int]:
    """Revoke `user_id`'s passkeys and refresh tokens and record the replacement.

    For re-enrollment (`scripts/enroll.py --replace`) after a lost or compromised phone.
    The caller commits, together with the new enrollment code issued in the same
    transaction. Returns `(passkeys_deleted, tokens_revoked)`.
    """
    passkeys_deleted, tokens_revoked = enrollment.revoke_credentials(db, user_id, now)
    record(db, client, AuthEventKind.passkeys_replaced, user_id)
    return passkeys_deleted, tokens_revoked
