import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import RunStatus, RunTrigger


class SyncRun(UUIDPk, Timestamps, Base):
    """One row per SimpleFIN pull (#48), scheduled or manual, logging its outcome."""

    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_user_id_started_at", "user_id", "started_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, native_enum=False, length=16))
    trigger: Mapped[RunTrigger] = mapped_column(Enum(RunTrigger, native_enum=False, length=16))
    new_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The errlist SimpleFIN returned, sanitized down to {code, msg, conn_id, account_id}:
    # never the Access URL or its credentials.
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
