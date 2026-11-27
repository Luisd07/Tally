"""Nightly backup: `pg_dump` the database, push it plus the receipts folder to Cloud
Storage with restic, then prune old snapshots. See the spec's "Backups" section.

The restic password and the database password reach the subprocesses only through
their environment (`RESTIC_PASSWORD`, `PGPASSWORD`), never on argv and never logged
(stderr is redacted before it's logged, too). GCS credentials come from the VM's
service account via Application Default Credentials; `settings.gcs_key_file` only
sets `GOOGLE_APPLICATION_CREDENTIALS` where ADC isn't available (ADC needs nothing
passed at all). Each subprocess gets a minimal environment -- just `PATH`/`HOME`
plus the variables that tool needs -- rather than this process's full environment,
which would otherwise also carry `DATABASE_URL`, the JWT key and the SimpleFIN key.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from loguru import logger
from sqlalchemy import make_url

from app.config import Settings
from app.schemas.backup import BackupStatus

Runner = Callable[..., CompletedProcess[str]]

LAST_SUCCESS_FILE = "last_success.json"
DUMP_FILE = "tally.dump"
RESTIC_TAG = "tally"
RESTIC_HOST = "tally"
RESTIC_CACHE_DIR = "/tmp/restic-cache"

# Generous ceilings for an e2-micro VM over a possibly slow uplink: a hung step (a
# stale restic lock, a network stall to GCS) must not block every later nightly run.
PG_DUMP_TIMEOUT = 30 * 60
RESTIC_BACKUP_TIMEOUT = 2 * 60 * 60
RESTIC_FORGET_TIMEOUT = 60 * 60

STDERR_LOG_CHARS = 2000


class BackupError(Exception):
    """A backup step failed. The message is safe to log: it never carries secrets."""


@dataclass
class BackupResult:
    """`degraded` is `True` when the snapshot was saved but `restic forget --prune`
    failed: the backup counts as successful (`last_success.json` is written), but
    retention didn't run this time.
    """

    snapshot_id: str
    finished_at: datetime
    degraded: bool = False


def _minimal_env(**extra: str) -> dict[str, str]:
    """`PATH` (and `HOME`, if this process has one) plus whatever the caller adds --
    not this process's full environment, which carries every app secret.
    """
    env = {"PATH": os.environ.get("PATH", "")}
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    env.update(extra)
    return env


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _run(
    runner: Runner,
    step: str,
    argv: list[str],
    env: dict[str, str],
    timeout: float,
    secrets: tuple[str, ...],
) -> CompletedProcess[str]:
    """Run one backup step. Always raises `BackupError` (never a bare `subprocess`
    exception) on a non-zero exit or a timeout, after logging a bounded,
    secret-redacted tail of stderr -- the only diagnostic a 3 a.m. failure leaves.
    """
    try:
        result = runner(argv, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logger.error("nightly backup: {} timed out after {}s", step, timeout)
        raise BackupError(f"{step} timed out") from exc
    if result.returncode != 0:
        tail = _redact(result.stderr, secrets)[-STDERR_LOG_CHARS:]
        logger.error("nightly backup: {} failed (exit {}): {}", step, result.returncode, tail)
        raise BackupError(f"{step} failed")
    return result


def _restic_env(settings: Settings) -> dict[str, str]:
    env = _minimal_env(
        RESTIC_REPOSITORY=settings.restic_repository,
        RESTIC_PASSWORD=settings.restic_password.get_secret_value(),
        RESTIC_CACHE_DIR=RESTIC_CACHE_DIR,
    )
    if settings.gcs_key_file:
        env["GOOGLE_APPLICATION_CREDENTIALS"] = settings.gcs_key_file
    return env


def _dump_path(backups_dir: Path) -> Path:
    """A fixed path, not a random `mkstemp` name: restic's default `--group-by`
    includes `paths`, and a different path every night would otherwise put every
    snapshot in its own group and defeat `forget`'s retention (same reasoning as
    the fixed `--host`/`--tag` below).
    """
    return backups_dir / DUMP_FILE


def _dump_database(settings: Settings, runner: Runner, dump_path: Path) -> None:
    url = make_url(settings.database_url.get_secret_value())
    argv = [
        "pg_dump",
        "--format=custom",
        "--file",
        str(dump_path),
        "--host",
        url.host or "127.0.0.1",
        "--port",
        str(url.port or 5432),
        "--username",
        url.username or "",
        url.database or "",
    ]
    password = url.password or ""
    env = _minimal_env(PGPASSWORD=password) if password else _minimal_env()
    _run(runner, "pg_dump", argv, env, PG_DUMP_TIMEOUT, secrets=(password,))


def _parse_snapshot_id(stdout: str) -> str | None:
    """The `snapshot_id` from restic's `--json` summary line (the last line of output)."""
    for line in reversed(stdout.splitlines()):
        try:
            payload: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("message_type") == "summary":
            snapshot_id = payload.get("snapshot_id")
            return snapshot_id if isinstance(snapshot_id, str) else None
    return None


