import uuid
from datetime import datetime
from typing import Self

from pydantic import ConfigDict, field_validator, model_validator

from app.models.enums import Bucket, MatchField, MatchType
from app.schemas.base import PatchSchema, Schema
from app.schemas.types import Pattern200, Priority, Text200


def _reject_blank_pattern(value: str | None) -> str | None:
    """A pattern of only whitespace normalizes (collapse + uppercase) to the empty
    string, which is a substring and a prefix of everything -- a `contains` or
    `starts_with` rule with that pattern would match every transaction. `Pattern200`'s
    `min_length=1` alone doesn't catch this, since `"   "` has length 3.
    """
    if value is not None and not value.strip():
        raise ValueError("pattern must not be blank")
    return value


class RuleCreate(Schema):
    """Fields the phone may set on create. At least one `set_*` field must be
    given -- a rule that sets nothing would match transactions and change
    nothing about them.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    priority: Priority
    match_field: MatchField
    match_type: MatchType
    pattern: Pattern200
    set_merchant: Text200 | None = None
    set_category_id: uuid.UUID | None = None
    set_bucket: Bucket | None = None
    active: bool = True

    _validate_pattern = field_validator("pattern")(_reject_blank_pattern)

    @model_validator(mode="after")
    def _at_least_one_set_field(self) -> Self:
        if self.set_merchant is None and self.set_category_id is None and self.set_bucket is None:
            raise ValueError("set_merchant, set_category_id or set_bucket must be set")
        return self


class RuleUpdate(PatchSchema):
    """Partial update; only fields present in the request body are applied.

    The at-least-one-`set_*` check against the merged result happens in
    `app.services.rules.update_rule`, since this schema alone can't see the
    row's other `set_*` fields when only one is sent.
    """

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"priority", "match_field", "match_type", "pattern", "active"})

    priority: Priority | None = None
    match_field: MatchField | None = None
    match_type: MatchType | None = None
    pattern: Pattern200 | None = None
    set_merchant: Text200 | None = None
    set_category_id: uuid.UUID | None = None
    set_bucket: Bucket | None = None
    active: bool | None = None

    _validate_pattern = field_validator("pattern")(_reject_blank_pattern)


class RuleOut(Schema):
    id: uuid.UUID
    priority: int
    match_field: MatchField
    match_type: MatchType
    pattern: str
    set_merchant: str | None
    set_category_id: uuid.UUID | None
    set_bucket: Bucket | None
    active: bool
    created_at: datetime
    updated_at: datetime
