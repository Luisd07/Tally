import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import IncomeType


class IncomeEntry(UUIDPk, Timestamps, Base):
    __tablename__ = "income_entries"
    __table_args__ = (
        # Pay you expect and pay that landed: at least one side has to be known.
        CheckConstraint(
            "expected IS NOT NULL OR received IS NOT NULL", name="expected_or_received"
        ),
        # Income is a positive deposit; it never carries SimpleFIN's negative outflow sign.
        CheckConstraint("expected >= 0", name="expected_non_negative"),
        CheckConstraint("received >= 0", name="received_non_negative"),
        Index("ix_income_entries_user_id_date", "user_id", "date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date)
    type: Mapped[IncomeType] = mapped_column(Enum(IncomeType, native_enum=False, length=16))
    description: Mapped[str | None] = mapped_column(String(500))
    # expected and received are positive deposits: pay you expect or pay that landed.
    expected: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    received: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # The matched deposit; nullable until the matcher (or you) link one.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    # The race weekend this pay belongs to, so trip pay can be tied together.
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
