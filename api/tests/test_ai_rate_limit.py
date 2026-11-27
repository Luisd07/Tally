"""Per-user rate limits on the routes that call Claude (SEC-2).

The SDK client is replaced, so no test reaches Anthropic.
"""

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.auth import tokens
from app.auth.ratelimit import AI_LIMITS, TAX_REVIEW_LIMITS
from app.config import Settings
from app.db import get_db
from app.main import create_app
from app.models import User
from app.services import ai

DRAFT = {
    "direction": "expense",
    "amount": "18.4",
    "merchant": "Chipotle",
    "description": "Lunch",
    "date": "",
    "category_id": "",
    "bucket": "personal",
}


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=500,
                output_tokens=80,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            content=[SimpleNamespace(type="text", text=json.dumps(DRAFT))],
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMessages:
    messages = FakeMessages()
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    monkeypatch.setattr(ai, "make_client", lambda settings: client)
    return messages


@pytest.fixture
def ai_client(settings: Settings, db_session: Session) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")}))
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as c:
        yield c


def parse(client: TestClient, headers: dict[str, str]) -> Any:
    return client.post("/api/ai/parse-expense", json={"text": "lunch 18.40"}, headers=headers)


def test_parse_expense_is_limited_per_minute(
    ai_client: TestClient, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    per_minute = AI_LIMITS[0].amount
    for _ in range(per_minute):
        assert parse(ai_client, auth_headers).status_code == 200
    assert len(fake.calls) == per_minute

    r = parse(ai_client, auth_headers)

    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1
    assert len(fake.calls) == per_minute


def test_ai_bucket_is_per_user(
    ai_client: TestClient,
    auth_headers: dict[str, str],
    other_user: User,
    settings: Settings,
    fake: FakeMessages,
) -> None:
    for _ in range(AI_LIMITS[0].amount):
        parse(ai_client, auth_headers)
    assert parse(ai_client, auth_headers).status_code == 429

    other = {"Authorization": f"Bearer {tokens.create_access_token(other_user.id, settings)}"}
    assert parse(ai_client, other).status_code == 200


def test_ai_routes_share_one_bucket(
    ai_client: TestClient, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    for _ in range(AI_LIMITS[0].amount):
        parse(ai_client, auth_headers)

    r = ai_client.post("/api/ai/chat", json={"message": "Dining?"}, headers=auth_headers)

    assert r.status_code == 429


def test_tax_review_is_limited_per_hour(
    ai_client: TestClient, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    per_hour = TAX_REVIEW_LIMITS[0].amount
    for year in range(2020, 2020 + per_hour):
        r = ai_client.post("/api/ai/tax-review", json={"year": year}, headers=auth_headers)
        assert r.status_code == 202

    r = ai_client.post("/api/ai/tax-review", json={"year": 2030}, headers=auth_headers)

    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in r.headers


def test_unauthenticated_call_gets_401_not_429(ai_client: TestClient) -> None:
    for _ in range(AI_LIMITS[0].amount + 1):
        r = ai_client.post("/api/ai/parse-expense", json={"text": "lunch"})
        assert r.status_code == 401
