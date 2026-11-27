"""Shared request dependencies. Every data query is scoped through `CurrentUser`."""

from typing import Annotated

import httpx
from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth import tokens
from app.config import Settings
from app.db import get_db
from app.errors import Unauthorized
from app.models import User

bearer = HTTPBearer(auto_error=False)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


AppSettings = Annotated[Settings, Depends(get_app_settings)]
DB = Annotated[Session, Depends(get_db)]


def get_simplefin_transport() -> httpx.BaseTransport | None:
    """The transport for calls to SimpleFIN. `None` uses httpx's real transport;
    tests override this dependency to inject `httpx.MockTransport`.
    """
    return None


SimplefinTransport = Annotated[httpx.BaseTransport | None, Depends(get_simplefin_transport)]


def get_current_user(
    db: DB,
    settings: AppSettings,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if creds is None:
        raise Unauthorized()
    user_id = tokens.decode_access_token(creds.credentials, settings)
    if user_id is None:
        raise Unauthorized()
    user = db.get(User, user_id)
    if user is None:
        raise Unauthorized()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def no_store(response: Response) -> None:
    """Mark a response `Cache-Control: no-store`. Used by every route that hands back tokens."""
    response.headers["Cache-Control"] = "no-store"
