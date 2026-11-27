import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from app.models.enums import CategoryKind
from app.schemas.base import PatchSchema, Schema
from app.schemas.types import Money, Name100, Percent, Text50


class CategoryCreate(Schema):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: Name100
    kind: CategoryKind
    # A name such as "travel", not a Schedule C line number; the numbers move between
    # tax years, so this is a free string rather than an enum.
    tax_line: Text50 | None = None
    deductible_pct: Percent = 100
    monthly_budget: Money | None = None
    archived: bool = False


class CategoryUpdate(PatchSchema):
    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"name", "kind", "deductible_pct", "archived"})

    name: Name100 | None = None
    kind: CategoryKind | None = None
    tax_line: Text50 | None = None
    deductible_pct: Percent | None = None
    monthly_budget: Money | None = None
    archived: bool | None = None


class CategoryOut(Schema):
    id: uuid.UUID
    name: str
    kind: CategoryKind
    tax_line: str | None
    deductible_pct: int
    monthly_budget: Decimal | None
    archived: bool
    created_at: datetime
    updated_at: datetime
