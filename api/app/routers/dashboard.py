from typing import Annotated

from fastapi import APIRouter, Query

from app.deps import DB, CurrentUser
from app.schemas.dashboard import DashboardOut, DashboardQuery
from app.services import dashboard as service

router = APIRouter()


@router.get("")
def get_dashboard(
    query: Annotated[DashboardQuery, Query()], db: DB, user: CurrentUser
) -> DashboardOut:
    return service.get_dashboard(db, user.id, query)
