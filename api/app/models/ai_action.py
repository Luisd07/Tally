import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import AIActionSource, AIActionStatus


class AIAction(UUIDPk, Timestamps, Base):
    """A change Claude proposed (#222). Never applied on its own: the user reviews and
    confirms it.

    `steps` is the validated `app.schemas.ai_actions.ActionSteps`, serialized so it parses
    back into the same steps. `undo` records what applying it changed."""

    __tablename__ = "ai_actions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    summary: Mapped[str] = mapped_column(String(300))
    status: Mapped[AIActionStatus] = mapped_column(
        Enum(AIActionStatus, native_enum=False, length=16)
    )
    source: Mapped[AIActionSource] = mapped_column(
        Enum(AIActionSource, native_enum=False, length=16)
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="SET NULL")
    )
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    undo: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))
