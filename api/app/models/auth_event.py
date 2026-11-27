import uuid

from sqlalchemy import Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import AuthEventKind


class AuthEvent(UUIDPk, Timestamps, Base):
    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_events_created_at", "created_at"),)

    # Null for attempts that can't be tied to a user (unknown credential, bad enrollment code),
    # and kept as null if the user is deleted so the audit trail survives.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[AuthEventKind] = mapped_column(Enum(AuthEventKind, native_enum=False, length=32))
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
