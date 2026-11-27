import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import ChallengePurpose


class WebAuthnChallenge(UUIDPk, Timestamps, Base):
    """A pending WebAuthn ceremony. Verifying deletes the row, so each challenge works once."""

    __tablename__ = "webauthn_challenges"

    purpose: Mapped[ChallengePurpose] = mapped_column(
        Enum(ChallengePurpose, native_enum=False, length=16)
    )
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    # Set for registration: the user whose enrollment code was redeemed. Null for sign-in,
    # which doesn't know the user until the passkey answers.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
