from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, model_validator


class Schema(BaseModel):
    """Base for API schemas; reads straight from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class PatchSchema(Schema):
    """Partial-update schema: every field is optional, but a NOT NULL column may
    not be cleared with an explicit `null`. Subclasses list those fields in
    `_required`; everything else stays clearable with `null`.
    """

    _required: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _reject_null_on_required(self) -> Self:
        nulled = sorted(
            field
            for field in self._required
            if field in self.model_fields_set and getattr(self, field) is None
        )
        if nulled:
            raise ValueError(f"{', '.join(nulled)} cannot be null")
        return self
