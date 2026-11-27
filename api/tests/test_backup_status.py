"""Acceptance tests for #38: `GET /api/backup/status`, read from
`backups_dir/last_success.json`. 401-without-a-token coverage comes from
`test_auth_required.py`, since this route is mounted through `PROTECTED_ROUTERS`
like every other data route.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.main import create_app

STATUS = "/api/backup/status"


@pytest.fixture
def backup_app(settings: Settings, tmp_path: Path) -> FastAPI:
    return create_app(settings.model_copy(update={"backups_dir": tmp_path}))


@pytest.fixture
def backup_client(backup_app: FastAPI, db_session: Session) -> Iterator[TestClient]:
    backup_app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(backup_app) as c:
        yield c
    backup_app.dependency_overrides.clear()


def test_status_is_null_when_no_backup_has_run_yet(
    backup_client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = backup_client.get(STATUS, headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"last_success_at": None, "snapshot_id": None}


def test_status_reports_the_last_successful_backup(
    backup_app: FastAPI, backup_client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    finished_at = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    (tmp_path / "last_success.json").write_text(
        json.dumps({"finished_at": finished_at.isoformat(), "snapshot_id": "a1b2c3d4"})
    )

    r = backup_client.get(STATUS, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert datetime.fromisoformat(body["last_success_at"]) == finished_at
    assert body["snapshot_id"] == "a1b2c3d4"


@pytest.mark.parametrize(
    "contents",
    [
        "not json at all",
        "{}",
        json.dumps({"finished_at": "not-a-real-date", "snapshot_id": "x"}),
        json.dumps([1, 2, 3]),
    ],
    ids=["not-json", "missing-keys", "bad-date", "wrong-shape"],
)
def test_status_is_null_when_the_file_is_corrupt(
    backup_client: TestClient, auth_headers: dict[str, str], tmp_path: Path, contents: str
) -> None:
    (tmp_path / "last_success.json").write_text(contents)

    r = backup_client.get(STATUS, headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"last_success_at": None, "snapshot_id": None}
