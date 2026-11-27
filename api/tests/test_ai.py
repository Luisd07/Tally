"""The Claude foundation (#168): key check, monthly cap, usage log and /api/ai/usage.

The SDK client is replaced by `FakeClient`, so no test reaches Anthropic.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.lib.credentials import WorkloadIdentityError
from anthropic.types.beta import (
    BetaFallbackMessageIterationUsage,
    BetaMessageIterationUsage,
)
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.main import create_app
from app.models import AIUsage, User
from app.services import ai

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)


def usage(
    input: int = 1000, output: int = 200, cache_read: int = 0, cache_write: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input,
        output_tokens=output,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response or SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=usage(),
            content=[SimpleNamespace(type="text", text='{"ok": true}')],
        )
        self.error = error

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, messages: FakeMessages) -> None:
        self.beta = SimpleNamespace(messages=messages)


@pytest.fixture
def ai_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"anthropic_api_key": SecretStr("sk-ant-test"), "ai_monthly_budget_usd": Decimal(1)}
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMessages:
    messages = FakeMessages()
    monkeypatch.setattr(ai, "make_client", lambda settings: FakeClient(messages))
    return messages


def spend(db: Session, user: User, cost: str, when: datetime = NOW) -> None:
    db.add(
        AIUsage(
            user_id=user.id,
            feature="test",
            model="claude-sonnet-5-5",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            batch=False,
            cost_usd=Decimal(cost),
            created_at=when,
        )
    )
    db.flush()


def call(db: Session, settings: Settings, user: User) -> Any:
    messages = [{"role": "user", "content": "hi"}]
    return ai.create_message(db, settings, user.id, "test", NOW, max_tokens=256, messages=messages)


# --- cost ---


def test_cost_uses_the_price_table_with_cache_and_batch_rates() -> None:
    tokens = ai.TokenUsage(input=1_000_000, output=100_000, cache_read=500_000, cache_write=200_000)
    # 2 + 1 + 0.10 + 0.50 dollars at Sonnet 5.5's rates.
    assert ai.cost_of("claude-sonnet-5-5", tokens, batch=False) == Decimal("3.600000")
    assert ai.cost_of("claude-sonnet-5-5", tokens, batch=True) == Decimal("1.800000")


def test_unknown_model_is_priced_at_the_dearest_rates() -> None:
    tokens = ai.TokenUsage(input=1_000_000, output=0, cache_read=0, cache_write=0)
    assert ai.cost_of("claude-new-model", tokens, batch=False) == Decimal("4.000000")


# --- calls ---


def test_no_key_means_disabled_and_no_call(
    db_session: Session, settings: Settings, user: User, fake: FakeMessages
) -> None:
    assert not ai.is_enabled(settings)
    with pytest.raises(ai.AIUnavailable) as exc:
        call(db_session, settings, user)
    assert exc.value.reason == "disabled"
    assert fake.calls == []


def test_call_records_one_usage_row_with_its_cost(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    response = call(db_session, ai_settings, user)

    assert ai.text_of(response) == '{"ok": true}'
    [sent] = fake.calls
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["fallbacks"] == "default"
    assert sent["betas"] == [ai.FALLBACK_BETA]
    rows = db_session.scalars(select(AIUsage).where(AIUsage.user_id == user.id)).all()
    assert [(r.feature, r.input_tokens, r.output_tokens, r.batch) for r in rows] == [
        ("test", 1000, 200, False)
    ]
    # 1000 * $2/M + 200 * $10/M
    assert rows[0].cost_usd == Decimal("0.004000")


def test_feature_model_override(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    settings = ai_settings.model_copy(update={"ai_feature_models": {"test": "claude-opus-5-5"}})
    call(db_session, settings, user)
    assert fake.calls[0]["model"] == "claude-opus-5-5"


def test_cap_blocks_calls_once_the_month_reaches_the_budget(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    spend(db_session, user, "0.99")
    call(db_session, ai_settings, user)  # still under $1
    spend(db_session, user, "0.01")

    with pytest.raises(ai.AIUnavailable) as exc:
        call(db_session, ai_settings, user)
    assert exc.value.reason == "budget"
    assert len(fake.calls) == 1


def test_last_months_spend_doesnt_count(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    # 23:30 New York time on 30 September is already October in UTC; it's still September.
    spend(db_session, user, "5", when=datetime(2026, 10, 1, 3, 30, tzinfo=UTC))
    assert ai.month_spend(db_session, user.id, NOW) == Decimal(0)
    call(db_session, ai_settings, user)


def test_other_users_spend_doesnt_count(
    db_session: Session, ai_settings: Settings, user: User, other_user: User, fake: FakeMessages
) -> None:
    spend(db_session, other_user, "5")
    call(db_session, ai_settings, user)
    assert len(fake.calls) == 1


def test_api_error_is_ai_unavailable(
    db_session: Session, ai_settings: Settings, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(request=request)
    messages = FakeMessages(error=error)
    monkeypatch.setattr(ai, "make_client", lambda settings: FakeClient(messages))

    with pytest.raises(ai.AIUnavailable) as exc:
        call(db_session, ai_settings, user)
    assert exc.value.reason == "error"
    assert db_session.scalars(select(AIUsage)).all() == []


def test_failed_token_exchange_is_ai_unavailable(
    db_session: Session, ai_settings: Settings, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = FakeMessages(error=WorkloadIdentityError("Token exchange failed"))
    monkeypatch.setattr(ai, "make_client", lambda settings: FakeClient(messages))

    with pytest.raises(ai.AIUnavailable) as exc:
        call(db_session, ai_settings, user)
    assert exc.value.reason == "error"


def test_refusal_is_recorded_then_ai_unavailable(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    fake.response = SimpleNamespace(
        model="claude-sonnet-5-5", stop_reason="refusal", usage=usage(output=0), content=[]
    )
    with pytest.raises(ai.AIUnavailable) as exc:
        call(db_session, ai_settings, user)
    assert exc.value.reason == "refused"
    assert len(db_session.scalars(select(AIUsage)).all()) == 1


def test_a_fallback_served_call_records_every_attempt(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    served = BetaFallbackMessageIterationUsage(
        type="fallback_message",
        model="claude-sonnet-5",
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    declined = BetaMessageIterationUsage(
        type="message",
        model="claude-sonnet-5-5",
        input_tokens=1000,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    top = usage(input=1000, output=200)
    top.iterations = [declined, served]
    fake.response = SimpleNamespace(
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=top,
        content=[SimpleNamespace(type="text", text="{}")],
    )

    call(db_session, ai_settings, user)

    rows = db_session.scalars(select(AIUsage).order_by(AIUsage.output_tokens)).all()
    assert [(r.model, r.output_tokens, r.cost_usd) for r in rows] == [
        # 1000 * $2/M + 50 * $10/M, then 1000 * $2/M + 200 * $10/M
        ("claude-sonnet-5-5", 50, Decimal("0.002500")),
        ("claude-sonnet-5", 200, Decimal("0.004000")),
    ]
    assert ai.month_spend(db_session, user.id, NOW) == Decimal("0.006500")


def test_an_attempt_without_a_model_is_priced_as_the_response(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    top = usage()
    top.iterations = [
        BetaMessageIterationUsage(
            type="message",
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
    ]
    fake.response.usage = top

    call(db_session, ai_settings, user)

    [row] = db_session.scalars(select(AIUsage)).all()
    assert (row.model, row.cost_usd) == ("claude-sonnet-5-5", Decimal("0.004000"))


def test_record_usage_for_batch_results_is_half_price(db_session: Session, user: User) -> None:
    row = ai.record_usage(
        db_session,
        user.id,
        "inbox_suggestions",
        "claude-sonnet-5-5",
        ai.TokenUsage(input=1_000_000, output=0, cache_read=0, cache_write=0),
        batch=True,
        now=NOW,
    )
    assert row.batch
    assert row.cost_usd == Decimal("1.000000")


# --- /api/ai/usage ---


@pytest.fixture
def ai_client(ai_settings: Settings, db_session: Session) -> Iterator[TestClient]:
    app = create_app(ai_settings)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as c:
        yield c


def test_usage_endpoint_reports_the_users_month(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    now = datetime.now(UTC)
    spend(db_session, user, "0.123456", when=now)
    spend(db_session, other_user, "9", when=now)

    body = ai_client.get("/api/ai/usage", headers=auth_headers).json()
    assert body["spent_usd"] == "0.12"
    assert body["budget_usd"] == "1.00"
    assert body["enabled"] is True
    assert len(body["month"]) == 7


def test_usage_endpoint_says_disabled_without_a_key(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = db_client.get("/api/ai/usage", headers=auth_headers).json()
    assert body["enabled"] is False
    assert body["spent_usd"] == "0.00"


def test_usage_endpoint_needs_a_token(client: TestClient) -> None:
    assert client.get("/api/ai/usage").status_code == 401
