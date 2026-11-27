"""The /dev web preview is mounted in development only (issue #131)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PREVIEW_DIR = Path(__file__).resolve().parents[1] / "app" / "dev_preview"


def _client(settings: Settings, environment: str) -> TestClient:
    return TestClient(create_app(settings.model_copy(update={"environment": environment})))


def test_preview_is_served_in_development(settings: Settings) -> None:
    client = _client(settings, "development")

    index = client.get("/dev/")
    script = client.get("/dev/app.js")

    assert index.status_code == 200
    assert "Tally" in index.text
    assert script.status_code == 200


@pytest.mark.parametrize("environment", ["test", "production"])
def test_preview_is_not_mounted_outside_development(settings: Settings, environment: str) -> None:
    client = _client(settings, environment)

    assert client.get("/dev/").status_code == 404
    assert client.get("/dev/app.js").status_code == 404


def test_preview_is_csp_friendly() -> None:
    page = (PREVIEW_DIR / "index.html").read_text(encoding="utf-8")
    script = (PREVIEW_DIR / "app.js").read_text(encoding="utf-8")

    assert "script-src 'self'" in page
    assert "unsafe-inline" not in page
    assert "<script>" not in page
    assert "eval(" not in script
    assert "innerHTML" not in script
    assert "https://" not in page + script
