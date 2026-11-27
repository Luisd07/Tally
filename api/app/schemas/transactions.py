import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.models.enums import Bucket, Source
from app.schemas.base import PatchSchema, Schema
from app.schemas.months import Month
from app.schemas.paging import decode_date_cursor
from app.schemas.types import Money, Text200, Text500, Text2000


class TransactionCreate(Schema):
    """A manual capture. The phone mints `id`, so a retried request lands on the same row."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    account_id: uuid.UUID
    posted_date: date
    amount: Money
    description_raw: Text500
    merchant: Text200 | None = None
    category_id: uuid.UUID | None = None
    bucket: Bucket = Bucket.personal
    notes: Text2000 | None = None
    transacted_at: datetime | None = None


class TransactionUpdate(PatchSchema):
    """Every field is optional; only the ones present in the request body are applied."""

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"bucket", "reviewed"})

    category_id: uuid.UUID | None = None
    bucket: Bucket | None = None
    notes: Text2000 | None = None
    reviewed: bool | None = None
    merchant: Text200 | None = None
    event_id: uuid.UUID | None = None


class TransactionOut(Schema):
    id: uuid.UUID
    account_id: uuid.UUID
    posted_date: date
    transacted_at: datetime | None
    amount: Decimal
    description_raw: str
    merchant: str | None
    category_id: uuid.UUID | None
    bucket: Bucket
    source: Source
    external_id: str | None
    pending: bool
    reviewed: bool
    notes: str | None
    event_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    reimbursement_id: uuid.UUID | None
    # True when this row has been split: at least one other transaction carries it as
    # `parent_id`. Not a column -- required (no default) so a serializer that bypasses
    # `to_out`/`to_out_many` (the only producers that compute it) fails loudly instead
    # of silently reporting every split parent as unsplit.
    is_split: bool
    created_at: datetime
    updated_at: datetime


class TransactionBulkUpdate(Schema):
    """`POST /api/transactions/bulk`: one `patch` applied to every row in `ids`."""

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    patch: TransactionUpdate

    @field_validator("ids")
    @classmethod
    def _unique_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("ids must be unique")
        return value

    @model_validator(mode="after")
    def _patch_not_empty(self) -> Self:
        if not self.patch.model_fields_set:
            raise ValueError("patch must set at least one field")
        return self


class TransactionBulkResult(Schema):
    updated: int
    items: list[TransactionOut]


class TransactionSplitPart(Schema):
    """One child row of a `POST /api/transactions/{id}/split`.

    `category_id`/`bucket`/`event_id` default to the parent's own value when the
    part doesn't set them at all; an explicit value overrides, and an explicit
    `null` clears it on that child. `merchant`/`notes` are never inherited -- a part
    that omits them just leaves that child without one.
    """

    model_config = ConfigDict(extra="forbid")

    amount: Money
    category_id: uuid.UUID | None = None
    bucket: Bucket | None = None
    event_id: uuid.UUID | None = None
    notes: Text2000 | None = None
    merchant: Text200 | None = None

    @field_validator("amount")
    @classmethod
    def _nonzero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("amount must not be zero")
        return value


class TransactionSplit(Schema):
    """`POST /api/transactions/{id}/split`: replace a transaction with 2..20 parts.

    Each part's amount must share the parent's sign; the service checks that against
    the parent's amount, and that the parts sum to it exactly, since neither is
    knowable from the request body alone.
    """

    model_config = ConfigDict(extra="forbid")

    parts: list[TransactionSplitPart] = Field(min_length=2, max_length=20)


class TransactionSplitResult(Schema):
    parent: TransactionOut
    children: list[TransactionOut]


class TransactionPage(Schema):
    items: list[TransactionOut]
    next_cursor: str | None


class TransactionListQuery(Schema):
    """Query params for `GET /api/transactions`.

    `cursor` is opaque to the client: base64 of `<posted_date>|<id>`, the last row of the
    previous page, ordered the same way as the listing (`posted_date` DESC, `id` DESC).

    `format=csv` returns every matching row (not paginated) as a CSV file instead of a
    `TransactionPage`. `cursor`/`limit` have no meaning for an unbounded export, so the
    CSV path ignores them rather than rejecting the request: FastAPI fills an unset
    `limit` in from its own default before this model ever sees the raw query string, so
    a request without one would otherwise be indistinguishable from `limit=100` sent on
    purpose, and the only way to tell them apart would be a 422 on a lot of harmless
    `?format=csv&limit=100` requests too.
    """

    month: Month | None = None
    account_id: uuid.UUID | None = None
    bucket: Bucket | None = None
    category_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    # Only the caller's own rows are ever matched, so another user's report id
    # returns an empty list, the same as a foreign `event_id`.
    reimbursement_id: uuid.UUID | None = None
    reviewed: bool | None = None
    pending: bool | None = None
    # A pre-split parent's own row stays in the list by default (see `TransactionOut.
    # is_split`); set this to drop it, e.g. for a view that only wants to sum amounts.
    exclude_split_parents: bool = False
    # #52/#53's Inbox flag: a manual row still unmatched `UNMATCHED_AFTER` (7 days)
    # after its own `posted_date` (see `app.services.matching`).
    unmatched_manual: bool = False
    q: Text200 | None = None
    format: Literal["json", "csv"] = "json"
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def _validate_cursor(cls, value: str | None) -> str | None:
        if value is not None:
            decode_date_cursor(value)
        return value
