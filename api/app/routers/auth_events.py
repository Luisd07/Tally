from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps import DB, no_store
from app.schemas.auth_events import AuthEventListQuery, AuthEventPage
from app.services import auth_events as service

router = APIRouter()


@router.get("", dependencies=[Depends(no_store)])
def list_events(query: Annotated[AuthEventListQuery, Query()], db: DB) -> AuthEventPage:
    """Recent sign-in activity, newest first (#22).

    Mounted at `/api/auth/events` through `PROTECTED_ROUTERS` (`app/main.py`), same as
    every other data router, so `get_current_user` comes from the shared mechanism
    instead of a per-route dependency. The listing isn't scoped to the caller though —
    see `app.services.auth_events` for why.
    """
    return service.list_auth_events(db, query)
