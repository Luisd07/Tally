import datetime as dt
import uuid
from decimal import Decimal

from pydantic import ConfigDict, Field

from app.models.enums import ReimbursementStatus
from app.schemas.base import PatchSchema, Schema
from app.schemas.types import Text500


class ReimbursementCreate(Schema):
    """`POST /api/reimbursements`: start a draft expense report, or attach more
    transactions to it.

    `transaction_ids` are only attached when this call actually creates the row
    (`created=True` from `create_idempotent`); a repeated `id` returns the existing
    row unchanged, the same as every other idempotent create. Use `PATCH .../{id}`
    with `add_transaction_ids` to attach more to a report that already exists.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    note: Text500 | None = None
    transaction_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class ReimbursementUpdate(PatchSchema):
    """`PATCH /api/reimbursements/{id}`.

    `status` only moves forward one step at a time, `draft` -> `submitted` ->
    `paid` (see `app.services.reimbursements.update_reimbursement`); setting it to
    its current value is a no-op rather than a 422, so a request that only touches
    other fields doesn't have to omit it. `add_transaction_ids`/
    `remove_transaction_ids` are only honored while the report's current status is
    `draft`.
    """

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"status"})

    status: ReimbursementStatus | None = None
    submitted_date: dt.date | None = None
    paid_date: dt.date | None = None
    income_entry_id: uuid.UUID | None = None
    note: Text500 | None = None
    add_transaction_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    remove_transaction_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)


class ReimbursementOut(Schema):
    id: uuid.UUID
    status: ReimbursementStatus
    submitted_date: dt.date | None
    paid_date: dt.date | None
    income_entry_id: uuid.UUID | None
    note: str | None
    # Sum of the linked transactions' amounts as a positive owed magnitude (`sum(
    # -amount)`), the same convention `app.services.dashboard` uses for spend: these
    # are reimbursable-bucket expenses, normally SimpleFIN outflows (negative), and
    # the report is about what's owed back to you, not a raw ledger balance.
    total: Decimal
    transaction_count: int
    created_at: dt.datetime
    updated_at: dt.datetime


class ReimbursementPage(Schema):
    items: list[ReimbursementOut]


class ReimbursementListQuery(Schema):
    """Query params for `GET /api/reimbursements`."""

    status: ReimbursementStatus | None = None


class OwedOut(Schema):
    """`GET /api/reimbursements/owed`: what you are currently owed (spec
    "Reimbursable expenses"), for the Home tab (#45).
    """

    owed: Decimal
