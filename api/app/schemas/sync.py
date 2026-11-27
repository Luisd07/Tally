import uuid
from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from app.models.enums import RunStatus, RunTrigger
from app.schemas.base import Schema
from app.schemas.paging import decode_cursor as _decode_cursor
from app.schemas.types import Text2048


class SimplefinClaimRequest(Schema):
    model_config = ConfigDict(extra="forbid")

    setup_token: Text2048


class SimplefinClaimResponse(Schema):
    """Never the Access URL: only when it was claimed and which host it points at."""

    claimed_at: datetime
    host: str


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    return _decode_cursor(cursor, datetime.fromisoformat)


class SyncRunOut(Schema):
    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    trigger: RunTrigger
    new_count: int
    updated_count: int
    errors: list[dict[str, Any]]


class SyncRunPage(Schema):
    items: list[SyncRunOut]
    next_cursor: str | None


class SyncRunListQuery(Schema):
    """Query params for `GET /api/sync/runs`.

    `cursor` is opaque to the client: base64 of `<started_at>|<id>`, the last row of
    the previous page, ordered the same way as the listing (`started_at` DESC, `id`
    DESC).
    """

    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        if value is not None:
            decode_cursor(value)
        return value
