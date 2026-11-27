"""Mileage log for Schedule C line 9 under the standard mileage rate (#167): business
trips, saved trip templates, the year's odometer readings and the per-user IRS rates.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class MileageTrip(UUIDPk, Timestamps, Base):
    __tablename__ = "mileage_trips"
    __table_args__ = (
        CheckConstraint("miles > 0", name="miles_positive"),
        CheckConstraint("purpose <> ''", name="purpose_not_empty"),
        Index("ix_mileage_trips_user_id_date", "user_id", "date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date)
    miles: Mapped[Decimal] = mapped_column(Numeric(8, 1))
    purpose: Mapped[str] = mapped_column(String(200))
    start_location: Mapped[str | None] = mapped_column(String(200))
    end_location: Mapped[str | None] = mapped_column(String(200))


class SavedTrip(UUIDPk, Timestamps, Base):
    """A trip template; logging it creates a `MileageTrip` dated today."""

    __tablename__ = "saved_trips"
    __table_args__ = (
        CheckConstraint("miles > 0", name="miles_positive"),
        CheckConstraint("purpose <> ''", name="purpose_not_empty"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    miles: Mapped[Decimal] = mapped_column(Numeric(8, 1))
    purpose: Mapped[str] = mapped_column(String(200))
    start_location: Mapped[str | None] = mapped_column(String(200))
    end_location: Mapped[str | None] = mapped_column(String(200))


class OdometerReading(UUIDPk, Timestamps, Base):
    """The car's odometer at the start and end of a tax year (Form 4562 / Schedule C
    Part IV). `end_reading` stays NULL until the year is over.
    """

    __tablename__ = "odometer_readings"
    __table_args__ = (
        UniqueConstraint("user_id", "year"),
        CheckConstraint("start_reading >= 0", name="start_reading_non_negative"),
        CheckConstraint(
            "end_reading IS NULL OR end_reading >= start_reading",
            name="end_reading_after_start_reading",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    year: Mapped[int] = mapped_column(SmallInteger)
    start_reading: Mapped[Decimal] = mapped_column(Numeric(8, 1))
    end_reading: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))


class MileageRate(UUIDPk, Timestamps, Base):
    """Dollars per business mile from `effective_from` until the next row's date."""

    __tablename__ = "mileage_rates"
    __table_args__ = (
        UniqueConstraint("user_id", "effective_from"),
        CheckConstraint("rate > 0", name="rate_positive"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    effective_from: Mapped[dt.date] = mapped_column(Date)
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 4))
