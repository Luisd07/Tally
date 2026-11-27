"""AI quick capture (#171): `POST /api/ai/parse-expense` returns a draft and saves nothing.

The SDK client is replaced, so no test reaches Anthropic.
"""

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import APP_TIMEZONE, Settings
from app.db import get_db
from app.main import create_app
from app.models import AIUsage, Category, Transaction, User
from app.models.enums import CategoryKind
from app.services import ai
from tests.factories import make_account, make_category


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: dict[str, Any] | str = {}
        self.stop_reason = "end_turn"

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(
                input_tokens=500,
                output_tokens=80,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            content=[SimpleNamespace(type="text", text=text)],
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


@pytest.fixture
def dining(db_session: Session, user: User) -> Category:
    row = make_category(user, name="Dining")
    db_session.add(row)
    db_session.flush()
    return row


def today() -> date:
    from datetime import UTC, datetime

    return datetime.now(UTC).astimezone(APP_TIMEZONE).date()


def parse(client: TestClient, headers: dict[str, str], text: str) -> Any:
    return client.post("/api/ai/parse-expense", json={"text": text}, headers=headers)


def test_note_becomes_a_draft(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    dining: Category,
    auth_headers: dict[str, str],
    fake: FakeMessages,
) -> None:
    fake.answer = {
        "direction": "expense",
        "amount": "18.4",
        "merchant": "Chipotle",
        "description": "Client lunch",
        "date": "",
        "category_id": str(dining.id),
        "bucket": "business",
    }
    response = parse(ai_client, auth_headers, "client lunch Chipotle 18.40 business")

    assert response.status_code == 200
    assert response.json() == {
        "direction": "expense",
        "amount": "18.40",
        "merchant": "Chipotle",
        "description": "Client lunch",
        "date": today().isoformat(),
        "category_id": str(dining.id),
        "bucket": "business",
    }
    # Nothing saved but the usage row.
    assert db_session.scalar(select(func.count()).select_from(Transaction)) == 0
    assert db_session.scalar(select(func.count()).select_from(AIUsage)) == 1


def test_request_carries_the_note_today_and_categories_but_no_accounts(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    dining: Category,
    auth_headers: dict[str, str],
    fake: FakeMessages,
) -> None:
    db_session.add(make_account(user, name="Sapphire Reserve", last4="4242"))
    db_session.flush()
    fake.answer = {
        "direction": "expense",
        "amount": "",
        "merchant": "",
        "description": "",
        "date": "",
        "category_id": "none",
        "bucket": "personal",
    }
    parse(ai_client, auth_headers, "coffee on my sapphire")

    [sent] = fake.calls
    assert sent["messages"][0]["content"].endswith("Note: coffee on my sapphire")
    assert f"Today is {today().isoformat()}" in sent["messages"][0]["content"]
    assert f"{dining.id} | Dining | expense" in sent["system"][0]["text"]
    assert "4242" not in json.dumps(sent) and "Sapphire Reserve" not in json.dumps(sent)
    schema = sent["output_config"]["format"]["schema"]
    assert schema["properties"]["category_id"]["enum"] == [str(dining.id), "none"]


def test_archived_categories_are_not_offered_to_the_model(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    dining: Category,
    auth_headers: dict[str, str],
    fake: FakeMessages,
) -> None:
    old = make_category(user, name="Old", archived=True)
    db_session.add(old)
    db_session.flush()
    fake.answer = {
        "direction": "expense",
        "amount": "",
        "merchant": "",
        "description": "",
        "date": "",
        "category_id": "none",
        "bucket": "personal",
    }
    parse(ai_client, auth_headers, "coffee")

    [sent] = fake.calls
    assert str(old.id) not in json.dumps(sent)
    assert str(dining.id) in sent["system"][0]["text"]


def test_out_of_schema_values_are_dropped(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
    fake: FakeMessages,
) -> None:
    theirs = make_category(other_user, name="Theirs", kind=CategoryKind.expense)
    db_session.add(theirs)
    db_session.flush()
    fake.answer = {
        "direction": "sideways",
        "amount": "lots",
        "merchant": "  ",
        "description": "x" * 900,
        "date": "1999-01-01",
        "category_id": str(theirs.id),
        "bucket": "nope",
    }
    body = parse(ai_client, auth_headers, "something").json()

    assert body["direction"] == "expense"
    assert body["amount"] is None
    assert body["merchant"] is None
    assert len(body["description"]) == 500
    assert body["date"] == today().isoformat()
    assert body["category_id"] is None
    assert body["bucket"] is None


def test_negative_amounts_come_back_positive(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    fake.answer = {
        "direction": "income",
        "amount": "-250",
        "merchant": "",
        "description": "Race pay",
        "date": "",
        "category_id": "none",
        "bucket": "income",
    }
    body = parse(ai_client, auth_headers, "got paid 250 for the race").json()
    assert (body["direction"], body["amount"], body["bucket"]) == ("income", "250.00", "income")


def test_unreadable_answer_is_503(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    fake.answer = "not json"
    response = parse(ai_client, auth_headers, "lunch")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_refusal_is_503_and_keeps_the_usage_row(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    fake: FakeMessages,
) -> None:
    fake.stop_reason = "refusal"
    assert parse(ai_client, auth_headers, "lunch").status_code == 503
    assert db_session.scalar(select(func.count()).select_from(AIUsage)) == 1


def test_a_crash_after_the_call_keeps_its_usage_row(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    fake: FakeMessages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import ai_capture

    def boom(**fields: Any) -> None:
        raise RuntimeError("draft failed")

    monkeypatch.setattr(ai_capture, "ExpenseDraft", boom)
    assert parse(ai_client, auth_headers, "lunch 12").status_code == 500
    # The request's session is never committed after an error.
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(AIUsage)) == 1


def test_without_ai_it_is_503_and_no_call(
    db_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    response = parse(db_client, auth_headers, "lunch 12")
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "AI assist is off."
    assert fake.calls == []


def test_over_budget_it_is_503(
    settings: Settings,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    fake: FakeMessages,
) -> None:
    capped = settings.model_copy(
        update={"anthropic_api_key": SecretStr("sk-ant-test"), "ai_monthly_budget_usd": Decimal(0)}
    )
    app = create_app(capped)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        response = parse(client, auth_headers, "lunch 12")
    assert response.status_code == 503
    assert "budget" in response.json()["error"]["message"]
    assert fake.calls == []


@pytest.mark.parametrize("text", ["", "x" * 501])
def test_note_length_is_checked(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages, text: str
) -> None:
    assert parse(ai_client, auth_headers, text).status_code == 422
    assert fake.calls == []
