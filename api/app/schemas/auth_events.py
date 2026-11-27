"""Request/response schemas for `GET /api/auth/events` (#22)."""

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.models.enums import AuthEventKind
from app.schemas.base import Schema
from app.schemas.paging import decode_cursor as _decode_cursor


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    return _decode_cursor(cursor, datetime.fromisoformat)


class AuthEventOut(Schema):
    id: uuid.UUID
    kind: AuthEventKind
    ip: str | None
    user_agent: str | None
    created_at: datetime


class AuthEventPage(Schema):
    items: list[AuthEventOut]
    next_cursor: str | None


class AuthEventListQuery(Schema):
    """Query params for `GET /api/auth/events`.

    `cursor` is opaque to the client: base64 of `<created_at>|<id>`, the last row of
    the previous page, ordered the same way as the listing (`created_at` DESC, `id`
    DESC).
    """

    kind: AuthEventKind | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        if value is not None:
            decode_cursor(value)
        return value
