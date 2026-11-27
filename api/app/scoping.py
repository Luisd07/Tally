"""Query helpers that scope every row lookup to the current user's id.

See `.claude/rules/api.md` Data: every data table has `user_id`, and every query
filters on the current user's id.
"""

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Mapped, Session
from sqlalchemy.sql import Select

from app.errors import NotFound


class _Owned(Protocol):
    id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]


def get_owned[ModelT: _Owned](
    db: Session, model: type[ModelT], id: uuid.UUID, user_id: uuid.UUID
) -> ModelT:
    """Fetch a row by id, scoped to `user_id`.

    Raises `NotFound` both when the row doesn't exist and when it belongs to someone
    else, via a single query, so the two responses are identical.
    """
    row = db.scalars(select(model).where(model.id == id, model.user_id == user_id)).one_or_none()
    if row is None:
        raise NotFound()
    return row


def owned[ModelT: _Owned](model: type[ModelT], user_id: uuid.UUID) -> Select[ModelT]:
    """A select for every row of `model` belonging to `user_id`."""
    return select(model).where(model.user_id == user_id)
