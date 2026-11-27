import uuid

from fastapi import APIRouter, Response, status

from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.categories import (
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
)
from app.services import categories

router = APIRouter()


@router.get("")
def list_categories(db: DB, user: CurrentUser) -> list[CategoryOut]:
    rows = categories.list_categories(db, user.id)
    return [CategoryOut.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_category(
    body: CategoryCreate, db: DB, user: CurrentUser, response: Response
) -> CategoryOut:
    category, created = categories.create_category(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return CategoryOut.model_validate(category)


@router.patch("/{id}")
def update_category(id: uuid.UUID, body: CategoryUpdate, db: DB, user: CurrentUser) -> CategoryOut:
    category = categories.update_category(db, user.id, id, body)
    db.commit()
    return CategoryOut.model_validate(category)
