"""AI Inbox suggestions (#169): batch submit and collect, and the Inbox's AI fallback.

The SDK client is replaced by `FakeClient`, so no test reaches Anthropic.
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, AIBatch, AISuggestion, AIUsage, Category, Transaction, User
from app.models.enums import AIBatchStatus, AIConfidence, AISuggestionStatus, Bucket
from app.services import ai, ai_suggestions
from tests.factories import make_account, make_category, make_transaction

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[Any]] = []
        self.status = "in_progress"
        self.results_by_id: dict[str, list[Any]] = {}
        # Raised by `retrieve` for that batch id.
        self.errors: dict[str, Exception] = {}

    def create(self, *, requests: list[Any]) -> Any:
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}")

    def retrieve(self, batch_id: str) -> Any:
        if batch_id in self.errors:
            raise self.errors[batch_id]
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    def results(self, batch_id: str) -> list[Any]:
        return self.results_by_id.get(batch_id, [])


class FakeClient:
    def __init__(self, batches: FakeBatches) -> None:
        self.messages = SimpleNamespace(batches=batches)


@pytest.fixture
def batches(monkeypatch: pytest.MonkeyPatch) -> FakeBatches:
    fake = FakeBatches()
    monkeypatch.setattr(ai, "make_client", lambda settings: FakeClient(fake))
    return fake


@pytest.fixture
def ai_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})


@pytest.fixture
def account(db_session: Session, user: User) -> Account:
    row = make_account(user, name="Sapphire Reserve", last4="4242", institution="Chase")
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def dining(db_session: Session, user: User) -> Category:
    row = make_category(user, name="Dining")
    db_session.add(row)
    db_session.flush()
    return row


def persist(db: Session, *rows: object) -> None:
    db.add_all(rows)
    db.flush()


def answer(custom_id: str, payload: dict[str, Any] | str, *, stop_reason: str = "end_turn") -> Any:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    message = SimpleNamespace(
        model="claude-sonnet-5-5",
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=2000,
            cache_creation_input_tokens=0,
        ),
        content=[SimpleNamespace(type="text", text=text)],
    )
    return SimpleNamespace(
        custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message)
    )


def failed(custom_id: str, kind: str) -> Any:
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type=kind))


# --- submit ---


def test_submit_sends_only_description_amount_and_date(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    row = make_transaction(
        user,
        account,
        reviewed=False,
        description_raw="CHIPOTLE 2291 ORLANDO FL",
        amount=Decimal("-18.40"),
        posted_date=date(2026, 10, 22),
    )
    persist(db_session, row)

    batch = ai_suggestions.submit(db_session, ai_settings, user.id, NOW)

    assert batch is not None and batch.anthropic_id == "msgbatch_1"
    [requests] = batches.created
    [request] = requests
    assert request["custom_id"] == str(row.id)
    params = request["params"]
    assert params["model"] == "claude-sonnet-5-5"
    assert params["messages"] == [
        {
            "role": "user",
            "content": "Description: CHIPOTLE 2291 ORLANDO FL\nAmount: -18.40\nDate: 2026-10-22",
        }
    ]
    [system] = params["system"]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert f"{dining.id} | Dining | expense" in system["text"]
    assert params["output_config"]["effort"] == "low"
    schema = params["output_config"]["format"]["schema"]
    assert schema["properties"]["category_id"]["enum"] == [str(dining.id), "none"]
    assert schema["additionalProperties"] is False
    # Nothing about the account leaves the server.
    sent = json.dumps(requests)
    for secret in ("Sapphire", "4242", "Chase", str(account.id)):
        assert secret not in sent

    [pending] = db_session.scalars(select(AISuggestion)).all()
    assert pending.transaction_id == row.id
    assert pending.status == AISuggestionStatus.pending


def test_archived_categories_are_not_offered_to_the_model(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    old = make_category(user, name="Old", archived=True)
    persist(db_session, old, make_transaction(user, account, reviewed=False))

    ai_suggestions.submit(db_session, ai_settings, user.id, NOW)

    params = batches.created[0][0]["params"]
    assert str(old.id) not in json.dumps(params)
    assert str(dining.id) in params["system"][0]["text"]


def test_past_choices_are_in_the_cached_prefix(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    persist(
        db_session,
        make_transaction(
            user,
            account,
            reviewed=True,
            description_raw="SQ *BLUE BOTTLE",
            merchant="Blue Bottle",
            category_id=dining.id,
            bucket=Bucket.business,
        ),
        make_transaction(user, account, reviewed=False, description_raw="NEW ROW"),
    )
    ai_suggestions.submit(db_session, ai_settings, user.id, NOW)
    text = batches.created[0][0]["params"]["system"][0]["text"]
    assert "SQ *BLUE BOTTLE (-12.34) -> Blue Bottle, Dining, business" in text


def test_candidates_skip_rows_something_else_already_covers(
    db_session: Session, user: User, account: Account, dining: Category
) -> None:
    wanted = make_transaction(user, account, reviewed=False, description_raw="WANTED")
    reviewed = make_transaction(
        user, account, reviewed=True, merchant="Known", category_id=dining.id
    )
    known_merchant = make_transaction(user, account, reviewed=False, merchant="Known")
    pending = make_transaction(user, account, reviewed=False, pending=True)
    parent = make_transaction(user, account, reviewed=False)
    already = make_transaction(user, account, reviewed=False)
    persist(db_session, wanted, reviewed, known_merchant, pending, parent, already)
    child = make_transaction(user, account, reviewed=True, parent_id=parent.id)
    pair = make_transaction(user, account, reviewed=False)
    persist(db_session, child, pair)
    pair.transfer_pair_id = child.id
    persist(
        db_session,
        AISuggestion(user_id=user.id, transaction_id=already.id, status=AISuggestionStatus.failed),
    )

    assert [r.id for r in ai_suggestions.candidates(db_session, user.id)] == [wanted.id]


@pytest.mark.parametrize("reason", ["disabled", "budget"])
def test_no_submit_when_ai_is_off_or_over_budget(
    db_session: Session,
    settings: Settings,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
    reason: str,
) -> None:
    persist(db_session, make_transaction(user, account, reviewed=False))
    if reason == "budget":
        ai_settings = ai_settings.model_copy(update={"ai_monthly_budget_usd": Decimal(0)})
    chosen = settings if reason == "disabled" else ai_settings

    assert ai_suggestions.submit(db_session, chosen, user.id, NOW) is None
    assert batches.created == []
    assert db_session.scalars(select(AISuggestion)).all() == []


def test_nothing_to_suggest_sends_nothing(
    db_session: Session, ai_settings: Settings, user: User, dining: Category, batches: FakeBatches
) -> None:
    assert ai_suggestions.submit(db_session, ai_settings, user.id, NOW) is None
    assert batches.created == []


# --- collect ---


def submitted(
    db: Session, settings: Settings, user: User, account: Account, count: int
) -> tuple[AIBatch, list[Transaction]]:
    rows = [
        make_transaction(user, account, reviewed=False, description_raw=f"ROW {i}")
        for i in range(count)
    ]
    persist(db, *rows)
    batch = ai_suggestions.submit(db, settings, user.id, NOW)
    assert batch is not None
    db.commit()  # as the worker does; collect's per-batch rollbacks start from here
    return batch, rows


def test_collect_stores_answers_and_logs_half_price_usage(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    batch, rows = submitted(db_session, ai_settings, user, account, 5)
    good, unknown_category, errored, expired, missing = (str(r.id) for r in rows)
    batches.status = "ended"
    batches.results_by_id[batch.anthropic_id] = [
        answer(
            good,
            {
                "category_id": str(dining.id),
                "bucket": "business",
                "merchant": " Chipotle ",
                "confidence": "high",
            },
        ),
        answer(
            unknown_category,
            {
                "category_id": str(uuid.uuid4()),
                "bucket": "nonsense",
                "merchant": "",
                "confidence": "low",
            },
        ),
        failed(errored, "errored"),
        failed(expired, "expired"),
    ]

    assert ai_suggestions.collect(db_session, ai_settings, NOW) == 1

    by_id = {str(s.transaction_id): s for s in db_session.scalars(select(AISuggestion))}
    assert (by_id[good].category_id, by_id[good].bucket, by_id[good].merchant) == (
        dining.id,
        Bucket.business,
        "Chipotle",
    )
    assert by_id[good].confidence == AIConfidence.high
    assert by_id[good].status == AISuggestionStatus.ready
    # An id outside the user's categories, an unknown bucket and a blank merchant are dropped.
    assert by_id[unknown_category].category_id is None
    assert by_id[unknown_category].bucket is None
    assert by_id[unknown_category].merchant is None
    assert by_id[errored].status == AISuggestionStatus.failed
    # Expired or missing: forgotten, so the next night sends them again.
    assert expired not in by_id and missing not in by_id
    assert db_session.get(AIBatch, batch.id).status == AIBatchStatus.ended  # type: ignore[union-attr]

    usage = db_session.scalars(select(AIUsage)).all()
    assert len(usage) == 2 and all(row.batch for row in usage)
    # (100 * 2 + 50 * 10 + 2000 * 0.2) / 1M, halved.
    assert usage[0].cost_usd == Decimal("0.000550")


def test_unparseable_or_refused_answers_fail(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    batch, rows = submitted(db_session, ai_settings, user, account, 2)
    batches.status = "ended"
    batches.results_by_id[batch.anthropic_id] = [
        answer(str(rows[0].id), "not json"),
        answer(str(rows[1].id), {}, stop_reason="refusal"),
    ]
    ai_suggestions.collect(db_session, ai_settings, NOW)
    statuses = {s.status for s in db_session.scalars(select(AISuggestion))}
    assert statuses == {AISuggestionStatus.failed}


def test_nul_characters_are_stripped(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    batch, rows = submitted(db_session, ai_settings, user, account, 3)
    batches.status = "ended"
    batches.results_by_id[batch.anthropic_id] = [
        answer(
            str(row.id),
            {
                "category_id": str(dining.id),
                "bucket": "personal",
                "merchant": "Chi\x00potle",
                "confidence": "high",
            },
        )
        for row in rows
    ]

    assert ai_suggestions.collect(db_session, ai_settings, NOW) == 1
    merchants = {s.merchant for s in db_session.scalars(select(AISuggestion))}
    assert merchants == {"Chipotle"}
    assert len(db_session.scalars(select(AIUsage)).all()) == 3


def test_an_answer_that_cant_be_stored_fails_alone(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch, rows = submitted(db_session, ai_settings, user, account, 3)
    answers = [good_answer(row, dining) for row in rows]
    answers[1] = answer(
        str(rows[1].id),
        {
            "category_id": str(dining.id),
            "bucket": "personal",
            "merchant": "Chi\x00potle",
            "confidence": "high",
        },
    )
    batches.status = "ended"
    batches.results_by_id[batch.anthropic_id] = answers
    # Let the NUL through, so storing that answer fails in Postgres.
    monkeypatch.setattr(ai, "clean_text", lambda text: text)

    assert ai_suggestions.collect(db_session, ai_settings, NOW) == 1

    db_session.rollback()
    by_id = {s.transaction_id: s for s in db_session.scalars(select(AISuggestion))}
    assert [by_id[row.id].status for row in rows] == [
        AISuggestionStatus.ready,
        AISuggestionStatus.failed,
        AISuggestionStatus.ready,
    ]
    assert by_id[rows[1].id].merchant is None
    assert status_of(db_session, batch) == AIBatchStatus.ended
    assert len(db_session.scalars(select(AIUsage)).all()) == 3


def test_collect_leaves_a_running_batch_alone(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    batch, _ = submitted(db_session, ai_settings, user, account, 1)
    assert ai_suggestions.collect(db_session, ai_settings, NOW) == 0
    assert db_session.get(AIBatch, batch.id).status == AIBatchStatus.in_progress  # type: ignore[union-attr]
    [suggestion] = db_session.scalars(select(AISuggestion)).all()
    assert suggestion.status == AISuggestionStatus.pending


# --- Inbox ---


def inbox(db_client: TestClient, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    response = db_client.get("/api/inbox", headers=headers)
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()["items"]}


def ready(user: User, row: Transaction, category: Category, merchant: str) -> AISuggestion:
    return AISuggestion(
        user_id=user.id,
        transaction_id=row.id,
        status=AISuggestionStatus.ready,
        category_id=category.id,
        bucket=Bucket.business,
        merchant=merchant,
        confidence=AIConfidence.high,
        model="claude-sonnet-5-5",
    )


def test_inbox_shows_the_ai_suggestion_when_history_has_none(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    dining: Category,
    auth_headers: dict[str, str],
) -> None:
    row = make_transaction(user, account, reviewed=False, description_raw="CHIPOTLE 2291")
    persist(db_session, row)
    persist(db_session, ready(user, row, dining, "Chipotle"))

    item = inbox(db_client, auth_headers)[str(row.id)]
    assert item["suggestion_source"] == "ai"
    assert item["suggested_category_id"] == str(dining.id)
    assert item["suggested_bucket"] == "business"
    assert item["suggested_merchant"] == "Chipotle"
    assert item["rule_worthy"] is False


def test_history_wins_over_ai(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    dining: Category,
    auth_headers: dict[str, str],
) -> None:
    travel = make_category(user, name="Travel")
    persist(db_session, travel)
    persist(
        db_session,
        make_transaction(user, account, reviewed=True, merchant="Hertz", category_id=travel.id),
    )
    row = make_transaction(user, account, reviewed=False, merchant="Hertz")
    persist(db_session, row)
    persist(db_session, ready(user, row, dining, "Hertz Car Rental"))

    item = inbox(db_client, auth_headers)[str(row.id)]
    assert item["suggestion_source"] == "history"
    assert item["suggested_category_id"] == str(travel.id)
    assert item["suggested_merchant"] is None


def test_pending_or_failed_ai_suggestions_dont_show(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
) -> None:
    row = make_transaction(user, account, reviewed=False)
    persist(db_session, row)
    persist(
        db_session,
        AISuggestion(user_id=user.id, transaction_id=row.id, status=AISuggestionStatus.pending),
    )
    item = inbox(db_client, auth_headers)[str(row.id)]
    assert item["suggestion_source"] is None
    assert item["suggested_category_id"] is None


def test_rule_worthy_after_two_matching_confirmations(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    dining: Category,
    auth_headers: dict[str, str],
) -> None:
    confirmed = [
        make_transaction(user, account, reviewed=True, merchant="Chipotle", category_id=dining.id)
        for _ in range(2)
    ]
    row = make_transaction(user, account, reviewed=False, description_raw="CHIPOTLE 2291")
    persist(db_session, *confirmed, row)
    persist(db_session, ready(user, row, dining, "Chipotle"))

    item = inbox(db_client, auth_headers)[str(row.id)]
    assert item["rule_worthy"] is True


# --- worker ---


def test_worker_submit_failure_never_raises(
    db_session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import worker

    def boom(*args: object) -> None:
        raise RuntimeError("anthropic down")

    monkeypatch.setattr(ai_suggestions, "submit", boom)
    worker.submit_suggestions(db_session, user.id, NOW)


# --- collect failures ---


def two_batches(
    db: Session, settings: Settings, user: User, account: Account
) -> tuple[AIBatch, Transaction, AIBatch, Transaction]:
    first, [first_row] = submitted(db, settings, user, account, 1)
    second, [second_row] = submitted(db, settings, user, account, 1)
    return first, first_row, second, second_row


def good_answer(row: Transaction, category: Category) -> Any:
    return answer(
        str(row.id),
        {
            "category_id": str(category.id),
            "bucket": "personal",
            "merchant": "X",
            "confidence": "high",
        },
    )


def status_of(db: Session, batch: AIBatch) -> AIBatchStatus:
    db.refresh(batch)
    return batch.status


def test_one_unreadable_batch_doesnt_cost_the_others_their_answers(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    first, _, second, second_row = two_batches(db_session, ai_settings, user, account)
    batches.status = "ended"
    request = httpx2.Request("GET", "https://api.anthropic.com/v1/messages/batches/x")
    batches.errors[first.anthropic_id] = anthropic.APIConnectionError(request=request)
    batches.results_by_id[second.anthropic_id] = [good_answer(second_row, dining)]

    assert ai_suggestions.collect(db_session, ai_settings, NOW) == 1

    assert status_of(db_session, first) == AIBatchStatus.in_progress  # retried next run
    assert status_of(db_session, second) == AIBatchStatus.ended
    [usage] = db_session.scalars(select(AIUsage)).all()
    assert usage.batch


def test_a_batch_anthropic_no_longer_has_is_closed_and_resent(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    batch, [row] = submitted(db_session, ai_settings, user, account, 1)
    request = httpx2.Request("GET", "https://api.anthropic.com/v1/messages/batches/x")
    batches.errors[batch.anthropic_id] = anthropic.NotFoundError(
        "not found", response=httpx2.Response(404, request=request), body=None
    )

    ai_suggestions.collect(db_session, ai_settings, NOW)

    assert status_of(db_session, batch) == AIBatchStatus.ended
    assert db_session.scalars(select(AISuggestion)).all() == []
    assert [r.id for r in ai_suggestions.candidates(db_session, user.id)] == [row.id]


def test_a_batch_failing_for_days_is_given_up(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    dining: Category,
    batches: FakeBatches,
) -> None:
    batch, _ = submitted(db_session, ai_settings, user, account, 1)
    batches.errors[batch.anthropic_id] = RuntimeError("still broken")
    later = batch.created_at + ai_suggestions.GIVE_UP_AFTER + timedelta(minutes=1)

    ai_suggestions.collect(db_session, ai_settings, later - timedelta(days=1))
    assert status_of(db_session, batch) == AIBatchStatus.in_progress
    ai_suggestions.collect(db_session, ai_settings, later)
    assert status_of(db_session, batch) == AIBatchStatus.ended
