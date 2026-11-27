"""One-time enrollment codes: printed on the VM by scripts/enroll.py, redeemed to add a passkey."""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.auth import tokens
from app.models import EnrollmentCode, Passkey, RefreshToken, User

CODE_TTL = timedelta(minutes=10)
# Uppercase letters and digits without the look-alikes 0/O, 1/I/L and U/V. 12 characters
# from 29 symbols is about 58 bits, plenty for a code that lives 10 minutes and works once.
ALPHABET = "ABCDEFGHJKMNPQRSTWXYZ23456789"
CODE_LENGTH = 12
GROUP = 4


def generate_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + GROUP] for i in range(0, CODE_LENGTH, GROUP))


def hash_code(code: str) -> str:
    """Hash after dropping dashes, spaces and case, so the code can be typed loosely."""
    normalized = "".join(code.split()).replace("-", "").upper()
    return hashlib.sha256(normalized.encode()).hexdigest()


def issue_code(db: Session, user_id: uuid.UUID, now: datetime) -> str:
    """Store a new code for the user and return it. Any unused code they had stops working."""
    db.execute(
        delete(EnrollmentCode).where(
            EnrollmentCode.user_id == user_id, EnrollmentCode.used_at.is_(None)
        )
    )
    code = generate_code()
    db.add(EnrollmentCode(user_id=user_id, code_hash=hash_code(code), expires_at=now + CODE_TTL))
    db.flush()
    return code


def get_or_create_user(db: Session, name: str) -> User:
    """Return the single owner, creating them with `name` if they don't exist yet."""
    user = db.scalars(select(User).order_by(User.created_at).limit(1)).first()
    if user is None:
        user = User(display_name=name)
        db.add(user)
        db.flush()
    return user


def revoke_credentials(db: Session, user_id: uuid.UUID, now: datetime) -> tuple[int, int]:
    """Delete every passkey and revoke every unrevoked refresh token for `user_id`.

    Deletes the passkeys before looking up which refresh-token families are still live.
    The DELETE has to wait for any passkey row a concurrent sign-in holds `FOR UPDATE`
    (`passkeys.finish_sign_in`), so by the time the family lookup below runs, that sign-in's
    new family, if it minted one, is already committed and gets picked up and revoked.
    Reading the families first would let that race slip a live token past this call.

    Locks each affected family, sorted by family_id for a consistent order, the same way
    `tokens.rotate_refresh_token` does before touching its rows: a revocation here can't
    race a concurrent rotation into leaving a live token behind in a family it also
    touches. Returns `(passkeys_deleted, tokens_revoked)`.
    """
    passkeys_deleted = len(
        db.scalars(delete(Passkey).where(Passkey.user_id == user_id).returning(Passkey.id)).all()
    )

    family_ids = sorted(
        db.scalars(
            select(RefreshToken.family_id)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .distinct()
        ).all()
    )
    tokens_revoked = sum(tokens.revoke_family(db, family_id, now) for family_id in family_ids)
    return passkeys_deleted, tokens_revoked


def issue_code_for_owner(db: Session, name: str, now: datetime) -> str:
    """Issue a code for the single owner, creating them with `name` on first enrollment."""
    user = get_or_create_user(db, name)
    return issue_code(db, user.id, now)


def redeem_code(db: Session, code: str, now: datetime) -> uuid.UUID | None:
    """Mark the code used and return its user, or None if it's unknown, expired or used.

    One UPDATE checks and marks the code, so two requests racing with the same code can't
    both redeem it: the second waits on the row lock and then matches nothing.
    """
    return db.scalar(
        update(EnrollmentCode)
        .where(
            EnrollmentCode.code_hash == hash_code(code),
            EnrollmentCode.used_at.is_(None),
            EnrollmentCode.expires_at > now,
        )
        .values(used_at=now)
        .returning(EnrollmentCode.user_id)
    )
