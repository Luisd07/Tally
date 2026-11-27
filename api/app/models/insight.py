import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import InsightKind


class Insight(UUIDPk, Timestamps, Base):
    """A spending moment worth telling the user about (#225), with the fix proposed for it
    when one applies.

    `subject_key` names what it's about (a category's month, a charge, a merchant), so the
    same insight isn't raised again within 30 days. `day` is the app-timezone date it was
    found on."""

    __tablename__ = "insights"
    __table_args__ = (Index("ix_insights_user_id_day", "user_id", "day"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[InsightKind] = mapped_column(Enum(InsightKind, native_enum=False, length=20))
    title: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(String(300))
    subject_key: Mapped[str] = mapped_column(String(200))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_actions.id", ondelete="SET NULL")
    )
    day: Mapped[date] = mapped_column(Date)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
