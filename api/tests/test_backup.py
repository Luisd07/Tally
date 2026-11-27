"""Unit tests for #38: the nightly backup job (`app/services/backup.py`).

A fake `runner` stands in for `subprocess.run` everywhere except
`test_real_pg_dump_of_the_test_database` and
`test_real_restic_forget_groups_fixed_host_and_tag_into_one_group`, which exercise
the actual binaries.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from pydantic import SecretStr
from sqlalchemy import Engine

from app.config import Settings
from app.services import backup
from app.services.backup import BackupError, run_backup

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
SNAPSHOT_ID = "a1b2c3d4"
DB_PASSWORD = "s3cret-db-pw"
RESTIC_PASSWORD = "s3cret-restic-pw"


@pytest.fixture
def backup_settings(tmp_path: Path) -> Settings:
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    return Settings(
        environment="test",
        database_url=SecretStr(f"postgresql+psycopg://tally:{DB_PASSWORD}@db-host:5432/tally"),
        restic_repository="gs:test-bucket:/tally",
        restic_password=SecretStr(RESTIC_PASSWORD),
        receipts_dir=receipts_dir,
        backups_dir=tmp_path / "backups",
    )


def _step_name(argv: list[str]) -> str:
    if argv[0] == "pg_dump":
        return "pg_dump"
    return " ".join(argv[:2])


class _FakeRunner:
    """Scripted stand-in for `subprocess.run`: records every call and answers each
    step with a canned `CompletedProcess` (or a timeout), keyed off the step name.
    """

    def __init__(
        self,
        *,
        pg_dump_rc: int = 0,
        restic_backup_rc: int = 0,
        restic_forget_rc: int = 0,
        snapshot_id: str | None = SNAPSHOT_ID,
        stderr: dict[str, str] | None = None,
        timeout_step: str | None = None,
    ) -> None:
        self.rc = {
            "pg_dump": pg_dump_rc,
            "restic backup": restic_backup_rc,
            "restic forget": restic_forget_rc,
        }
        self.snapshot_id = snapshot_id
        self.stderr = stderr or {}
        self.timeout_step = timeout_step
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output
        assert text
        assert timeout
        self.calls.append((list(argv), dict(env)))
        step = _step_name(argv)
        if step == self.timeout_step:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        rc = self.rc[step]
        default_err = "" if rc == 0 else "boom"
        stderr = self.stderr.get(step, default_err)
        if step == "restic backup" and rc == 0:
            stdout = ""
            if self.snapshot_id is not None:
                summary = {"message_type": "summary", "snapshot_id": self.snapshot_id}
                stdout = json.dumps(summary) + "\n"
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=stderr)
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=stderr)


def _dump_path_from(argv: list[str]) -> Path:
    return Path(argv[argv.index("--file") + 1])


def _calls_by_step(runner: _FakeRunner) -> dict[str, tuple[list[str], dict[str, str]]]:
    return {_step_name(argv): (argv, env) for argv, env in runner.calls}


# --- argv shape -----------------------------------------------------------------------


def test_argv_sequence_and_retention_flags(backup_settings: Settings) -> None:
    runner = _FakeRunner()
    run_backup(backup_settings, NOW, runner=runner)

    assert len(runner.calls) == 3
    pg_dump_argv, _ = runner.calls[0]
    restic_backup_argv, _ = runner.calls[1]
    restic_forget_argv, _ = runner.calls[2]

    assert pg_dump_argv[0] == "pg_dump"
    assert "--format=custom" in pg_dump_argv
    dump_path = _dump_path_from(pg_dump_argv)
    assert dump_path == backup_settings.backups_dir / "tally.dump"

    assert restic_backup_argv == [
        "restic",
        "backup",
        "--host",
        "tally",
        "--tag",
        "tally",
        "--json",
        str(dump_path),
        str(backup_settings.receipts_dir),
    ]
    assert restic_forget_argv == [
        "restic",
        "forget",
        "--tag",
        "tally",
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


def test_dump_path_is_fixed_across_runs(backup_settings: Settings) -> None:
    """MV-2: a stable path (not a random `mkstemp` name) is what lets restic's
    `--group-by host,tags` put every night's snapshot in the same group.
    """
    first_runner = _FakeRunner()
    run_backup(backup_settings, NOW, runner=first_runner)
    first_dump_path = _dump_path_from(first_runner.calls[0][0])

    later_runner = _FakeRunner(snapshot_id="second-snap")
    run_backup(backup_settings, datetime(2026, 10, 17, 3, 0, tzinfo=UTC), runner=later_runner)
    later_dump_path = _dump_path_from(later_runner.calls[0][0])

    assert first_dump_path == later_dump_path == backup_settings.backups_dir / "tally.dump"


# --- secrets & minimal env -------------------------------------------------------------


def test_secrets_in_env_never_in_argv_or_logs(backup_settings: Settings) -> None:
    runner = _FakeRunner()
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        run_backup(backup_settings, NOW, runner=runner)
    finally:
        logger.remove(sink_id)

    calls = _calls_by_step(runner)
    pg_dump_argv, pg_dump_env = calls["pg_dump"]
    restic_backup_argv, restic_backup_env = calls["restic backup"]
    restic_forget_argv, restic_forget_env = calls["restic forget"]

    assert pg_dump_env["PGPASSWORD"] == DB_PASSWORD
    assert restic_backup_env["RESTIC_PASSWORD"] == RESTIC_PASSWORD
    assert restic_forget_env["RESTIC_PASSWORD"] == RESTIC_PASSWORD

    all_argv = [*pg_dump_argv, *restic_backup_argv, *restic_forget_argv]
    assert DB_PASSWORD not in all_argv
    assert RESTIC_PASSWORD not in all_argv
    assert not any(DB_PASSWORD in m or RESTIC_PASSWORD in m for m in messages)


def test_subprocess_env_is_minimal_not_the_full_process_environment(
    monkeypatch: pytest.MonkeyPatch, backup_settings: Settings
) -> None:
    """MV-7/UB-4: pg_dump and restic get `PATH`/`HOME` plus only the variables they
    need -- not this process's full environment, which also carries `DATABASE_URL`,
    the JWT signing key and the SimpleFIN key.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-not-be-forwarded")
    monkeypatch.setenv("JWT_SIGNING_KEY", "also-should-not-be-forwarded")

    runner = _FakeRunner()
    run_backup(backup_settings, NOW, runner=runner)

    for _argv, env in runner.calls:
        assert "DATABASE_URL" not in env
        assert "JWT_SIGNING_KEY" not in env
        assert env["PATH"] == os.environ.get("PATH", "")


