"""AI-proposed changes (#222).

Claude never changes data itself. It proposes an action: a short summary and a list of
steps, each the same change the app could make by hand. The action is checked and stored
as `proposed`, and nothing happens until the user confirms it. Confirming applies every
step at once through the same services the app uses, and records what undo needs. Undo
reverts every step at once, refusing if anything it would revert was changed since.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from fastapi.encoders import jsonable_encoder
from loguru import logger
from pydantic import BaseModel, ValidationError
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.errors import AppError, Conflict, NotFound, UnprocessableEntity
from app.models import (
    Account,
    AIAction,
    Base,
    Category,
    ChatConversation,
    Event,
    Rule,
    Subscription,
    Transaction,
)
from app.models.enums import AIActionSource, AIActionStatus
from app.schemas.ai_actions import (
    STEPS,
    ActionProposal,
    ActionStep,
    CategorizeTransactionsStep,
    CreateCategoryStep,
    CreateRuleStep,
    CreateSubscriptionStep,
    MarkReviewedStep,
    UpdateCategoryStep,
    UpdateSubscriptionStep,
    dump_steps,
)
from app.schemas.categories import CategoryUpdate
from app.schemas.subscriptions import SubscriptionUpdate
from app.schemas.transactions import TransactionUpdate
from app.scoping import _Owned, get_owned, owned
from app.services.categories import create_category, update_category
from app.services.rules import create_rule, delete_rule
from app.services.subscriptions import create_subscription, update_subscription
from app.services.transactions import bulk_update_transactions, update_transaction

LIST_LIMIT = 100
# A proposal older than this is refused at confirm: what it was based on has likely moved on.
EXPIRES_AFTER = timedelta(days=7)

type _Ref = tuple[type[_Owned], uuid.UUID]


def _references(step: ActionStep) -> list[_Ref]:
    """Every existing row `step` points at."""
    refs: list[tuple[type[_Owned], uuid.UUID | None]] = []
    match step:
        case CategorizeTransactionsStep(ids=ids, patch=patch):
            refs += [(Transaction, id) for id in ids]
            refs += [(Category, patch.category_id), (Event, patch.event_id)]
        case MarkReviewedStep(ids=ids):
            refs += [(Transaction, id) for id in ids]
        case CreateCategoryStep():
            pass
        case UpdateCategoryStep(id=id):
            refs.append((Category, id))
        case CreateRuleStep(rule=rule):
            refs.append((Category, rule.set_category_id))
        case CreateSubscriptionStep(subscription=sub):
            refs += [(Account, sub.account_id), (Category, sub.category_id)]
        case UpdateSubscriptionStep(id=id, patch=patch):
            refs += [(Subscription, id), (Account, patch.account_id), (Category, patch.category_id)]
    return [(model, id) for model, id in refs if id is not None]


def _created(step: ActionStep) -> _Ref | None:
    """The new row `step` creates, if any. Its id is required by the schema."""
    match step:
        case CreateCategoryStep(category=category) if category.id:
            return Category, category.id
        case CreateRuleStep(rule=rule) if rule.id:
            return Rule, rule.id
        case CreateSubscriptionStep(subscription=sub) if sub.id:
            return Subscription, sub.id
    return None


def _error(index: int, msg: str) -> dict[str, Any]:
    return {"type": "value_error", "loc": ["body", "steps", index], "msg": f"Value error, {msg}"}


def _check_ids(db: Session, user_id: uuid.UUID, steps: Sequence[ActionStep]) -> None:
    """Raise `UnprocessableEntity` naming every id that isn't the user's own row or one an
    earlier step creates, and every new id that's already taken."""
    errors: list[dict[str, Any]] = []
    created: dict[type[_Owned], set[uuid.UUID]] = defaultdict(set)
    referenced: list[tuple[int, type[_Owned], uuid.UUID]] = []
    new: list[tuple[int, type[_Owned], uuid.UUID]] = []
    for index, step in enumerate(steps):
        referenced += [
            (index, model, id) for model, id in _references(step) if id not in created[model]
        ]
        if (made := _created(step)) is not None:
            model, id = made
            if id in created[model]:
                errors.append(_error(index, f"{model.__name__.lower()} id {id} is created twice"))
            created[model].add(id)
            new.append((index, model, id))

    wanted: dict[type[_Owned], set[uuid.UUID]] = defaultdict(set)
    for _, model, id in referenced:
        wanted[model].add(id)
    found = {
        model: set(db.scalars(select(model.id).where(model.id.in_(ids), model.user_id == user_id)))
        for model, ids in wanted.items()
    }
    errors += [
        _error(index, f"unknown {model.__name__.lower()} id {id}")
        for index, model, id in referenced
        if id not in found[model]
    ]

    budgeted = {
        step.id
        for step in steps
        if isinstance(step, UpdateCategoryStep)
        and "monthly_budget" in step.patch.model_fields_set
        and step.patch.archived is not False
    }
    archived = set(
        db.scalars(
            select(Category.id).where(
                Category.id.in_(budgeted), Category.user_id == user_id, Category.archived
            )
        )
    )
    errors += [
        _error(index, f"category {step.id} is archived; unarchive it to change its budget")
        for index, step in enumerate(steps)
        if isinstance(step, UpdateCategoryStep)
        and step.id in archived
        and "monthly_budget" in step.patch.model_fields_set
        and step.patch.archived is not False
    ]

    taken: set[uuid.UUID] = set()
    for model, ids in created.items():
        taken.update(db.scalars(select(model.id).where(model.id.in_(ids))))
    errors += [
        _error(index, f"{model.__name__.lower()} id {id} already exists")
        for index, model, id in new
        if id in taken
    ]
    if errors:
        raise UnprocessableEntity(details=errors)


