"""AI year-end tax review (#175). The SDK client is replaced, so no test reaches Anthropic."""

import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.errors import Conflict
from app.main import create_app
from app.models import Account, TaxReview, TaxReviewItem, User
from app.models.enums import Bucket, IncomeType, TaxReviewKind, TaxReviewStatus
from app.services import ai, tax_review
from tests.factories import (
    make_account,
    make_category,
    make_income_entry,
    make_mileage_trip,
    make_receipt,
    make_transaction,
)

NOW = datetime(2027, 1, 10, 15, 0, tzinfo=UTC)


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: Any = {"summary": "A quiet year.", "items": []}

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=40000,
                output_tokens=3000,
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
def ai_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})


@pytest.fixture
def ai_client(ai_settings: Settings, db_session: Session) -> Iterator[TestClient]:
    app = create_app(ai_settings)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as c:
        yield c


@pytest.fixture
def account(db_session: Session, user: User) -> Account:
    row = make_account(user, name="Sapphire Reserve", last4="4242")
    db_session.add(row)
    db_session.flush()
    return row


def add(db: Session, *rows: object) -> None:
    db.add_all(rows)
    db.flush()


# --- request ---


def test_requesting_again_replaces_a_finished_review(db_session: Session, user: User) -> None:
    review = tax_review.request(db_session, user.id, 2026, NOW)
    review.status = TaxReviewStatus.ready
    db_session.flush()
    again = tax_review.request(db_session, user.id, 2026, NOW)
    assert again.id == review.id
    assert again.status == TaxReviewStatus.requested


def test_one_review_at_a_time_unless_it_went_stale(db_session: Session, user: User) -> None:
    review = tax_review.request(db_session, user.id, 2026, NOW)
    with pytest.raises(Conflict):
        tax_review.request(db_session, user.id, 2026, review.updated_at + timedelta(minutes=1))
    later = review.updated_at + tax_review.STALE_AFTER + timedelta(minutes=1)
    assert tax_review.request(db_session, user.id, 2026, later).status == TaxReviewStatus.requested


# --- prompt ---


def test_prompt_has_the_year_summary_mileage_and_income(
    db_session: Session, ai_settings: Settings, user: User, account: Account
) -> None:
    travel = make_category(user, name="Travel", tax_line="travel")
    add(db_session, travel)
    flight = make_transaction(
        user,
        account,
        posted_date=date(2026, 3, 4),
        merchant="Delta",
        amount=Decimal("-412.30"),
        category_id=travel.id,
        bucket=Bucket.business,
    )
    lunch = make_transaction(
        user, account, posted_date=date(2026, 5, 1), description_raw="CHIPOTLE"
    )
    hold = make_transaction(
        user, account, posted_date=date(2026, 6, 1), description_raw="HOLD", pending=True
    )
    other_year = make_transaction(
        user, account, posted_date=date(2025, 12, 31), description_raw="LAST YEAR"
    )
    add(db_session, flight, lunch, hold, other_year)
    add(
        db_session,
        make_receipt(user, flight),
        make_mileage_trip(user, date=date(2026, 3, 5), miles=Decimal("94.3")),
        make_income_entry(
            user, date=date(2026, 4, 1), type=IncomeType.event_pay, received=Decimal("800.00")
        ),
    )

    prompt, refs = tax_review.build_prompt(db_session, ai_settings, user.id, 2026)

    assert refs == {"T1": flight.id, "T2": lunch.id}
    assert "T1 | 2026-03-04 | Delta | -412.30 | business | Travel (travel) | receipt" in prompt
    assert "T2 | 2026-05-01 | CHIPOTLE | -12.34 | personal | uncategorized | no receipt" in prompt
    assert "HOLD" not in prompt and "LAST YEAR" not in prompt
    assert "- 2026-03: 94.3" in prompt and "- 2026-04: 0.0" in prompt
    assert "- event_pay: 800.00" in prompt
    assert "Schedule C summary" in prompt
    assert "Sapphire" not in prompt and "4242" not in prompt


