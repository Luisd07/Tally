"""Every /api route behind `PROTECTED_ROUTERS` must answer 401 without a token.

Coverage is derived from the routes themselves, not a path prefix: the only routes
exempt are the ones actually mounted on the open `auth.router` (enrollment and
sign-in ceremonies, refresh, signout). `/api/auth/events` also starts with
`/api/auth/`, but it's a separate router mounted through `PROTECTED_ROUTERS`
(`app/main.py`), so it's covered here like any other protected route, with no
allowlist to maintain.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import tokens
from app.config import Settings
from app.deps import CurrentUser
from app.main import create_app
from app.models import User
from app.routers import auth


def _open_paths() -> set[str]:
    """Every path FastAPI resolves for the literal open `auth.router`, mounted the
    same way `app.main.create_app` mounts it, read back out of its own OpenAPI schema.
    """
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    return set(app.openapi()["paths"])


def _protected_routes() -> list[tuple[str, str]]:
    # fastapi 0.143.1 wraps included routers in `_IncludedRouter`, so `app.routes`
    # no longer holds `APIRoute` instances for them; read the resolved paths back
    # out of the OpenAPI schema instead.
    app = create_app(Settings(environment="test", log_level="WARNING"))
    open_paths = _open_paths()
    routes = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/") or path in open_paths:
            continue
        resolved = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        routes.extend((method.upper(), resolved) for method in operations)
    return routes


_PROTECTED_ROUTES = _protected_routes()


def test_protected_routes_found() -> None:
    assert _PROTECTED_ROUTES
    assert ("GET", "/api/accounts") in _PROTECTED_ROUTES
    assert ("GET", "/api/categories") in _PROTECTED_ROUTES
    assert ("GET", "/api/auth/events") in _PROTECTED_ROUTES


@pytest.mark.parametrize(("method", "path"), _PROTECTED_ROUTES)
def test_route_requires_token(client: TestClient, method: str, path: str) -> None:
    r = client.request(method, path)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


# The routers are still empty, so exercise CurrentUser through a throwaway route.
@pytest.fixture
def me_app(app: FastAPI) -> FastAPI:
    router = APIRouter()

    @router.get("/api/_test/me")
    def me(user: CurrentUser) -> dict[str, str]:
        return {"id": str(user.id)}

    app.include_router(router)
    return app


def _token(settings: Settings, user_id: uuid.UUID, **overrides: object) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": str(user_id),
        "typ": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    payload.update(overrides)
    return jwt.encode(payload, settings.jwt_signing_key.get_secret_value(), algorithm="HS256")


def test_valid_token_is_accepted(
    me_app: FastAPI, db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.get("/api/_test/me", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"id": str(user.id)}


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": datetime.now(UTC) - timedelta(seconds=1)},
        {"typ": "refresh"},
        {"aud": "someone-else"},
        {"iss": "someone-else"},
    ],
    ids=["expired", "wrong-type", "wrong-audience", "wrong-issuer"],
)
def test_bad_token_is_rejected(
    me_app: FastAPI,
    db_client: TestClient,
    user: User,
    settings: Settings,
    overrides: dict[str, object],
) -> None:
    token = _token(settings, user.id, **overrides)
    r = db_client.get("/api/_test/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_token_signed_with_another_key_is_rejected(settings: Settings) -> None:
    other = settings.model_copy(update={"jwt_signing_key": SecretStr("x" * 40)})
    token = tokens.create_access_token(uuid.uuid4(), other)
    assert tokens.decode_access_token(token, settings) is None


def test_token_for_unknown_user_is_rejected(
    me_app: FastAPI, db_client: TestClient, settings: Settings
) -> None:
    token = tokens.create_access_token(uuid.uuid4(), settings)
    r = db_client.get("/api/_test/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
