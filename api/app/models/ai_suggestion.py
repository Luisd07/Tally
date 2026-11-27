import uuid

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import AIBatchStatus, AIConfidence, AISuggestionStatus, Bucket


class AIBatch(UUIDPk, Timestamps, Base):
    """A Message Batches API job (#169). The worker submits one after the nightly sync and
    collects it once Anthropic reports it ended."""

    __tablename__ = "ai_batches"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    feature: Mapped[str] = mapped_column(String(50))
    # Anthropic's batch id ("msgbatch_...").
    anthropic_id: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[AIBatchStatus] = mapped_column(Enum(AIBatchStatus, native_enum=False, length=16))


class AISuggestion(UUIDPk, Timestamps, Base):
    """Claude's guess for one Inbox transaction (#169). Never applied on its own: the Inbox
    shows it, and the user confirms it with one tap."""

    __tablename__ = "ai_suggestions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), unique=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_batches.id", ondelete="SET NULL")
    )
    status: Mapped[AISuggestionStatus] = mapped_column(
        Enum(AISuggestionStatus, native_enum=False, length=16)
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    bucket: Mapped[Bucket | None] = mapped_column(Enum(Bucket, native_enum=False, length=16))
    merchant: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[AIConfidence | None] = mapped_column(
        Enum(AIConfidence, native_enum=False, length=16)
    )
    model: Mapped[str | None] = mapped_column(String(100))
