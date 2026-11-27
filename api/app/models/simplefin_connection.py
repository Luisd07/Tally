import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk


class SimplefinConnection(UUIDPk, Timestamps, Base):
    """One row per user: the most recently claimed SimpleFIN Access URL, encrypted.

    UNIQUE(user_id): one active connection per user is enough (#47); claiming a new
    setup token upserts, replacing the stored URL rather than adding a second row.
    """

    __tablename__ = "simplefin_connections"
    __table_args__ = (UniqueConstraint("user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    access_url_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
