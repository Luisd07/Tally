import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import AccountKind


class Account(UUIDPk, Timestamps, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # One row per SimpleFIN account per user, so a repeated sync upserts instead of duplicating.
        UniqueConstraint("user_id", "simplefin_account_id"),
        CheckConstraint("due_day BETWEEN 1 AND 31", name="due_day_range"),
        CheckConstraint("apr BETWEEN 0 AND 100", name="apr_range"),
        CheckConstraint("last4 ~ '^[0-9]{4}$'", name="last4_digits"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[AccountKind] = mapped_column(Enum(AccountKind, native_enum=False, length=16))
    institution: Mapped[str | None] = mapped_column(String(100))
    simplefin_account_id: Mapped[str | None] = mapped_column(String(200))
    last4: Mapped[str | None] = mapped_column(String(4))
    due_day: Mapped[int | None] = mapped_column(SmallInteger)
    planned_payment: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    apr: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    promo_apr_end: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    @property
    def is_synced(self) -> bool:
        """Whether the nightly sync owns this account's balances, not a manual entry."""
        return self.simplefin_account_id is not None
