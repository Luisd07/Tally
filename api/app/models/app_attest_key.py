import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import AppAttestEnvironment


class AppAttestKey(UUIDPk, Timestamps, Base):
    """A device key that passed App Attest, checked against every later assertion.

    Attestation runs on the open `/api/auth/attest` route, before the device has signed in,
    so a new key has no user. The first sign-in that carries a valid assertion with it binds
    it to the passkey's user, and from then on assertions with it are refused for anyone
    else (Apple: a key must not be associated with more than one user). One row per device:
    the app attests a fresh key after a reinstall, and the old row is simply never used again.
    """

    __tablename__ = "app_attest_keys"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # SHA-256 of `public_key`, which is what the app calls its key identifier.
    key_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    # The credential certificate's P-256 key as an X9.62 uncompressed point (65 bytes).
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    # The authenticator data counter is an unsigned 32-bit value, too big for INTEGER.
    sign_count: Mapped[int] = mapped_column(BigInteger)
    environment: Mapped[AppAttestEnvironment] = mapped_column(
        Enum(AppAttestEnvironment, native_enum=False, length=16)
    )


class AppAttestChallenge(UUIDPk, Timestamps, Base):
    """A one-time App Attest challenge. Verifying deletes the row, so each works once."""

    __tablename__ = "app_attest_challenges"

    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