def test_restic_env_sets_a_cache_dir(backup_settings: Settings) -> None:
    """MV-9: the `app` user has no home directory, so without an explicit cache dir
    restic warns and runs uncached on every call.
    """
    runner = _FakeRunner()
    run_backup(backup_settings, NOW, runner=runner)
    for argv, env in runner.calls:
        if argv[0] == "restic":
            assert env["RESTIC_CACHE_DIR"] == backup.RESTIC_CACHE_DIR


# --- stderr on failure (redacted) ------------------------------------------------------


def test_failure_logs_a_redacted_stderr_tail(backup_settings: Settings) -> None:
    """MV-5/UB-2: a failure must log enough of stderr to diagnose it, but never the
    restic or database password -- even if the (untrusted) tool output echoed one.
    """
    stderr = f"restic: fatal: repository password is {RESTIC_PASSWORD}, auth failed\nboom"
    runner = _FakeRunner(restic_backup_rc=1, stderr={"restic backup": stderr})

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        with pytest.raises(BackupError):
            run_backup(backup_settings, NOW, runner=runner)
    finally:
        logger.remove(sink_id)

    assert any("auth failed" in m and "boom" in m for m in messages)
    assert not any(RESTIC_PASSWORD in m for m in messages)


# --- timeouts ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timeout_step", ["pg_dump", "restic backup"], ids=lambda s: s.replace(" ", "-")
)
def test_a_hung_dump_or_backup_step_times_out_and_raises_backup_error(
    backup_settings: Settings, timeout_step: str
) -> None:
    """MV-4: a hung pg_dump/restic backup must not block every later nightly run."""
    runner = _FakeRunner(timeout_step=timeout_step)
    with pytest.raises(BackupError, match="timed out"):
        run_backup(backup_settings, NOW, runner=runner)
    assert not (backup_settings.backups_dir / "tally.dump").exists()


def test_a_hung_forget_step_times_out_degraded_not_raised(backup_settings: Settings) -> None:
    """A hung `restic forget` is a prune failure like any other (MV-3): the snapshot
    that already landed stays recorded, and the run comes back `degraded` instead
    of raising.
    """
    runner = _FakeRunner(timeout_step="restic forget")
    result = run_backup(backup_settings, NOW, runner=runner)
    assert result.degraded is True
    assert result.snapshot_id == SNAPSHOT_ID


# --- temp dump cleanup -----------------------------------------------------------------


def test_dump_file_deleted_on_success(backup_settings: Settings) -> None:
    runner = _FakeRunner()
    run_backup(backup_settings, NOW, runner=runner)
    dump_path = _dump_path_from(runner.calls[0][0])
    assert not dump_path.exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pg_dump_rc": 1},
        {"restic_backup_rc": 1},
        {"snapshot_id": None},
    ],
    ids=["pg_dump-fails", "restic-backup-fails", "no-snapshot-id"],
)
def test_dump_deleted_and_last_success_untouched_when_the_backup_itself_fails(
    backup_settings: Settings, kwargs: dict[str, Any]
) -> None:
    runner = _FakeRunner(**kwargs)
    with pytest.raises(BackupError):
        run_backup(backup_settings, NOW, runner=runner)

    dump_path = _dump_path_from(runner.calls[0][0])
    assert not dump_path.exists()
    assert not (backup_settings.backups_dir / backup.LAST_SUCCESS_FILE).exists()


# --- last_success.json / degraded prune (MV-3) ------------------------------------------


