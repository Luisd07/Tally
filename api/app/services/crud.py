"""Generic CRUD helpers shared by every resource service: idempotent create and
partial update, including unique-violation-to-Conflict translation.

See `app.scoping` for by-id lookups and list queries.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from psycopg.errors import UniqueViolation
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import Conflict, UnprocessableEntity
from app.scoping import _Owned, get_owned


def _raise_conflict(exc: IntegrityError, unique_conflicts: Mapping[str, str]) -> None:
    constraint = isinstance(exc.orig, UniqueViolation) and exc.orig.diag.constraint_name
    if constraint and constraint in unique_conflicts:
        raise Conflict(unique_conflicts[constraint]) from exc


def create_idempotent[ModelT: _Owned](
    db: Session,
    model: type[ModelT],
    user_id: uuid.UUID,
    values: dict[str, Any],
    *,
    unique_conflicts: Mapping[str, str] | None = None,
) -> tuple[ModelT, bool]:
    """Insert a new row, or hand back the caller's existing row for a repeated id.

    One atomic INSERT ... ON CONFLICT (id) DO NOTHING ... RETURNING, then a scoped
    SELECT, so a retried capture can never race into a duplicate. Returns
    `(row, created)`; `created` is `False` when the id already belonged to this user.
    A row with the same id owned by another user raises `Conflict`.

    `unique_conflicts` maps a constraint name to the message raised when some other
    unique index on the table rejects the insert; only a savepoint rolls back.
    """
    row_id = values.pop("id", None) or uuid.uuid4()
    stmt = (
        insert(model)
        .values(id=row_id, user_id=user_id, **values)
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(model)
    )
    try:
        with db.begin_nested():
            row = db.scalars(stmt).one_or_none()
    except IntegrityError as exc:
        _raise_conflict(exc, unique_conflicts or {})
        raise
    if row is not None:
        return row, True

    existing = db.scalars(select(model).where(model.id == row_id)).one()
    if existing.user_id != user_id:
        raise Conflict(f"This id belongs to another {model.__name__.lower()}.")
    return existing, False


def merged(obj: Any, patch: BaseModel, field: str) -> Any:
    """The value `field` will have once `patch` is applied: `patch`'s value if the
    request set it, otherwise `obj`'s current value.

    Lets a check that spans two fields see the full picture even when a PATCH only
    touches one of them. Passing the same object for `obj` and `patch` (a create,
    with no row yet) collapses to `patch`'s own field.
    """
    return getattr(patch, field) if field in patch.model_fields_set else getattr(obj, field)


def check_date_order(
    obj: Any, patch: BaseModel, *, start: str = "start_date", end: str = "end_date"
) -> None:
    """Raise `UnprocessableEntity` when `end` would land before `start`, checked
    against the merged result so a PATCH that only touches one side of the pair is
    checked against the full picture rather than just the half it sent.
    """
    start_value = merged(obj, patch, start)
    end_value = merged(obj, patch, end)
    if start_value is not None and end_value is not None and end_value < start_value:
        raise UnprocessableEntity.value_error(f"{end} must be on or after {start}")


def check_owned_refs(
    db: Session, user_id: uuid.UUID, data: BaseModel, refs: Mapping[str, type[_Owned]]
) -> None:
    """Raise `NotFound` for any FK in `refs` that's set on `data` but isn't the
    caller's row.

    `refs` maps a field name on `data` to the model it points at, e.g.
    `{"account_id": Account, "category_id": Category}`. A field left `None` (whether
    by default or an explicit clear) has nothing to check; the DB doesn't enforce
    same-user FKs, so every other value is confirmed with `get_owned`.
    """
    for field, model in refs.items():
        value = getattr(data, field)
        if value is not None:
            get_owned(db, model, value, user_id)


def apply_patch[ModelT: _Owned](
    db: Session,
    obj: ModelT,
    data: BaseModel,
    *,
    unique_conflicts: Mapping[str, str] | None = None,
) -> ModelT:
    """Apply every field in `data.model_fields_set` to `obj`, then flush.

    Only a savepoint rolls back on a listed unique violation, which is translated to
    `Conflict`; anything else re-raises.
    """
    for field in data.model_fields_set:
        setattr(obj, field, getattr(data, field))
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        _raise_conflict(exc, unique_conflicts or {})
        raise
    return obj
