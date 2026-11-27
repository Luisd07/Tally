"""Category creation and updates. See `app.scoping` for the user-scoping helpers."""

import uuid

from sqlalchemy.orm import Session

from app.models import Category
from app.schemas.categories import CategoryCreate, CategoryUpdate
from app.scoping import get_owned, owned
from app.services.crud import apply_patch, create_idempotent

_UNIQUE_CONFLICTS = {"uq_categories_user_id_name": "A category with this name already exists."}


def list_categories(db: Session, user_id: uuid.UUID) -> list[Category]:
    """Every category, archived ones included: clients resolve old transactions' names from it."""
    return list(db.scalars(owned(Category, user_id).order_by(Category.name)))


def create_category(db: Session, user_id: uuid.UUID, data: CategoryCreate) -> tuple[Category, bool]:
    """Insert a new category, or hand back the caller's existing row for a repeated id.

    A duplicate name for this user is a separate conflict: the id-level ON CONFLICT clause
    doesn't cover it, so it still raises and is mapped to 409 here.
    """
    return create_idempotent(
        db, Category, user_id, data.model_dump(), unique_conflicts=_UNIQUE_CONFLICTS
    )


def update_category(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, data: CategoryUpdate
) -> Category:
    category = get_owned(db, Category, id, user_id)
    return apply_patch(db, category, data, unique_conflicts=_UNIQUE_CONFLICTS)
