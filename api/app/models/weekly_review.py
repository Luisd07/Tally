import uuid
from datetime import date

from sqlalchemy import Date, Enum, ForeignKey, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import FindingKind


class WeeklyReview(UUIDPk, Timestamps, Base):
    """Claude's short summary of one Monday-to-Sunday week (#173), for Home."""

    __tablename__ = "weekly_reviews"
    __table_args__ = (UniqueConstraint("user_id", "week_start"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    week_start: Mapped[date] = mapped_column(Date)
    week_end: Mapped[date] = mapped_column(Date)
    summary: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100))


class ReviewFinding(UUIDPk, Timestamps, Base):
    """One thing a weekly review flagged, tied to its transaction."""

    __tablename__ = "review_findings"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("weekly_reviews.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[FindingKind] = mapped_column(Enum(FindingKind, native_enum=False, length=20))
    # SET NULL if the transaction is deleted; the matcher repoints it when rows merge.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    note: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(SmallInteger)
