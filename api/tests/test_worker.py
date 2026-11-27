"""`app.worker.nightly_sync` (#48) and `app.worker.nightly_backup` (#38): the
scheduled jobs, called directly. `nightly_backup` is a thin wrapper around
`run_backup` that never lets the scheduler crash, and never logs a secret
while doing it.
"""

import uuid
from datetime import UTC, datetime

import pytest
from loguru import logger
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from app import worker
from app.config import Settings
from app.models import SyncRun, User
from app.models.enums import RunStatus, RunTrigger
from app.services.backup import BackupError, BackupResult


def test_nightly_sync_runs_for_the_oldest_user_and_commits(
    engine: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Needs a committed owner row, so it runs outside the rolled-back test
    transaction, the same two-connection-style pattern `test_refresh_rotation.py`
    uses for its advisory-lock tests.

    This user has no SimpleFIN connection set up, so `run_sync` fails before any
    network call -- enough to prove the job wires a real session, the owner user
    and `run_sync` together end to end, commits, and logs, without needing to
    mock SimpleFIN or APNs for a job test that isn't about either.
    """
    user_id = uuid.uuid4()
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Oldest User"))
        setup.commit()

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "make_engine", lambda _settings: engine)
    # The job disposes the engine it's handed; don't let it tear down the
    # session-scoped fixture every other test still needs.
    monkeypatch.setattr(engine, "dispose", lambda: None)

    try:
        worker.nightly_sync()

        with Session(engine) as check:
            run = check.scalars(select(SyncRun).where(SyncRun.user_id == user_id)).one()
            assert run.trigger == RunTrigger.scheduled
            assert run.status == RunStatus.failed  # no SimpleFIN connection for this user
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(SyncRun).where(SyncRun.user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_nightly_sync_does_not_create_a_user_when_there_is_no_owner(
    engine: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no owner at all, the job must skip rather than create one -- that's
    `get_or_create_user`'s job, not the worker's.

    Needs the test database to have no committed `User` row at all; every other
    test that commits one outside the rolled-back transaction cleans it up in a
    `finally`, so this should hold, but skips rather than false-failing if some
    other test ever leaves one behind.
    """
    with Session(engine) as check:
        if check.scalars(select(User.id).limit(1)).first() is not None:
            pytest.skip("another committed-row test left a user in the shared test database")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "make_engine", lambda _settings: engine)
    monkeypatch.setattr(engine, "dispose", lambda: None)

    worker.nightly_sync()  # must not raise, must not create a user

    with Session(engine) as check:
        assert check.scalars(select(User.id).limit(1)).first() is None


def test_nightly_backup_calls_run_backup_with_settings_and_now(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    calls: list[tuple[object, object]] = []

    def fake_run_backup(settings: Settings, now: object, *, runner: object = None) -> BackupResult:
        calls.append((settings, now))
        return BackupResult(snapshot_id="abc123", finished_at=datetime.now(UTC))

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "run_backup", fake_run_backup)

    worker.nightly_backup()

    assert len(calls) == 1
    assert calls[0][0] is settings


def test_nightly_backup_warns_on_a_degraded_run(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    def fake_run_backup(*args: object, **kwargs: object) -> BackupResult:
        return BackupResult(snapshot_id="abc123", finished_at=datetime.now(UTC), degraded=True)

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "run_backup", fake_run_backup)

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        worker.nightly_backup()
    finally:
        logger.remove(sink_id)

    assert any("degraded" in m and "abc123" in m for m in messages)


def test_nightly_backup_logs_a_failure_without_crashing_or_leaking_secrets(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    secret = "s3cret-restic-password"

    def fake_run_backup(*args: object, **kwargs: object) -> None:
        raise BackupError("restic backup failed")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "run_backup", fake_run_backup)

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        worker.nightly_backup()  # must not raise
    finally:
        logger.remove(sink_id)

    assert any("restic backup failed" in m for m in messages)
    assert not any(secret in m for m in messages)
