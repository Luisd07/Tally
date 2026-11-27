from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.deps import DB, CurrentUser
from app.schemas.inbox import InboxPage, InboxQuery
from app.services import inbox as service

router = APIRouter()


@router.get("")
def list_inbox(query: Annotated[InboxQuery, Query()], db: DB, user: CurrentUser) -> InboxPage:
    return service.list_inbox(db, user.id, query, datetime.now(UTC))