# --- write ---


def test_write_keeps_valid_items_and_links_them(
    db_session: Session, ai_settings: Settings, user: User, account: Account, fake: FakeMessages
) -> None:
    lunch = make_transaction(
        user, account, posted_date=date(2026, 5, 1), description_raw="CLIENT LUNCH"
    )
    add(db_session, lunch)
    review = tax_review.request(db_session, user.id, 2026, NOW)
    fake.answer = {
        "summary": "  Mostly travel.  ",
        "items": [
            {"kind": "missed_deduction", "ref": "T1", "note": "Looks like a client lunch."},
            {"kind": "missed_deduction", "ref": "T1", "note": "Looks like a client lunch."},
            {"kind": "mileage_gap", "ref": "", "note": "No miles logged in April."},
            {"kind": "other", "ref": "T42", "note": "Not a reference it was given."},
            {"kind": "nonsense", "ref": "T1", "note": "Unknown kind."},
        ],
    }

    tax_review.write(db_session, ai_settings, review, NOW)

    assert review.status == TaxReviewStatus.ready
    assert review.summary == "Mostly travel."
    items = db_session.scalars(
        select(TaxReviewItem)
        .where(TaxReviewItem.review_id == review.id)
        .order_by(TaxReviewItem.position)
    ).all()
    assert [(i.kind, i.transaction_id) for i in items] == [
        (TaxReviewKind.missed_deduction, lunch.id),
        (TaxReviewKind.mileage_gap, None),
    ]
    [sent] = fake.calls
    assert sent["output_config"]["effort"] == "high"
    assert sent["timeout"] == 600


@pytest.mark.parametrize("answer", ["not json", {"summary": "", "items": []}])
def test_an_unreadable_answer_fails_the_review(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages, answer: Any
) -> None:
    review = tax_review.request(db_session, user.id, 2026, NOW)
    fake.answer = answer
    tax_review.write(db_session, ai_settings, review, NOW)
    assert review.status == TaxReviewStatus.failed
    assert review.error


