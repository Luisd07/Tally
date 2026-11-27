import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class Event(UUIDPk, Timestamps, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="end_date_after_start_date"),
        Index("ix_events_user_id_start_date", "user_id", "start_date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    series: Mapped[str | None] = mapped_column(String(100))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(200))
