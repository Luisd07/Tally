import uuid

from sqlalchemy import Enum, ForeignKey, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import TaxReviewKind, TaxReviewStatus


class TaxReview(UUIDPk, Timestamps, Base):
    """Claude's year-end notes for the tax preparer (#175). One per user and year; asking
    again replaces it. The worker writes it, since it can take a few minutes."""

    __tablename__ = "tax_reviews"
    __table_args__ = (UniqueConstraint("user_id", "year"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    year: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[TaxReviewStatus] = mapped_column(
        Enum(TaxReviewStatus, native_enum=False, length=16)
    )
    summary: Mapped[str | None] = mapped_column(Text)
    # Why the latest run failed, for the Tax screen.
    error: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(100))


class TaxReviewItem(UUIDPk, Timestamps, Base):
    """One thing a tax review noted, tied to its transaction when it's about one."""

    __tablename__ = "tax_review_items"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tax_reviews.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[TaxReviewKind] = mapped_column(Enum(TaxReviewKind, native_enum=False, length=20))
    # SET NULL if the transaction is deleted; the matcher repoints it when rows merge.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    note: Mapped[str] = mapped_column(String(1000))
    position: Mapped[int] = mapped_column(SmallInteger)
