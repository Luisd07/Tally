import enum
import uuid

from sqlalchemy import Boolean, CheckConstraint, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class SignConvention(enum.StrEnum):
    """How the CSV's single amount column signs money. `negative_is_out` already matches
    SimpleFIN's sign; `positive_is_out` (a card export listing charges as positives) is
    flipped on the way in. Unused for debit/credit column pairs, which carry their own sign.
    """

    negative_is_out = "negative_is_out"
    positive_is_out = "positive_is_out"


class ImportMapping(UUIDPk, Timestamps, Base):
    """The saved CSV column mapping for one account (#54), so a bank's export is mapped once.

    Columns are header names when `has_header`, else 0-based column indexes as strings.
    """

    __tablename__ = "import_mappings"
    __table_args__ = (
        CheckConstraint(
            "(amount_column IS NOT NULL AND debit_column IS NULL AND credit_column IS NULL)"
            " OR (amount_column IS NULL"
            " AND debit_column IS NOT NULL AND credit_column IS NOT NULL)",
            name="amount_or_debit_credit",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), unique=True
    )
    date_column: Mapped[str] = mapped_column(String(100))
    # Either one signed amount column, or a debit and a credit column.
    amount_column: Mapped[str | None] = mapped_column(String(100))
    debit_column: Mapped[str | None] = mapped_column(String(100))
    credit_column: Mapped[str | None] = mapped_column(String(100))
    description_column: Mapped[str] = mapped_column(String(100))
    # A `strptime` format, e.g. "%m/%d/%Y".
    date_format: Mapped[str] = mapped_column(String(40))
    sign_convention: Mapped[SignConvention] = mapped_column(
        Enum(SignConvention, native_enum=False, length=20),
        default=SignConvention.negative_is_out,
        server_default=SignConvention.negative_is_out.value,
    )
    has_header: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
