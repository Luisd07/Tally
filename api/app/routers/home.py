from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.deps import DB, CurrentUser
from app.schemas.home import HomeOut, HomeQuery
from app.services import home as service

router = APIRouter()


@router.get("")
def get_home(query: Annotated[HomeQuery, Query()], db: DB, user: CurrentUser) -> HomeOut:
    return service.get_home(db, user.id, query, datetime.now(UTC))