def test_over_budget_fails_with_a_reason(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    review = tax_review.request(db_session, user.id, 2026, NOW)
    capped = ai_settings.model_copy(update={"ai_monthly_budget_usd": Decimal(0)})
    tax_review.write(db_session, capped, review, NOW)
    assert review.status == TaxReviewStatus.failed
    assert review.error == "AI assist has reached this month's budget."
    assert fake.calls == []


def test_worker_runs_requested_reviews(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    tax_review.request(db_session, user.id, 2026, NOW)
    db_session.commit()
    assert tax_review.run_requested(db_session, ai_settings, NOW) == 1
    review = db_session.scalars(select(TaxReview)).one()
    assert review.status == TaxReviewStatus.ready
    assert tax_review.run_requested(db_session, ai_settings, NOW) == 0


# --- endpoints and export ---


def test_request_then_read_the_review(
    ai_client: TestClient,
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
    fake: FakeMessages,
) -> None:
    flight = make_transaction(
        user, account, posted_date=date(2026, 3, 4), merchant="Delta", amount=Decimal("-412.30")
    )
    add(db_session, flight)
    assert (
        ai_client.get("/api/ai/tax-review", params={"year": 2026}, headers=auth_headers).json()
        is None
    )

    response = ai_client.post("/api/ai/tax-review", json={"year": 2026}, headers=auth_headers)
    assert response.status_code == 202
    assert response.json()["status"] == "requested"
    assert (
        ai_client.post("/api/ai/tax-review", json={"year": 2026}, headers=auth_headers).status_code
        == 409
    )

    fake.answer = {
        "summary": "One flight.",
        "items": [{"kind": "missing_receipt", "ref": "T1", "note": "No receipt for the flight."}],
    }
    tax_review.run_requested(db_session, ai_settings, NOW)

    body = ai_client.get("/api/ai/tax-review", params={"year": 2026}, headers=auth_headers).json()
    assert body["status"] == "ready"
    assert body["summary"] == "One flight."
    [item] = body["items"]
    assert item["kind"] == "missing_receipt"
    assert item["transaction"]["id"] == str(flight.id)

    export = ai_client.get("/api/tax/export", params={"year": 2026}, headers=auth_headers)
    archive = zipfile.ZipFile(io.BytesIO(export.content))
    text = archive.read("ai-review-2026.txt").decode()
    assert "One flight." in text
    assert "Missing receipt [2026-03-04 Delta -412.30]: No receipt for the flight." in text


def test_no_review_file_in_the_export_until_one_is_ready(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    export = db_client.get("/api/tax/export", params={"year": 2026}, headers=auth_headers)
    names = zipfile.ZipFile(io.BytesIO(export.content)).namelist()
    assert "ai-review-2026.txt" not in names


def test_without_ai_requesting_is_503(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    assert (
        db_client.post("/api/ai/tax-review", json={"year": 2026}, headers=auth_headers).status_code
        == 503
    )


def test_reviews_are_owner_only(
    db_client: TestClient, db_session: Session, other_user: User, auth_headers: dict[str, str]
) -> None:
    add(
        db_session,
        TaxReview(
            user_id=other_user.id, year=2026, status=TaxReviewStatus.ready, summary="Theirs."
        ),
    )
    assert (
        db_client.get("/api/ai/tax-review", params={"year": 2026}, headers=auth_headers).json()
        is None
    )


def test_a_failure_after_the_call_keeps_its_usage_row(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    fake: FakeMessages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models import AIUsage

    tax_review.request(db_session, user.id, 2026, NOW)
    db_session.commit()
    real_clean = tax_review.ai.clean_text

    def boom(text: str) -> str:
        raise RuntimeError("storing failed")

    monkeypatch.setattr(tax_review.ai, "clean_text", boom)
    tax_review.run_requested(db_session, ai_settings, NOW)
    monkeypatch.setattr(tax_review.ai, "clean_text", real_clean)

    review = db_session.scalars(select(TaxReview)).one()
    assert review.status == TaxReviewStatus.failed
    assert len(db_session.scalars(select(AIUsage)).all()) == 1


def test_nul_characters_are_stripped(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    review = tax_review.request(db_session, user.id, 2026, NOW)
    fake.answer = {
        "summary": "Mostly\u0000 travel.",
        "items": [{"kind": "other", "ref": "", "note": "Check\u0000 this."}],
    }
    tax_review.write(db_session, ai_settings, review, NOW)
    db_session.flush()
    assert review.summary == "Mostly travel."


def test_a_review_left_running_by_a_restart_is_picked_up_again(
    db_session: Session, ai_settings: Settings, user: User, fake: FakeMessages
) -> None:
    review = tax_review.request(db_session, user.id, 2026, NOW)
    review.status = TaxReviewStatus.running
    db_session.commit()
    assert (
        tax_review.run_requested(db_session, ai_settings, review.updated_at + timedelta(minutes=5))
        == 0
    )
    later = review.updated_at + tax_review.STALE_AFTER + timedelta(minutes=1)
    assert tax_review.run_requested(db_session, ai_settings, later) == 1
    assert db_session.scalars(select(TaxReview)).one().status == TaxReviewStatus.ready


def test_a_broken_review_doesnt_break_the_export(
    db_client: TestClient, user: User, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("bad row")

    monkeypatch.setattr(tax_review, "as_text", boom)
    export = db_client.get("/api/tax/export", params={"year": 2026}, headers=auth_headers)
    assert export.status_code == 200
    assert "transactions-2026.csv" in zipfile.ZipFile(io.BytesIO(export.content)).namelist()
