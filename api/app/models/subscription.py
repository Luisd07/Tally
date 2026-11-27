import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import Bucket, Cadence, SubscriptionKind


class Subscription(UUIDPk, Timestamps, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="end_date_after_start_date",
        ),
        CheckConstraint("amount < 0", name="amount_negative"),
        CheckConstraint(
            "business_pct IS NULL OR (business_pct >= 0 AND business_pct <= 100)",
            name="business_pct_range",
        ),
        Index("ix_subscriptions_user_id_next_due", "user_id", "next_due"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    # Car payment vs. a recurring subscription (#44's totals split on this).
    kind: Mapped[SubscriptionKind] = mapped_column(
        Enum(SubscriptionKind, native_enum=False, length=16),
        default=SubscriptionKind.subscription,
        server_default="subscription",
    )
    # SimpleFIN's sign: bills are outflows, so this is negative like transactions.amount.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    cadence: Mapped[Cadence] = mapped_column(Enum(Cadence, native_enum=False, length=16))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    bucket: Mapped[Bucket] = mapped_column(
        Enum(Bucket, native_enum=False, length=16),
        default=Bucket.personal,
        server_default="personal",
    )
    next_due: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    # Share of each matched charge that is business (#129): the sync-time matcher splits
    # a charge at this percentage. NULL and 0 leave the charge's bucket alone.
    business_pct: Mapped[int | None] = mapped_column(SmallInteger)
    # What a bank description must contain to match (#139); the name when NULL.
    match_text: Mapped[str | None] = mapped_column(String(200))
