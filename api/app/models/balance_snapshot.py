import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class BalanceSnapshot(UUIDPk, Timestamps, Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = (
        # One row per account per day; a repeated sync upserts instead of duplicating.
        UniqueConstraint("account_id", "date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date)
    # SimpleFIN's sign as reported by the account: card debt is negative, consistent with
    # transactions.amount. Writers (#48 sync, #62 manual, sheet import) must store it that way.
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2))
