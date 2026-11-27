import uuid
from typing import Literal

from pydantic import Field, field_validator

from app.models.enums import Bucket
from app.schemas.base import Schema
from app.schemas.paging import decode_date_cursor
from app.schemas.transactions import TransactionOut


class InboxItem(TransactionOut):
    """A transaction needing attention plus what the user last did with the same merchant."""

    # A split parent whose bank amount changed on sync: its children no longer sum to it.
    needs_resplit: bool
    # A manual entry still unmatched to a bank row after `UNMATCHED_AFTER`.
    unmatched_manual: bool
    suggested_category_id: uuid.UUID | None
    suggested_bucket: Bucket | None
    # Where the suggestion came from: the last reviewed row with the same merchant, or
    # Claude (#169) when history has none. Null with no suggestion.
    suggestion_source: Literal["history", "ai"] | None
    # A clean merchant name to set along with the category; only AI suggestions carry one.
    suggested_merchant: str | None
    # Two or more reviewed rows for this merchant already have the suggested category, so
    # confirming this one is worth offering as a rule.
    rule_worthy: bool


class InboxPage(Schema):
    items: list[InboxItem]
    next_cursor: str | None
    # Every inbox row the user has, independent of paging: the tab's badge count.
    total: int


class InboxQuery(Schema):
    """Query params for `GET /api/inbox`; `cursor` is the same opaque value as
    `TransactionListQuery.cursor`.
    """

    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        if value is not None:
            decode_date_cursor(value)
        return value
