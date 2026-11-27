import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import ApnsEnvironment


class Device(UUIDPk, Timestamps, Base):
    __tablename__ = "devices"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Unique so registering again updates the row, and an APNs "unregistered" reply finds it.
    apns_token: Mapped[str] = mapped_column(String(200), unique=True)
    environment: Mapped[ApnsEnvironment] = mapped_column(
        Enum(ApnsEnvironment, native_enum=False, length=16)
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
