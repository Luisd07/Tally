"""Background worker: nightly SimpleFIN sync, matching, push notification, AI Inbox
suggestions, spending insights, backup."""

import random
import uuid
from datetime import UTC, datetime

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import APP_TIMEZONE, Settings, get_settings
from app.db import make_engine
from app.log import setup_logging
from app.models import User
from app.models.enums import RunTrigger
from app.services import (
    ai_chat,
    ai_suggestions,
    insights,
    receipt_reading,
    tax_review,
    weekly_review,
)
from app.services import sync as sync_service
from app.services.backup import run_backup

# A fixed random minute, so the daily pull doesn't land on the hour with everyone else.
SYNC_MINUTE = random.Random("tally-sync").randint(0, 59)


def nightly_sync() -> None:
    """Pull the last 7 days from SimpleFIN, run rules and matchers, find tonight's spending
    insights, then push one notification for both.

    Runs for the single owner user, the same "oldest user" rule
    `app.auth.enrollment.get_or_create_user` uses -- but never creates one: with no
    owner yet (a fresh VM, before the first enrollment), there's nothing to sync.
    """
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            owner = db.scalars(select(User).order_by(User.created_at).limit(1)).first()
            if owner is None:
                logger.info("Nightly sync: no owner user yet; skipping.")
                return
            now = datetime.now(UTC)
            run, push = sync_service.run_sync(
                db, owner.id, now, settings, trigger=RunTrigger.scheduled
            )
            db.commit()
            insights_and_push(db, settings, owner.id, now, push)
            logger.info(
                "Nightly sync finished: status={} new={} updated={} errors={}",
                run.status,
                run.new_count,
                run.updated_count,
                len(run.errors),
            )
            submit_suggestions(db, owner.id, now)
    finally:
        engine.dispose()


def insights_and_push(
    db: Session,
    settings: Settings,
    user_id: uuid.UUID,
    now: datetime,
    push: sync_service.PushIntent | None,
    *,
    apns_client: httpx.Client | None = None,
) -> None:
    """Tonight's spending insights (#225), then the night's one push: the sync's, with the
    top new insight folded in. An insights failure is logged and still sends the sync's
    push, after the sync already committed."""
    try:
        _, push = insights.run_insights(db, settings, user_id, now, push)
    except Exception as exc:
        db.rollback()
        logger.warning("Insights run failed: {}", type(exc).__name__)
    sync_service.notify(db, user_id, now, settings, push, apns_client=apns_client)


def submit_suggestions(db: Session, user_id: uuid.UUID, now: datetime) -> None:
    """Send tonight's Inbox rows to Claude as one batch (#169). A failure here is logged and
    never costs the sync that already committed."""
    try:
        ai_suggestions.submit(db, get_settings(), user_id, now)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Inbox suggestions submit failed: {}", type(exc).__name__)


def collect_suggestions() -> None:
    """Store the answers of any AI batch that has ended (#169)."""
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            ai_suggestions.collect(db, settings, datetime.now(UTC))
    except Exception as exc:
        logger.warning("Inbox suggestions collect failed: {}", type(exc).__name__)
    finally:
        engine.dispose()


def nightly_backup() -> None:
    """pg_dump plus the receipts folder, pushed with restic."""
    try:
        result = run_backup(get_settings(), datetime.now(UTC))
    except Exception as exc:
        # str(exc) only: no secrets are ever in a BackupError message, and `logger.exception`
        # would render local variables (the restic/pg_dump env dicts) into the traceback.
        logger.error("nightly backup failed: {}", exc)
        return
    if result.degraded:
        logger.warning(
            "nightly backup degraded: snapshot {} saved but retention pruning failed",
            result.snapshot_id,
        )


def read_receipts() -> None:
    """Read new receipt images with Claude (#170); each receipt commits on its own."""
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            receipt_reading.read_new_receipts(db, settings, datetime.now(UTC))
    except Exception as exc:
        logger.warning("Receipt reading run failed: {}", type(exc).__name__)
    finally:
        engine.dispose()


def write_weekly_review() -> None:
    """Last week's AI review for Home (#173), for the owner user."""
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            owner = db.scalars(select(User).order_by(User.created_at).limit(1)).first()
            if owner is None:
                return
            weekly_review.run_review(db, settings, owner.id, datetime.now(UTC))
            db.commit()
    except Exception as exc:
        logger.warning("Weekly review failed: {}", type(exc).__name__)
    finally:
        engine.dispose()


def purge_chats() -> None:
    """Delete money chats untouched for 30 days (#174)."""
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            ai_chat.purge_old(db, datetime.now(UTC))
            db.commit()
    except Exception as exc:
        logger.warning("Chat purge failed: {}", type(exc).__name__)
    finally:
        engine.dispose()


def write_tax_reviews() -> None:
    """Write any year-end tax review the Tax screen asked for (#175)."""
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            tax_review.run_requested(db, settings, datetime.now(UTC))
    except Exception as exc:
        logger.warning("Tax review run failed: {}", type(exc).__name__)
    finally:
        engine.dispose()


def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    engine = make_engine(settings)

    scheduler = BlockingScheduler(timezone=APP_TIMEZONE)
    scheduler.add_job(nightly_sync, "cron", hour=5, minute=SYNC_MINUTE, id="simplefin_sync")
    scheduler.add_job(nightly_backup, "cron", hour=3, minute=0, id="backup")
    scheduler.add_job(purge_chats, "cron", hour=4, minute=0, id="chat_purge")
    # After Monday's 05:xx sync, so the week's last charges are in.
    scheduler.add_job(
        write_weekly_review, "cron", day_of_week="mon", hour=7, minute=30, id="weekly_review"
    )
    # Batches usually end within the hour; checking is one cheap query while none are open.
    scheduler.add_job(collect_suggestions, "interval", minutes=10, id="ai_suggestions_collect")
    scheduler.add_job(write_tax_reviews, "interval", minutes=1, id="tax_review")
    # A receipt is read within a couple of minutes of its upload.
    scheduler.add_job(read_receipts, "interval", minutes=2, id="receipt_reading")
    logger.info("Worker started; sync at 05:{:02d}", SYNC_MINUTE)
    try:
        scheduler.start()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
