import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.income import IncomeCreate, IncomeListQuery, IncomeOut, IncomePage, IncomeUpdate
from app.services import income as service

router = APIRouter()


@router.get("")
def list_income(
    query: Annotated[IncomeListQuery, Query()], db: DB, user: CurrentUser
) -> IncomePage:
    return service.list_income(db, user.id, query)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_income(body: IncomeCreate, db: DB, user: CurrentUser, response: Response) -> IncomeOut:
    row, created = service.create_income(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return IncomeOut.model_validate(row)


@router.patch("/{id}")
def update_income(id: uuid.UUID, body: IncomeUpdate, db: DB, user: CurrentUser) -> IncomeOut:
    row = service.update_income(db, user.id, id, body)
    db.commit()
    return IncomeOut.model_validate(row)
