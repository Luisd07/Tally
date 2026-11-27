import uuid

from fastapi import APIRouter, Response, status

from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.rules import RuleCreate, RuleOut, RuleUpdate
from app.services import rules as service

router = APIRouter()


@router.get("")
def list_rules(db: DB, user: CurrentUser) -> list[RuleOut]:
    rows = service.list_rules(db, user.id)
    return [RuleOut.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_rule(body: RuleCreate, response: Response, db: DB, user: CurrentUser) -> RuleOut:
    row, created = service.create_rule(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return RuleOut.model_validate(row)


@router.patch("/{id}")
def update_rule(id: uuid.UUID, body: RuleUpdate, db: DB, user: CurrentUser) -> RuleOut:
    row = service.update_rule(db, user.id, id, body)
    db.commit()
    return RuleOut.model_validate(row)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    service.delete_rule(db, user.id, id)
    db.commit()
