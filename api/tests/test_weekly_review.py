"""AI weekly review (#173). The SDK client is replaced, so no test reaches Anthropic."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, AIUsage, ReviewFinding, User, WeeklyReview
from app.models.enums import Bucket, Cadence, FindingKind
from app.services import ai, weekly_review
from tests.factories import (
    make_account,
    make_category,
    make_receipt,
    make_subscription,
    make_transaction,
)

# Monday 26 October 2026, 08:00 in New York: last week is 19-25 October.
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: Any = {"summary": "A quiet week.", "findings": []}

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=3000,
                output_tokens=400,
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
def account(db_session: Session, user: User) -> Account:
    row = make_account(user, name="Sapphire Reserve", last4="4242")
    db_session.add(row)
    db_session.flush()
    return row


def add(db: Session, *rows: object) -> None:
    db.add_all(rows)
    db.flush()


def test_last_week_is_the_monday_to_sunday_before() -> None:
    assert weekly_review.last_week(NOW) == weekly_review.Week(
        date(2026, 10, 19), date(2026, 10, 25)
    )
    # Sunday night in New York is still that week; Monday 00:30 UTC is Sunday there.
    sunday = datetime(2026, 10, 26, 0, 30, tzinfo=UTC)
    assert weekly_review.last_week(sunday).start == date(2026, 10, 12)


def test_prompt_has_the_weeks_rows_receipts_subscriptions_and_averages(
    db_session: Session, user: User, account: Account
) -> None:
    dining = make_category(user, name="Dining")
    add(db_session, dining)
    netflix = make_subscription(
        user, name="Netflix", amount=Decimal("-15.49"), cadence=Cadence.monthly
    )
    add(db_session, netflix)
    lunch = make_transaction(
        user,
        account,
        posted_date=date(2026, 10, 20),
        description_raw="CHIPOTLE 2291",
        merchant="Chipotle",
        amount=Decimal("-18.40"),
        category_id=dining.id,
        bucket=Bucket.business,
    )
    stream = make_transaction(
        user,
        account,
        posted_date=date(2026, 10, 21),
        description_raw="NETFLIX.COM",
        amount=Decimal("-17.99"),
        subscription_id=netflix.id,
    )
    earlier = make_transaction(
        user, account, posted_date=date(2026, 9, 1), amount=Decimal("-80.00"), category_id=dining.id
    )
    outside = make_transaction(
        user, account, posted_date=date(2026, 10, 26), description_raw="NEXT WEEK"
    )
    add(db_session, lunch, stream, earlier, outside)
    add(db_session, make_receipt(user, stream))

    prompt, refs = weekly_review.build_prompt(db_session, user.id, weekly_review.last_week(NOW))

    assert refs == {"T1": lunch.id, "T2": stream.id}
    assert "T1 | 2026-10-20 | Chipotle | -18.40 | business | Dining | no receipt" in prompt
    assert (
        "T2 | 2026-10-21 | NETFLIX.COM | -17.99 | personal | uncategorized | receipt | "
        "subscription: Netflix" in prompt
    )
    assert "- Netflix | -15.49 | monthly" in prompt
    # $80 in early September, so history runs from then: 7 weeks to 19 October.
    assert "- Dining: 11.43" in prompt
    assert "NEXT WEEK" not in prompt
    assert "Sapphire" not in prompt and "4242" not in prompt


def test_review_keeps_valid_findings_only(
    db_session: Session, ai_settings: Settings, user: User, account: Account, fake: FakeMessages
) -> None:
    rows = [
        make_transaction(user, account, posted_date=date(2026, 10, 20 + i), description_raw=f"R{i}")
        for i in range(2)
    ]
    add(db_session, *rows)
    fake.answer = {
        "summary": "  You spent $24.68.  ",
        "findings": [
            {"kind": "double_charge", "ref": "T2", "note": "Charged twice."},
            {"kind": "double_charge", "ref": "T2", "note": "Same again."},  # duplicate
            {"kind": "unusual", "ref": "T99", "note": "Not a given ref."},
            {"kind": "made_up", "ref": "T1", "note": "Unknown kind."},
            {"kind": "missing_receipt", "ref": "T1", "note": "No receipt."},
        ],
    }

    review = weekly_review.run_review(db_session, ai_settings, user.id, NOW)

    assert review is not None
    assert review.summary == "You spent $24.68."
    assert (review.week_start, review.week_end) == (date(2026, 10, 19), date(2026, 10, 25))
    findings = db_session.scalars(
        select(ReviewFinding)
        .where(ReviewFinding.review_id == review.id)
        .order_by(ReviewFinding.position)
    ).all()
    assert [(f.kind, f.transaction_id) for f in findings] == [
        (FindingKind.double_charge, rows[1].id),
        (FindingKind.missing_receipt, rows[0].id),
    ]
    [sent] = fake.calls
    assert sent["output_config"]["effort"] == "medium"


def test_review_runs_once_per_week(
    db_session: Session, ai_settings: Settings, user: User, account: Account, fake: FakeMessages
) -> None:
    add(db_session, make_transaction(user, account, posted_date=date(2026, 10, 20)))
    assert weekly_review.run_review(db_session, ai_settings, user.id, NOW) is not None
    assert weekly_review.run_review(db_session, ai_settings, user.id, NOW) is None
    assert len(fake.calls) == 1


def test_an_empty_week_or_no_ai_writes_nothing(
    db_session: Session,
    settings: Settings,
    ai_settings: Settings,
    user: User,
    account: Account,
    fake: FakeMessages,
) -> None:
    assert weekly_review.run_review(db_session, ai_settings, user.id, NOW) is None
    add(db_session, make_transaction(user, account, posted_date=date(2026, 10, 20)))
    assert weekly_review.run_review(db_session, settings, user.id, NOW) is None
    assert fake.calls == []
    fake.answer = "not json"
    assert weekly_review.run_review(db_session, ai_settings, user.id, NOW) is None
    assert db_session.scalars(select(WeeklyReview)).all() == []


def test_a_crash_after_the_call_keeps_its_usage_row(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    account: Account,
    fake: FakeMessages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(db_session, make_transaction(user, account, posted_date=date(2026, 10, 20)))
    fake.answer = {
        "summary": "A week.",
        "findings": [{"kind": "unusual", "ref": "T1", "note": "x"}],
    }

    def boom(**fields: Any) -> None:
        raise RuntimeError("storing failed")

    monkeypatch.setattr(weekly_review, "ReviewFinding", boom)
    with pytest.raises(RuntimeError):
        weekly_review.run_review(db_session, ai_settings, user.id, NOW)

    # The worker rolls back on a failure; the billed call's row was already committed.
    db_session.rollback()
    assert db_session.scalars(select(AIUsage)).one().feature == "weekly_review"
    assert db_session.scalars(select(WeeklyReview)).all() == []


def test_nul_characters_are_stripped(
    db_session: Session, ai_settings: Settings, user: User, account: Account, fake: FakeMessages
) -> None:
    add(db_session, make_transaction(user, account, posted_date=date(2026, 10, 20)))
    fake.answer = {
        "summary": "A quiet\x00 week.",
        "findings": [{"kind": "unusual", "ref": "T1", "note": "Big\x00 one."}],
    }
    review = weekly_review.run_review(db_session, ai_settings, user.id, NOW)
    assert review is not None
    assert review.summary == "A quiet week."
    assert db_session.scalars(select(ReviewFinding)).one().note == "Big one."


# --- GET /api/ai/weekly-review ---


def test_endpoint_returns_the_latest_review(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    account: Account,
    auth_headers: dict[str, str],
) -> None:
    flagged = make_transaction(
        user, account, posted_date=date(2026, 10, 20), merchant="Chipotle", amount=Decimal("-18.40")
    )
    gone = make_transaction(user, account, posted_date=date(2026, 10, 21))
    add(db_session, flagged, gone)
    old = WeeklyReview(
        user_id=user.id,
        week_start=date(2026, 10, 12),
        week_end=date(2026, 10, 18),
        summary="Old.",
        model="m",
    )
    new = WeeklyReview(
        user_id=user.id,
        week_start=date(2026, 10, 19),
        week_end=date(2026, 10, 25),
        summary="New.",
        model="m",
    )
    theirs = WeeklyReview(
        user_id=other_user.id,
        week_start=date(2026, 11, 2),
        week_end=date(2026, 11, 8),
        summary="Theirs.",
        model="m",
    )
    add(db_session, old, new, theirs)
    add(
        db_session,
        ReviewFinding(
            user_id=user.id,
            review_id=new.id,
            kind=FindingKind.unusual,
            transaction_id=gone.id,
            note="Gone.",
            position=1,
        ),
        ReviewFinding(
            user_id=user.id,
            review_id=new.id,
            kind=FindingKind.double_charge,
            transaction_id=flagged.id,
            note="Twice.",
            position=0,
        ),
    )
    db_session.delete(gone)
    db_session.flush()

    body = db_client.get("/api/ai/weekly-review", headers=auth_headers).json()

    assert body["summary"] == "New."
    assert body["week_start"] == "2026-10-19"
    first, second = body["findings"]
    assert first["kind"] == "double_charge"
    assert first["transaction"]["id"] == str(flagged.id)
    assert first["transaction"]["merchant"] == "Chipotle"
    assert first["transaction"]["amount"] == "-18.40"
    assert second["transaction"] is None


def test_endpoint_is_null_before_the_first_review(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    response = db_client.get("/api/ai/weekly-review", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() is None


def test_averages_only_count_settled_spending_over_the_weeks_there_are(
    db_session: Session, user: User, account: Account
) -> None:
    dining = make_category(user, name="Dining")
    add(db_session, dining)
    add(
        db_session,
        # Two weeks of history before 19 October.
        make_transaction(
            user,
            account,
            posted_date=date(2026, 10, 5),
            amount=Decimal("-60.00"),
            category_id=dining.id,
        ),
        make_transaction(
            user,
            account,
            posted_date=date(2026, 10, 6),
            amount=Decimal("10.00"),
            category_id=dining.id,
        ),
        make_transaction(
            user,
            account,
            posted_date=date(2026, 10, 7),
            amount=Decimal("-500.00"),
            category_id=dining.id,
            bucket=Bucket.transfer,
        ),
        make_transaction(
            user,
            account,
            posted_date=date(2026, 10, 8),
            amount=Decimal("-99.00"),
            category_id=dining.id,
            pending=True,
        ),
    )
    averages = weekly_review._category_averages(db_session, user.id, weekly_review.last_week(NOW))
    assert averages == {"Dining": Decimal("25.00")}  # $50 net over 2 weeks


def test_pending_rows_stay_out_of_the_review(
    db_session: Session, user: User, account: Account
) -> None:
    add(
        db_session,
        make_transaction(user, account, posted_date=date(2026, 10, 20), description_raw="SETTLED"),
        make_transaction(
            user, account, posted_date=date(2026, 10, 21), description_raw="HOLD", pending=True
        ),
    )
    prompt, refs = weekly_review.build_prompt(db_session, user.id, weekly_review.last_week(NOW))
    assert len(refs) == 1
    assert "SETTLED" in prompt and "HOLD" not in prompt
