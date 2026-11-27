import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import ConfigDict, Field, field_validator

from app.models.enums import Bucket, Cadence, SubscriptionKind
from app.schemas.base import PatchSchema, Schema
from app.schemas.rules import _reject_blank_pattern
from app.schemas.types import Name100, NegativeMoney, Pattern200

BusinessPct = Annotated[int, Field(ge=0, le=100)]


class SubscriptionCreate(Schema):
    """Fields the phone may set on create. `amount` follows SimpleFIN's sign: a bill or
    subscription is an outflow, so it's negative, same as `transactions.amount`.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: Name100
    amount: NegativeMoney
    cadence: Cadence
    kind: SubscriptionKind = SubscriptionKind.subscription
    account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    bucket: Bucket = Bucket.personal
    next_due: date | None = None
    active: bool = True
    start_date: date | None = None
    end_date: date | None = None
    business_pct: BusinessPct | None = None
    match_text: Pattern200 | None = None

    _validate_match_text = field_validator("match_text")(_reject_blank_pattern)


class SubscriptionUpdate(PatchSchema):
    """Partial update; only fields present in the request body are applied."""

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"name", "amount", "cadence", "kind", "bucket", "active"})

    name: Name100 | None = None
    amount: NegativeMoney | None = None
    cadence: Cadence | None = None
    kind: SubscriptionKind | None = None
    account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    bucket: Bucket | None = None
    next_due: date | None = None
    active: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    business_pct: BusinessPct | None = None
    match_text: Pattern200 | None = None

    _validate_match_text = field_validator("match_text")(_reject_blank_pattern)


class PriceChange(Schema):
    """The subscription's stored amount against its latest linked charge."""

    old: Decimal
    new: Decimal


class SubscriptionFlags(Schema):
    """Computed on read, never stored. Everything off for an inactive subscription."""

    price_change: PriceChange | None = None
    missed: bool = False
    dormant: bool = False


class SubscriptionOut(Schema):
    id: uuid.UUID
    name: str
    amount: Decimal
    cadence: Cadence
    kind: SubscriptionKind
    account_id: uuid.UUID | None
    category_id: uuid.UUID | None
    bucket: Bucket
    next_due: date | None
    active: bool
    start_date: date | None
    end_date: date | None
    business_pct: int | None
    match_text: str | None
    created_at: datetime
    updated_at: datetime
    flags: SubscriptionFlags = SubscriptionFlags()


class SubscriptionSuggestion(Schema):
    """A detected recurring charge, shaped so its fields can be POSTed to
    `/api/subscriptions` as-is to accept it.
    """

    name: str
    amount: Decimal
    cadence: Cadence
    next_due: date
    charge_count: int
    transaction_ids: list[uuid.UUID]


class SubscriptionTotals(Schema):
    """Monthly and annual cost, normalized across cadences. Negative, same sign as the
    rows summed into it.
    """

    monthly: Decimal
    annual: Decimal


class SubscriptionPage(Schema):
    items: list[SubscriptionOut]
    totals: dict[SubscriptionKind, SubscriptionTotals]
