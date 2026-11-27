import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class AIUsage(UUIDPk, Timestamps, Base):
    """One Claude call (or one batch result) and what it cost (#168). The month's sum of
    `cost_usd` is checked against `AI_MONTHLY_BUDGET_USD` before every new call."""

    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_user_id_created_at", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Which feature made the call, e.g. "inbox_suggestions".
    feature: Mapped[str] = mapped_column(String(50))
    # The model that answered, which can differ from the one asked for after a fallback.
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer)
    cache_write_tokens: Mapped[int] = mapped_column(Integer)
    # Batch API results bill at half price.
    batch: Mapped[bool] = mapped_column(Boolean)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
