"""AI-proposed changes (#222): the steps an action may take, and how it's shown.

Each step reuses the request schema of the endpoint that would make the same change by
hand, so a step is checked exactly as that request would be. Steps that create a row
carry its id, so later steps in the same action can refer to it and undo can find it.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    computed_field,
    field_validator,
    model_validator,
)

from app.models.enums import AIActionSource, AIActionStatus
from app.schemas.base import PatchSchema, Schema
from app.schemas.categories import CategoryCreate
from app.schemas.rules import RuleCreate
from app.schemas.subscriptions import SubscriptionCreate, SubscriptionUpdate
from app.schemas.transactions import TransactionBulkUpdate
from app.schemas.types import Money, Name100

MAX_STEPS = 20
# Distinct transactions one action may change, across all its steps.
MAX_TRANSACTIONS = 500


def _require_id[CreateT: CategoryCreate | RuleCreate | SubscriptionCreate](
    value: CreateT,
) -> CreateT:
    if value.id is None:
        raise ValueError("id is required")
    return value


class CategorizeTransactionsStep(TransactionBulkUpdate):
    """One patch applied to every transaction in `ids`, as `POST /api/transactions/bulk`."""

    kind: Literal["categorize_transactions"]


class MarkReviewedStep(Schema):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["mark_reviewed"]
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)

    @field_validator("ids")
    @classmethod
    def _unique_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("ids must be unique")
        return value


class CreateCategoryStep(Schema):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["create_category"]
    category: CategoryCreate

    _validate_category = field_validator("category")(_require_id)


class CategoryStepPatch(PatchSchema):
    """What an update_category step may change: not the kind or tax settings, which would
    quietly move a category between spending and income."""

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"name", "archived"})

    name: Name100 | None = None
    monthly_budget: Money | None = None
    archived: bool | None = None


class UpdateCategoryStep(Schema):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["update_category"]
    id: uuid.UUID
    patch: CategoryStepPatch

    @model_validator(mode="after")
    def _patch_not_empty(self) -> Self:
        if not self.patch.model_fields_set:
            raise ValueError("patch must set at least one field")
        return self


class CreateRuleStep(Schema):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["create_rule"]
    rule: RuleCreate

    _validate_rule = field_validator("rule")(_require_id)


class CreateSubscriptionStep(Schema):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["create_subscription"]
    subscription: SubscriptionCreate

    _validate_subscription = field_validator("subscription")(_require_id)


class UpdateSubscriptionStep(Schema):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["update_subscription"]
    id: uuid.UUID
    patch: SubscriptionUpdate

    @model_validator(mode="after")
    def _patch_not_empty(self) -> Self:
        if not self.patch.model_fields_set:
            raise ValueError("patch must set at least one field")
        return self


ActionStep = Annotated[
    CategorizeTransactionsStep
    | MarkReviewedStep
    | CreateCategoryStep
    | UpdateCategoryStep
    | CreateRuleStep
    | CreateSubscriptionStep
    | UpdateSubscriptionStep,
    Field(discriminator="kind"),
]
ActionSteps = Annotated[list[ActionStep], Field(min_length=1, max_length=MAX_STEPS)]

# Parses stored `ai_actions.steps` back into steps. Store them with `dump_steps`: a patch
# keeps only the fields it sets, so an unset field never reads back as an explicit null.
STEPS: TypeAdapter[list[ActionStep]] = TypeAdapter(ActionSteps)


def dump_steps(steps: list[ActionStep]) -> list[dict[str, Any]]:
    return STEPS.dump_python(steps, mode="json", exclude_unset=True)


def _transaction_ids(steps: list[ActionStep]) -> set[uuid.UUID]:
    return {
        id
        for step in steps
        if isinstance(step, CategorizeTransactionsStep | MarkReviewedStep)
        for id in step.ids
    }


class ActionProposal(Schema):
    """What Claude proposes; checked before anything is stored."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=300)
    steps: ActionSteps

    @model_validator(mode="after")
    def _transactions_within_limit(self) -> Self:
        if len(_transaction_ids(self.steps)) > MAX_TRANSACTIONS:
            raise ValueError(
                f"an action may change at most {MAX_TRANSACTIONS} distinct transactions"
            )
        return self


class ActionEffects(BaseModel):
    """What an action really changes, counted from its steps rather than its summary."""

    transactions: int
    categories_created: int
    categories_updated: int
    rules_created: int
    subscriptions_created: int
    subscriptions_updated: int

    @classmethod
    def of(cls, steps: list[ActionStep]) -> Self:
        created = {
            step.subscription.id for step in steps if isinstance(step, CreateSubscriptionStep)
        }
        updated = {step.id for step in steps if isinstance(step, UpdateSubscriptionStep)}
        new_categories = {
            step.category.id for step in steps if isinstance(step, CreateCategoryStep)
        }
        updated_categories = {step.id for step in steps if isinstance(step, UpdateCategoryStep)}
        return cls(
            transactions=len(_transaction_ids(steps)),
            categories_created=sum(isinstance(step, CreateCategoryStep) for step in steps),
            categories_updated=len(updated_categories - new_categories),
            rules_created=sum(isinstance(step, CreateRuleStep) for step in steps),
            subscriptions_created=len(created),
            subscriptions_updated=len(updated - created),
        )


class AIActionOut(Schema):
    id: uuid.UUID
    summary: str
    status: AIActionStatus
    source: AIActionSource
    steps: ActionSteps
    created_at: datetime
    applied_at: datetime | None
    error: str | None

    @computed_field
    @property
    def effects(self) -> ActionEffects:
        return ActionEffects.of(self.steps)
