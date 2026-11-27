import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import CategoryKind


class Category(UUIDPk, Timestamps, Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        CheckConstraint("deductible_pct BETWEEN 0 AND 100", name="deductible_pct_range"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[CategoryKind] = mapped_column(Enum(CategoryKind, native_enum=False, length=16))
    # A name such as "travel", not a Schedule C line number; the numbers move between tax years.
    tax_line: Mapped[str | None] = mapped_column(String(50))
    deductible_pct: Mapped[int] = mapped_column(SmallInteger, default=100, server_default="100")
    monthly_budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