def _restic_backup(settings: Settings, runner: Runner, dump_path: Path) -> str:
    argv = [
        "restic",
        "backup",
        "--host",
        RESTIC_HOST,
        "--tag",
        RESTIC_TAG,
        "--json",
        str(dump_path),
        str(settings.receipts_dir),
    ]
    secrets = (settings.restic_password.get_secret_value(),)
    result = _run(
        runner, "restic backup", argv, _restic_env(settings), RESTIC_BACKUP_TIMEOUT, secrets
    )
    snapshot_id = _parse_snapshot_id(result.stdout)
    if snapshot_id is None:
        logger.error("nightly backup: restic backup succeeded but reported no snapshot id")
        raise BackupError("restic backup did not report a snapshot id")
    return snapshot_id


def _restic_forget(settings: Settings, runner: Runner) -> None:
    argv = [
        "restic",
        "forget",
        "--tag",
        RESTIC_TAG,
        "--group-by",
        "host,tags",
        "--keep-daily",
        "7",
        "--keep-weekly",
        "4",
        "--keep-monthly",
        "12",
        "--prune",
    ]
    secrets = (settings.restic_password.get_secret_value(),)
    _run(runner, "restic forget", argv, _restic_env(settings), RESTIC_FORGET_TIMEOUT, secrets)


def _write_last_success(settings: Settings, now: datetime, snapshot_id: str) -> None:
    path = settings.backups_dir / LAST_SUCCESS_FILE
    payload = json.dumps({"finished_at": now.isoformat(), "snapshot_id": snapshot_id})
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def run_backup(
    settings: Settings, now: datetime, *, runner: Runner = subprocess.run
) -> BackupResult:
    """`pg_dump` into a fixed path under `backups_dir`, push it plus `receipts_dir`
    with restic, then prune old snapshots.

    `last_success.json` is written as soon as the restic snapshot exists -- that's
    the actual backup, and a snapshot the status endpoint doesn't know about is
    worse than one it reports as slightly stale. Pruning runs afterwards as its own
    step: a failure there (for example a bucket role with no object-delete
    permission) is logged and reported through `BackupResult.degraded`, but doesn't
    discard the snapshot that was just recorded. The dump file is removed in every
    case once restic has it, successfully or not.

    `runner` is injectable so tests can capture the argv/env without the real
    `pg_dump`/`restic` binaries on PATH.
    """
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    dump_path = _dump_path(settings.backups_dir)
    dump_path.unlink(missing_ok=True)  # clear a stale dump left by a killed previous run
    try:
        _dump_database(settings, runner, dump_path)
        snapshot_id = _restic_backup(settings, runner, dump_path)
    finally:
        dump_path.unlink(missing_ok=True)

    _write_last_success(settings, now, snapshot_id)

    degraded = False
    try:
        _restic_forget(settings, runner)
    except BackupError as exc:
        logger.error("nightly backup: retention pruning failed, old snapshots were kept: {}", exc)
        degraded = True

    return BackupResult(snapshot_id=snapshot_id, finished_at=now, degraded=degraded)


def read_backup_status(settings: Settings) -> BackupStatus:
    """The last successful backup, read from `backups_dir/last_success.json`.

    A missing or corrupt file (not yet written, or truncated by a crash mid-write --
    `_write_last_success` is atomic, so this is defensive) reads as "no backup yet".
    """
    path = settings.backups_dir / LAST_SUCCESS_FILE
    try:
        raw = json.loads(path.read_text())
        return BackupStatus(
            last_success_at=datetime.fromisoformat(raw["finished_at"]),
            snapshot_id=str(raw["snapshot_id"]),
        )
    except OSError, ValueError, KeyError, TypeError:
        return BackupStatus(last_success_at=None, snapshot_id=None)