def propose_action(
    db: Session,
    user_id: uuid.UUID,
    summary: str,
    steps: Sequence[Any],
    source: AIActionSource,
    conversation_id: uuid.UUID | None = None,
) -> AIAction:
    """Check and store a proposed action. Flushes, never commits.

    `steps` may be raw JSON (as Claude sends it) or `ActionStep`s. Raises
    `UnprocessableEntity` for a malformed step or an id the user can't use, and `NotFound`
    for someone else's `conversation_id`."""
    try:
        proposal = ActionProposal.model_validate({"summary": summary, "steps": list(steps)})
    except ValidationError as exc:
        details = [
            {k: v for k, v in err.items() if k not in ("input", "ctx", "url")}
            | {"loc": ["body", *err["loc"]]}
            for err in exc.errors()
        ]
        raise UnprocessableEntity(details=jsonable_encoder(details)) from exc
    _check_ids(db, user_id, proposal.steps)
    if conversation_id is not None:
        get_owned(db, ChatConversation, conversation_id, user_id)

    action = AIAction(
        user_id=user_id,
        summary=proposal.summary,
        status=AIActionStatus.proposed,
        source=source,
        conversation_id=conversation_id,
        steps=dump_steps(proposal.steps),
    )
    db.add(action)
    db.flush()
    logger.info(
        "AI action {} proposed from {} with {} steps", action.id, source, len(proposal.steps)
    )
    return action


def list_actions(
    db: Session, user_id: uuid.UUID, status: AIActionStatus | None = None
) -> list[AIAction]:
    """The user's newest actions first, at most `LIST_LIMIT`."""
    stmt = owned(AIAction, user_id)
    if status is not None:
        stmt = stmt.where(AIAction.status == status)
    stmt = stmt.order_by(AIAction.created_at.desc(), AIAction.id.desc()).limit(LIST_LIMIT)
    return list(db.scalars(stmt))