def test_last_success_written_only_when_the_backup_itself_succeeds(
    backup_settings: Settings,
) -> None:
    result = run_backup(backup_settings, NOW, runner=_FakeRunner())
    assert result.snapshot_id == SNAPSHOT_ID
    assert result.finished_at == NOW
    assert result.degraded is False

    path = backup_settings.backups_dir / backup.LAST_SUCCESS_FILE
    assert json.loads(path.read_text()) == {
        "finished_at": NOW.isoformat(),
        "snapshot_id": SNAPSHOT_ID,
    }


def test_a_failed_prune_keeps_the_new_snapshot_recorded_and_marks_the_run_degraded(
    backup_settings: Settings,
) -> None:
    runner = _FakeRunner(restic_forget_rc=1)
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        result = run_backup(backup_settings, NOW, runner=runner)
    finally:
        logger.remove(sink_id)

    assert result.snapshot_id == SNAPSHOT_ID
    assert result.degraded is True
    assert any("pruning failed" in m.lower() for m in messages)

    path = backup_settings.backups_dir / backup.LAST_SUCCESS_FILE
    assert json.loads(path.read_text()) == {
        "finished_at": NOW.isoformat(),
        "snapshot_id": SNAPSHOT_ID,
    }


def test_last_success_write_is_atomic_with_no_leftover_tmp_file(backup_settings: Settings) -> None:
    run_backup(backup_settings, NOW, runner=_FakeRunner())
    assert list(backup_settings.backups_dir.glob("*.tmp")) == []

    later = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)
    run_backup(backup_settings, later, runner=_FakeRunner(snapshot_id="second-snap"))

    path = backup_settings.backups_dir / backup.LAST_SUCCESS_FILE
    assert json.loads(path.read_text()) == {
        "finished_at": later.isoformat(),
        "snapshot_id": "second-snap",
    }
    assert list(backup_settings.backups_dir.glob("*.tmp")) == []


# --- read_backup_status -----------------------------------------------------------------


def test_read_backup_status_missing_file(backup_settings: Settings) -> None:
    status = backup.read_backup_status(backup_settings)
    assert status.last_success_at is None
    assert status.snapshot_id is None


def test_read_backup_status_valid_file(backup_settings: Settings) -> None:
    backup_settings.backups_dir.mkdir(parents=True, exist_ok=True)
    path = backup_settings.backups_dir / backup.LAST_SUCCESS_FILE
    path.write_text(json.dumps({"finished_at": NOW.isoformat(), "snapshot_id": SNAPSHOT_ID}))

    status = backup.read_backup_status(backup_settings)
    assert status.last_success_at == NOW
    assert status.snapshot_id == SNAPSHOT_ID


@pytest.mark.parametrize(
    "contents",
    [
        "not json",
        "{}",
        json.dumps({"finished_at": "not-a-date", "snapshot_id": "x"}),
        json.dumps([1, 2]),
    ],
    ids=["not-json", "missing-keys", "bad-date", "wrong-shape"],
)
def test_read_backup_status_corrupt_file(backup_settings: Settings, contents: str) -> None:
    backup_settings.backups_dir.mkdir(parents=True, exist_ok=True)
    path = backup_settings.backups_dir / backup.LAST_SUCCESS_FILE
    path.write_text(contents)

    status = backup.read_backup_status(backup_settings)
    assert status.last_success_at is None
    assert status.snapshot_id is None


# --- optional: the real binaries --------------------------------------------------------


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not on PATH")
def test_real_pg_dump_of_the_test_database(
    settings: Settings, engine: Engine, tmp_path: Path
) -> None:
    """Runs `pg_dump` for real against the test database (`engine` makes sure it
    exists and is migrated). Everywhere else in this file uses a fake runner.
    """
    dump_path = tmp_path / "real.dump"
    backup._dump_database(settings, subprocess.run, dump_path)
    assert dump_path.read_bytes()[:5] == b"PGDMP"


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic not on PATH")
def test_real_restic_forget_groups_fixed_host_and_tag_into_one_group(tmp_path: Path) -> None:
    """MV-2 repro check, against a throwaway local repo (never the real bucket): with
    a fixed `--host`/`--tag`, `restic forget --group-by host,tags` must see every
    snapshot as a single group, or retention never prunes anything.
    """
    repo = tmp_path / "repo"
    env = {**os.environ, "RESTIC_REPOSITORY": str(repo), "RESTIC_PASSWORD": "throwaway-password"}
    subprocess.run(["restic", "init"], env=env, check=True, capture_output=True, text=True)

    target = tmp_path / "data.txt"
    for i in range(3):
        target.write_text(f"backup {i}")
        subprocess.run(
            ["restic", "backup", "--host", "tally", "--tag", "tally", "--json", str(target)],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    result = subprocess.run(
        [
            "restic",
            "forget",
            "--tag",
            "tally",
            "--group-by",
            "host,tags",
            "--keep-daily",
            "7",
            "--keep-weekly",
            "4",
            "--keep-monthly",
            "12",
            "--dry-run",
            "--json",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    groups = json.loads(result.stdout)
    assert len(groups) == 1, "all 3 snapshots should fall into a single host+tags group"
    accounted_for = len(groups[0]["keep"]) + len(groups[0].get("remove", []))
    assert accounted_for == 3
