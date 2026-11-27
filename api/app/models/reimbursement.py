import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, Date, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import ReimbursementStatus


class Reimbursement(UUIDPk, Timestamps, Base):
    """One expense report; `transactions.reimbursement_id` points back at it (spec
    "Data model": *"One expense report; its transactions point at it"*).
    """

    __tablename__ = "reimbursements"
    __table_args__ = (
        # Filing a report means dating it; paying one means dating that too.
        CheckConstraint(
            "status NOT IN ('submitted', 'paid') OR submitted_date IS NOT NULL",
            name="submitted_date_required_when_submitted",
        ),
        CheckConstraint(
            "status <> 'paid' OR paid_date IS NOT NULL", name="paid_date_required_when_paid"
        ),
        CheckConstraint(
            "submitted_date IS NULL OR paid_date IS NULL OR paid_date >= submitted_date",
            name="paid_date_after_submitted_date",
        ),
        Index("ix_reimbursements_user_id_status", "user_id", "status"),
        # One deposit settles at most one report (NULLs never collide).
        Index("uq_reimbursements_income_entry_id", "income_entry_id", unique=True),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[ReimbursementStatus] = mapped_column(
        Enum(ReimbursementStatus, native_enum=False, length=16),
        default=ReimbursementStatus.draft,
        server_default="draft",
    )
    submitted_date: Mapped[dt.date | None] = mapped_column(Date)
    paid_date: Mapped[dt.date | None] = mapped_column(Date)
    # The matched deposit; mark-paid links it (spec: "Mark paid links the income entry").
    income_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("income_entries.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(String(500))
