import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.reimbursements import (
    OwedOut,
    ReimbursementCreate,
    ReimbursementListQuery,
    ReimbursementOut,
    ReimbursementPage,
    ReimbursementUpdate,
)
from app.services import reimbursements as service

router = APIRouter()


@router.get("")
def list_reimbursements(
    query: Annotated[ReimbursementListQuery, Query()], db: DB, user: CurrentUser
) -> ReimbursementPage:
    return service.list_reimbursements(db, user.id, query)


@router.get("/owed")
def get_owed(db: DB, user: CurrentUser) -> OwedOut:
    return OwedOut(owed=service.owed_to_you(db, user.id))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_reimbursement(
    body: ReimbursementCreate, db: DB, user: CurrentUser, response: Response
) -> ReimbursementOut:
    row, created = service.create_reimbursement(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return service.to_out(db, user.id, row)


@router.patch("/{id}")
def update_reimbursement(
    id: uuid.UUID, body: ReimbursementUpdate, db: DB, user: CurrentUser
) -> ReimbursementOut:
    row = service.update_reimbursement(db, user.id, id, body)
    db.commit()
    return service.to_out(db, user.id, row)
