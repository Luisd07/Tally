import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class ChatConversation(UUIDPk, Timestamps, Base):
    """One money chat (#174), kept on the server for 30 days and never on the phone.

    `messages` is the Messages API history exactly as sent and received (tool calls,
    tool results and thinking blocks included), so the next question carries on from it."""

    __tablename__ = "chat_conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Questions asked so far, against `ai_chat.MAX_QUESTIONS`.
    questions: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    # `ai_chat.prompt_version` when the chat started; null for chats from before it existed.
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    # Whether the app that started the chat can show proposals, so its tools stay the same.
    actions_supported: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
