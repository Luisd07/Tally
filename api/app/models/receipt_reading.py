import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Numeric, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import ReceiptReadingStatus


class ReceiptReading(UUIDPk, Timestamps, Base):
    """What Claude read off one receipt image (#170). Shown for the user to confirm; nothing
    is applied to the transaction on its own."""

    __tablename__ = "receipt_readings"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[ReceiptReadingStatus] = mapped_column(
        Enum(ReceiptReadingStatus, native_enum=False, length=16)
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    merchant: Mapped[str | None] = mapped_column(String(200))
    receipt_date: Mapped[date | None] = mapped_column(Date)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    tip: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # [{"description": str, "amount": "12.34"}], amounts as decimal strings.
    line_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    # Null when there were no line items to check.
    adds_up: Mapped[bool | None] = mapped_column(Boolean)
    # Whether the total is the transaction's amount (a tip added later breaks this).
    matches_transaction: Mapped[bool | None] = mapped_column(Boolean)
    model: Mapped[str | None] = mapped_column(String(100))
