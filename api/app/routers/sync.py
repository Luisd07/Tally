from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.config import APP_TIMEZONE
from app.deps import DB, AppSettings, CurrentUser, SimplefinTransport
from app.errors import BadGateway, Conflict, UnprocessableEntity
from app.models.enums import RunTrigger
from app.schemas.sync import (
    SimplefinClaimRequest,
    SimplefinClaimResponse,
    SyncRunListQuery,
    SyncRunOut,
    SyncRunPage,
)
from app.services import simplefin
from app.services import sync as sync_service
from app.services.sync import MAX_BACKFILL_DAYS

router = APIRouter()


@router.post("/simplefin/claim", status_code=status.HTTP_201_CREATED)
def claim_simplefin(
    body: SimplefinClaimRequest,
    db: DB,
    user: CurrentUser,
    settings: AppSettings,
    transport: SimplefinTransport,
) -> SimplefinClaimResponse:
    now = datetime.now(UTC)
    try:
        connection, host = simplefin.claim_connection(
            db, settings, user.id, body.setup_token, now, transport=transport
        )
    except simplefin.InvalidSetupToken as exc:
        raise UnprocessableEntity(str(exc)) from exc
    except simplefin.ClaimRejected as exc:
        raise Conflict(
            "This setup token was already used. If that wasn't you, it may be "
            "compromised: disable it in SimpleFIN Bridge and create a new one."
        ) from exc
    except simplefin.SimplefinRequestFailed as exc:
        raise BadGateway("Could not reach SimpleFIN.") from exc
    db.commit()
    return SimplefinClaimResponse(claimed_at=connection.claimed_at, host=host)


@router.post("/simplefin/run")
def run_simplefin_sync(
    db: DB,
    user: CurrentUser,
    settings: AppSettings,
    transport: SimplefinTransport,
    since: Annotated[date | None, Query()] = None,
) -> SyncRunOut:
    """Manual "Sync now".

    Synchronous: the fetch is small, so the response carries the finished
    run's summary rather than a 202 the app would have to poll. The optional
    `since` backfills from that day (at most `MAX_BACKFILL_DAYS` back, so at most 10
    SimpleFIN requests; a user's first sync backfills the year on its own). Refuses with 429
    (`Retry-After` in seconds) if the caller's last run started too recently
    (`sync_service.ensure_cooldown_elapsed`, checked under the per-user lock).
    The push, if any, is sent after `commit` -- see `sync_service.notify`.
    """
    now = datetime.now(UTC)
    if since is not None:
        today = now.astimezone(APP_TIMEZONE).date()
        if since > today:
            raise UnprocessableEntity("since can't be in the future.")
        if since < today - timedelta(days=MAX_BACKFILL_DAYS):
            raise UnprocessableEntity(f"since can't be more than {MAX_BACKFILL_DAYS} days ago.")
    run, push = sync_service.run_sync(
        db, user.id, now, settings, trigger=RunTrigger.manual, since=since, sf_transport=transport
    )
    db.commit()
    sync_service.notify(db, user.id, now, settings, push)
    return SyncRunOut.model_validate(run)


@router.get("/runs")
def list_sync_runs(
    query: Annotated[SyncRunListQuery, Query()], db: DB, user: CurrentUser
) -> SyncRunPage:
    """Sync history and errors, most recent first."""
    return sync_service.list_sync_runs(db, user.id, query)