def get_action(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> AIAction:
    return get_owned(db, AIAction, id, user_id)


def _lock(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> AIAction:
    """The user's action, locked so two requests can't both change it. Raises `NotFound`
    for someone else's."""
    action = db.scalars(
        owned(AIAction, user_id).where(AIAction.id == id).with_for_update()
    ).one_or_none()
    if action is None:
        raise NotFound()
    return action


def _lock_proposed(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> AIAction:
    """Raises `NotFound` for someone else's action and `Conflict` unless it's still
    `proposed`."""
    action = _lock(db, user_id, id)
    if action.status != AIActionStatus.proposed:
        raise Conflict(f"Only a proposed action can change; this one is {action.status}.")
    return action


def _before(row: object, patch: BaseModel) -> dict[str, Any]:
    """`row`'s current values of exactly the fields `patch` sets, as JSON."""
    fields = patch.model_fields_set
    values = {field: getattr(row, field) for field in fields}
    return (
        type(patch)
        .model_construct(_fields_set=fields, **values)
        .model_dump(mode="json", exclude_unset=True)
    )


def _patch_transactions(
    db: Session, user_id: uuid.UUID, kind: str, ids: list[uuid.UUID], patch: TransactionUpdate
) -> dict[str, Any]:
    rows = db.scalars(
        select(Transaction)
        .where(Transaction.id.in_(ids), Transaction.user_id == user_id)
        .with_for_update()
    ).all()
    before = {row.id: _before(row, patch) for row in rows}
    updated = bulk_update_transactions(db, user_id, ids, patch)
    return {
        "kind": kind,
        "rows": [
            {"id": str(row.id), "before": before[row.id], "updated_at": row.updated_at.isoformat()}
            for row in updated
        ],
    }


def _apply_step(db: Session, user_id: uuid.UUID, step: ActionStep) -> dict[str, Any]:
    """Apply one step and return what undoing it needs."""
    match step:
        case CategorizeTransactionsStep(ids=ids, patch=patch):
            return _patch_transactions(db, user_id, step.kind, ids, patch)
        case MarkReviewedStep(ids=ids):
            reviewed = TransactionUpdate(reviewed=True)
            return _patch_transactions(db, user_id, step.kind, ids, reviewed)
        case CreateCategoryStep(category=category):
            row, created = create_category(db, user_id, category)
        case CreateRuleStep(rule=rule):
            row, created = create_rule(db, user_id, rule)
        case CreateSubscriptionStep(subscription=sub):
            row, created = create_subscription(db, user_id, sub)
        case UpdateCategoryStep(id=id, patch=patch):
            before = _before(get_owned(db, Category, id, user_id), patch)
            row = update_category(
                db, user_id, id, CategoryUpdate.model_validate(patch.model_dump(exclude_unset=True))
            )
            return {
                "kind": step.kind,
                "id": str(row.id),
                "before": before,
                "updated_at": row.updated_at.isoformat(),
            }
        case UpdateSubscriptionStep(id=id, patch=patch):
            before = _before(get_owned(db, Subscription, id, user_id), patch)
            row = update_subscription(db, user_id, id, patch)
            return {
                "kind": step.kind,
                "id": str(row.id),
                "before": before,
                "updated_at": row.updated_at.isoformat(),
            }
    # Propose rejected taken ids, so an existing row now means something changed since.
    if not created:
        raise Conflict(f"{type(row).__name__} {row.id} already exists.")
    return {"kind": step.kind, "id": str(row.id)}


def _failure(steps: list[ActionStep], index: int, exc: Exception) -> str:
    if not steps:
        return "This suggestion couldn't be read, so nothing was changed."
    index = min(index, len(steps) - 1)
    match exc:
        case NotFound():
            reason = "something it changes or uses no longer exists."
        case UnprocessableEntity(details=[_, *_] as details):
            reason = "; ".join(str(detail["msg"]) for detail in details)
        case AppError():
            reason = exc.detail
        case IntegrityError() | StaleDataError():
            reason = "it conflicts with your current data."
        case _:
            reason = "it couldn't be saved."
    return f"Step {index + 1} ({steps[index].kind}) couldn't be applied: {reason}"[:500]


def _changed_since_proposed(
    db: Session, user_id: uuid.UUID, action: AIAction, steps: list[ActionStep]
) -> str | None:
    """Why the action can't apply because a row a step patches was edited after it was
    proposed, or `None`. Locks those rows so nothing changes them before the steps do."""
    wanted: dict[type[Transaction] | type[Subscription] | type[Category], set[uuid.UUID]] = (
        defaultdict(set)
    )
    for step in steps:
        match step:
            case CategorizeTransactionsStep(ids=ids) | MarkReviewedStep(ids=ids):
                wanted[Transaction].update(ids)
            case UpdateCategoryStep(id=id):
                wanted[Category].add(id)
            case UpdateSubscriptionStep(id=id):
                wanted[Subscription].add(id)
            case _:
                pass
    changed: list[str] = []
    for model, ids in wanted.items():
        stmt = owned(model, user_id).where(model.id.in_(ids)).with_for_update()
        changed += [
            f"{model.__name__.lower()} {row.id}"
            for row in db.scalars(stmt.execution_options(populate_existing=True))
            if row.updated_at > action.created_at
        ]
    if not changed:
        return None
    return (
        "This suggestion can't be applied because things it changes were changed since this "
        "was suggested: " + "; ".join(changed) + "."
    )[:500]


def confirm_action(db: Session, user_id: uuid.UUID, id: uuid.UUID, now: datetime) -> AIAction:
    """Apply every step of a proposed action, in order, all or nothing. Flushes, never
    commits.

    On success the action is `applied` and `undo` holds, per step, what reverting it
    needs. The action is `failed` with an `error`, and nothing is applied, when it was
    proposed more than `EXPIRES_AFTER` ago, when a row a step patches was edited since it
    was proposed, or when any step fails; the caller still commits so that outcome is
    kept. Raises `NotFound` for someone else's action and `Conflict` unless it's
    `proposed`."""
    action = _lock_proposed(db, user_id, id)
    steps: list[ActionStep] = []
    undo: list[dict[str, Any]] = []
    error: str | None = None
    if now - action.created_at > EXPIRES_AFTER:
        error = "This suggestion expired; ask again for a fresh one."
    else:
        try:
            with db.begin_nested():
                steps = STEPS.validate_python(action.steps)
                error = _changed_since_proposed(db, user_id, action, steps)
                if error is None:
                    for step in steps:
                        undo.append(_apply_step(db, user_id, step))
        except (AppError, SQLAlchemyError, ValidationError) as exc:
            error = _failure(steps, len(undo), exc)
            logger.warning("AI action {} failed at step {}: {}", action.id, len(undo) + 1, exc)
    if error is not None:
        action.status = AIActionStatus.failed
        action.error = error
        db.flush()
        return action

    action.status = AIActionStatus.applied
    action.applied_at = now
    action.undo = undo
    db.flush()
    logger.info("AI action {} applied {} steps", action.id, len(steps))
    return action


def dismiss_action(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> AIAction:
    """Raises `NotFound` for someone else's action and `Conflict` unless it's `proposed`."""
    action = _lock_proposed(db, user_id, id)
    action.status = AIActionStatus.dismissed
    db.flush()
    return action


_CREATED: dict[str, type[_Owned]] = {
    "create_category": Category,
    "create_rule": Rule,
    "create_subscription": Subscription,
}
_UPDATED: dict[str, type[_Owned]] = {
    "update_category": Category,
    "update_subscription": Subscription,
}

# A row undo touches, and the `updated_at` apply left it with. `None` for a row the action
# created: created_at and updated_at start out equal, so any later edit tells them apart.
type _Target = tuple[type[_Owned], uuid.UUID, datetime | None]


def _targets(undo: list[dict[str, Any]]) -> list[_Target]:
    targets: list[_Target] = []
    for entry in undo:
        match entry:
            case {"rows": rows}:
                targets += [
                    (Transaction, uuid.UUID(row["id"]), datetime.fromisoformat(row["updated_at"]))
                    for row in rows
                ]
            case {"kind": kind, "id": id, "updated_at": updated_at}:
                targets.append((_UPDATED[kind], uuid.UUID(id), datetime.fromisoformat(updated_at)))
            case {"kind": kind, "id": id}:
                targets.append((_CREATED[kind], uuid.UUID(id), None))
    return targets


def _changed_since(db: Session, user_id: uuid.UUID, undo: list[dict[str, Any]]) -> list[str]:
    """Every row undo would touch that's gone or was edited after apply, as "<kind> <id>
    <reason>". Locks the rest so nothing changes them before undo does."""
    targets = _targets(undo)
    wanted: dict[type[_Owned], set[uuid.UUID]] = defaultdict(set)
    for model, id, _ in targets:
        wanted[model].add(id)
    found: dict[uuid.UUID, Any] = {}
    for model, ids in wanted.items():
        stmt = owned(model, user_id).where(model.id.in_(ids)).with_for_update()
        found.update(
            (row.id, row) for row in db.scalars(stmt.execution_options(populate_existing=True))
        )

    problems: dict[uuid.UUID, str] = {}
    for model, id, updated_at in targets:
        row = found.get(id)
        name = f"{model.__name__.lower()} {id}"
        if row is None:
            problems[id] = f"{name} no longer exists"
        elif row.updated_at != (row.created_at if updated_at is None else updated_at):
            problems.setdefault(id, f"{name} was changed since")
    return list(problems.values())


def _in_use(db: Session, model: type[Base], id: uuid.UUID) -> bool:
    """Whether any row references this one through a foreign key. Read from the schema,
    so a reference added later is covered without changing this."""
    columns = [
        fk.parent
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table is model.__table__
    ]
    return any(db.scalar(select(exists().where(column == id))) for column in columns)


def _remove_category(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> None:
    """Delete the category, or archive it if anything still references it."""
    category = get_owned(db, Category, id, user_id)
    if _in_use(db, Category, id):
        category.archived = True
    else:
        db.delete(category)
    db.flush()


def _remove_subscription(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> None:
    """Delete the subscription, or deactivate it if anything still references it."""
    subscription = get_owned(db, Subscription, id, user_id)
    if _in_use(db, Subscription, id):
        subscription.active = False
    else:
        db.delete(subscription)
    db.flush()


def _undo_entry(db: Session, user_id: uuid.UUID, entry: dict[str, Any]) -> None:
    match entry:
        case {"rows": rows}:
            for row in rows:
                patch = TransactionUpdate.model_validate(row["before"])
                update_transaction(db, user_id, uuid.UUID(row["id"]), patch)
        case {"kind": "update_category", "id": id, "before": before}:
            update_category(db, user_id, uuid.UUID(id), CategoryUpdate.model_validate(before))
        case {"kind": "update_subscription", "id": id, "before": before}:
            update_subscription(
                db, user_id, uuid.UUID(id), SubscriptionUpdate.model_validate(before)
            )
        case {"kind": "create_category", "id": id}:
            _remove_category(db, user_id, uuid.UUID(id))
        case {"kind": "create_rule", "id": id}:
            delete_rule(db, user_id, uuid.UUID(id))
        case {"kind": "create_subscription", "id": id}:
            _remove_subscription(db, user_id, uuid.UUID(id))


def undo_action(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> AIAction:
    """Revert every step of an applied action, last step first, all or nothing. Flushes,
    never commits.

    Transaction, category and subscription patches get their before-values back, created rules are
    deleted, and a created category or subscription is deleted unless something still
    references it, in which case it's archived or deactivated. Raises `NotFound` for
    someone else's action, and `Conflict` unless it's `applied` or when a row it would
    revert is gone or was changed since; then nothing is changed."""
    action = _lock(db, user_id, id)
    if action.status != AIActionStatus.applied:
        raise Conflict(f"Only an applied action can be undone; this one is {action.status}.")
    undo = action.undo or []
    if problems := _changed_since(db, user_id, undo):
        raise Conflict(
            "This action can't be undone because things it changed were edited or removed since: "
            + "; ".join(problems)
            + "."
        )
    try:
        with db.begin_nested():
            for entry in reversed(undo):
                _undo_entry(db, user_id, entry)
    except (AppError, IntegrityError, StaleDataError) as exc:
        raise Conflict(
            "This action can't be undone: something it would restore no longer exists or "
            "conflicts with your current data."
        ) from exc

    action.status = AIActionStatus.undone
    db.flush()
    logger.info("AI action {} undone", action.id)
    return action
