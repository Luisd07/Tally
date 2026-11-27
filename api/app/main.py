from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.deps import get_current_user
from app.errors import install_error_handlers
from app.log import setup_logging
from app.middleware import RequestContextMiddleware
from app.routers import (
    accounts,
    ai,
    auth,
    auth_events,
    backup,
    categories,
    dashboard,
    devices,
    events,
    health,
    home,
    imports,
    inbox,
    income,
    insights,
    mileage,
    receipts,
    reimbursements,
    rules,
    subscriptions,
    sync,
    tax,
    transactions,
)

PROTECTED_ROUTERS = [
    (backup, "/api/backup"),
    (devices, "/api/devices"),
    (home, "/api/home"),
    (inbox, "/api/inbox"),
    (transactions, "/api/transactions"),
    (receipts, "/api/receipts"),
    (accounts, "/api/accounts"),
    (categories, "/api/categories"),
    (income, "/api/income"),
    (reimbursements, "/api/reimbursements"),
    (subscriptions, "/api/subscriptions"),
    (rules, "/api/rules"),
    (events, "/api/events"),
    (auth_events, "/api/auth/events"),
    (sync, "/api/sync"),
    (imports, "/api/imports"),
    (dashboard, "/api/dashboard"),
    (tax, "/api/tax"),
    (mileage, "/api/mileage"),
    (ai, "/api/ai"),
    (insights, "/api/insights"),
]


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory. Run with `uvicorn --factory app.main:create_app`."""
    settings = settings or get_settings()
    setup_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = make_engine(settings)
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        logger.info("API started ({})", settings.environment)
        yield
        engine.dispose()

    app = FastAPI(
        title="Tally API",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    # Open routes
    app.include_router(health.router)
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])

    # Everything else requires a signed-in user
    for module, prefix in PROTECTED_ROUTERS:
        app.include_router(
            module.router,
            prefix=prefix,
            tags=[prefix.rsplit("/", 1)[-1]],
            dependencies=[Depends(get_current_user)],
        )

    # Throwaway local web preview; `scripts.dev_token` prints the link. Never mounted in
    # production or tests.
    if settings.environment == "development":
        app.mount(
            "/dev",
            StaticFiles(directory=Path(__file__).parent / "dev_preview", html=True),
            name="dev_preview",
        )

    return app
