import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class Receipt(UUIDPk, Timestamps, Base):
    __tablename__ = "receipts"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # RESTRICT: matchers (#52, #50) must move a receipt to its new transaction before
    # deleting the old one, so tax evidence never disappears via a cascade.
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT")
    )
    # receipts/YYYY/MM/<uuid>.jpg, relative to settings.receipts_dir.
    file_path: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(50))
    # Not unique: the same receipt image can be attached again (e.g. after a re-match).
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
