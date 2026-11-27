import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import Bucket, Source


class Transaction(UUIDPk, Timestamps, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # A repeated sync upserts on (account_id, external_id); manual rows have no external_id
        # and a plain UNIQUE constraint lets any number of them share NULL.
        UniqueConstraint("account_id", "external_id"),
        Index("ix_transactions_user_id_posted_date", "user_id", "posted_date"),
        Index("ix_transactions_account_id_posted_date", "account_id", "posted_date"),
        Index("ix_transactions_category_id", "category_id"),
        Index("ix_transactions_reimbursement_id", "reimbursement_id"),
        Index(
            "ix_transactions_unreviewed",
            "user_id",
            postgresql_where=text("reviewed = false"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    posted_date: Mapped[date] = mapped_column(Date)
    # Pending rows carry posted=0 from SimpleFIN; fall back to this until it posts.
    transacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SimpleFIN's sign: negative is money leaving the account.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    description_raw: Mapped[str] = mapped_column(String(500))
    merchant: Mapped[str | None] = mapped_column(String(200))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    bucket: Mapped[Bucket] = mapped_column(
        Enum(Bucket, native_enum=False, length=16),
        default=Bucket.personal,
        server_default="personal",
    )
    source: Mapped[Source] = mapped_column(Enum(Source, native_enum=False, length=16))
    # NULL for manual rows; synced and CSV rows carry the bank's id.
    external_id: Mapped[str | None] = mapped_column(String(200))
    pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(String(2000))
    # Set by the transfer matcher (#61); linking a card payment to its other side.
    transfer_pair_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    # Set when this row is a split of another; deleting the parent deletes its splits.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE")
    )
    # Set by the subscription matcher (#56); linking a bill payment to its subscription.
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    # The race weekend this charge belongs to, so trip costs can be tied together.
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    # The expense report this reimbursable charge has been filed under (#64).
    # `use_alter=True`: transactions -> reimbursements -> income_entries -> transactions
    # is a real three-table cycle (`income_entries.transaction_id` already existed),
    # which SQLAlchemy can't topologically sort; this tells it the constraint is applied
    # separately (an ALTER, same as the migration already does) instead of warning (and,
    # with this suite's warnings-as-errors, failing `test_migrations_match_models`).
    reimbursement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reimbursements.id", ondelete="SET NULL", use_alter=True)
    )
    # Hash of account, date, amount and normalized description, for matching rows with no bank
    # id (CSV import, manual-to-bank matching). Not unique: two identical charges can land the
    # same day. Left for #48/#54 to compute; nullable until something populates it.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
