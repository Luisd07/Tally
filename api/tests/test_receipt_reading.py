"""AI receipt reading (#170): the worker reads receipt images and the server checks the
arithmetic. The SDK client is replaced, so no test reaches Anthropic.
"""

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIUsage, Category, Receipt, ReceiptReading, Transaction, User
from app.models.enums import ReceiptReadingStatus
from app.services import ai, receipt_reading
from tests.factories import make_account, make_category, make_receipt, make_transaction

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: list[Any] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        stop_reason = "end_turn"
        if answer == "refusal":
            stop_reason, answer = "refusal", ""
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason=stop_reason,
            usage=SimpleNamespace(
                input_tokens=1500,
                output_tokens=300,
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
def ai_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={"anthropic_api_key": SecretStr("sk-ant-test"), "receipts_dir": tmp_path}
    )


@pytest.fixture
def dining(db_session: Session, user: User) -> Category:
    row = make_category(user, name="Dining")
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def charge(db_session: Session, user: User) -> Transaction:
    account = make_account(user, name="Sapphire Reserve", last4="4242")
    db_session.add(account)
    db_session.flush()
    row = make_transaction(user, account, amount=Decimal("-21.60"), description_raw="CHIPOTLE")
    db_session.add(row)
    db_session.flush()
    return row


def stored_receipt(
    db: Session, settings: Settings, user: User, transaction: Transaction, name: str = "a.jpg"
) -> Receipt:
    path = settings.receipts_dir / "2026" / "10" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG)
    receipt = make_receipt(user, transaction, file_path=f"2026/10/{name}")
    db.add(receipt)
    db.flush()
    return receipt


GOOD = {
    "merchant": "Chipotle",
    "date": "2026-10-22",
    "subtotal": "20.00",
    "tax": "1.60",
    "tip": "",
    "total": "21.60",
    "line_items": [
        {"description": "Burrito bowl", "amount": "12.50"},
        {"description": "Chips & guac", "amount": "7.50"},
    ],
    "category_id": "none",
}


def test_reads_a_receipt_and_checks_the_math(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    dining: Category,
    charge: Transaction,
    fake: FakeMessages,
) -> None:
    receipt = stored_receipt(db_session, ai_settings, user, charge)
    fake.answers = [{**GOOD, "category_id": str(dining.id)}]

    reading = receipt_reading.read_receipt(db_session, ai_settings, receipt, NOW)

    assert reading.status == ReceiptReadingStatus.ready
    assert (reading.merchant, reading.receipt_date) == ("Chipotle", date(2026, 10, 22))
    assert (reading.subtotal, reading.tax, reading.tip, reading.total) == (
        Decimal("20.00"),
        Decimal("1.60"),
        None,
        Decimal("21.60"),
    )
    assert reading.line_items == [
        {"description": "Burrito bowl", "amount": "12.50"},
        {"description": "Chips & guac", "amount": "7.50"},
    ]
    assert reading.category_id == dining.id
    assert reading.adds_up is True
    assert reading.matches_transaction is True
    assert db_session.scalars(select(AIUsage)).one().feature == "receipt_reading"

    [sent] = fake.calls
    image = sent["messages"][0]["content"][0]
    assert image["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": base64.standard_b64encode(JPEG).decode(),
    }
    assert "Sapphire" not in json.dumps(sent) and "4242" not in json.dumps(sent)
    assert sent["output_config"]["format"]["schema"]["properties"]["category_id"]["enum"] == [
        str(dining.id),
        "none",
    ]


@pytest.mark.parametrize(
    ("change", "adds_up", "matches"),
    [
        ({"total": "25.00"}, False, False),  # items + tax don't make the total
        ({"subtotal": "19.00"}, False, True),  # items don't make the subtotal
        ({"tip": "3.00", "total": "24.60"}, True, False),  # tip on top: adds up, not the charge
        ({"line_items": []}, None, True),  # nothing to check
        ({"subtotal": "", "total": "21.62"}, True, False),  # within a few cents of rounding
    ],
)
def test_flags(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    charge: Transaction,
    fake: FakeMessages,
    change: dict[str, Any],
    adds_up: bool | None,
    matches: bool,
) -> None:
    receipt = stored_receipt(db_session, ai_settings, user, charge)
    fake.answers = [{**GOOD, **change}]
    reading = receipt_reading.read_receipt(db_session, ai_settings, receipt, NOW)
    assert (reading.adds_up, reading.matches_transaction) == (adds_up, matches)


def test_out_of_schema_values_are_dropped(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    other_user: User,
    charge: Transaction,
    fake: FakeMessages,
) -> None:
    theirs = make_category(other_user, name="Theirs")
    db_session.add(theirs)
    db_session.flush()
    receipt = stored_receipt(db_session, ai_settings, user, charge)
    fake.answers = [
        {
            **GOOD,
            "date": "last tuesday",
            "total": "lots",
            "category_id": str(theirs.id),
            "line_items": [
                {"description": "", "amount": "1.00"},
                {"description": "x", "amount": "?"},
            ],
        }
    ]
    reading = receipt_reading.read_receipt(db_session, ai_settings, receipt, NOW)
    assert reading.receipt_date is None
    assert reading.total is None
    assert reading.category_id is None
    assert reading.line_items == []


def test_a_refusal_or_unreadable_answer_fails_for_good(
    db_session: Session, ai_settings: Settings, user: User, charge: Transaction, fake: FakeMessages
) -> None:
    refused = stored_receipt(db_session, ai_settings, user, charge, "r.jpg")
    garbled = stored_receipt(db_session, ai_settings, user, charge, "g.jpg")
    fake.answers = ["refusal", "not json"]
    assert receipt_reading.read_receipt(db_session, ai_settings, refused, NOW).status == (
        ReceiptReadingStatus.failed
    )
    assert receipt_reading.read_receipt(db_session, ai_settings, garbled, NOW).status == (
        ReceiptReadingStatus.failed
    )
    assert receipt_reading.unread(db_session) == []


def test_api_errors_retry_then_give_up(
    db_session: Session, ai_settings: Settings, user: User, charge: Transaction, fake: FakeMessages
) -> None:
    import anthropic
    import httpx2

    receipt = stored_receipt(db_session, ai_settings, user, charge)
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fake.answers = [anthropic.APIConnectionError(request=request) for _ in range(3)]

    readings = []
    for attempt in range(1, 4):
        assert receipt_reading.unread(db_session) == [receipt]
        readings.append(receipt_reading.read_receipt(db_session, ai_settings, receipt, NOW))
        assert readings[-1].attempts == attempt
    reading = readings[-1]
    assert reading.status == ReceiptReadingStatus.failed
    assert receipt_reading.unread(db_session) == []


def test_a_missing_file_fails_without_a_call(
    db_session: Session, ai_settings: Settings, user: User, charge: Transaction, fake: FakeMessages
) -> None:
    receipt = make_receipt(user, charge, file_path="2026/10/missing.jpg")
    db_session.add(receipt)
    db_session.flush()
    reading = receipt_reading.read_receipt(db_session, ai_settings, receipt, NOW)
    assert reading.status == ReceiptReadingStatus.failed
    assert fake.calls == []


def test_run_reads_each_receipt_and_stops_at_the_cap(
    db_session: Session, ai_settings: Settings, user: User, charge: Transaction, fake: FakeMessages
) -> None:
    for name in ("1.jpg", "2.jpg", "3.jpg"):
        stored_receipt(db_session, ai_settings, user, charge, name)
    db_session.commit()
    fake.answers = [GOOD, GOOD, GOOD]
    # The first call costs about $0.006; a cap just above that stops the run after it.
    capped = ai_settings.model_copy(update={"ai_monthly_budget_usd": Decimal("0.006")})

    assert receipt_reading.read_new_receipts(db_session, capped, NOW) == 1
    assert len(fake.calls) == 1
    assert len(db_session.scalars(select(ReceiptReading)).all()) == 1


def test_a_crash_after_the_call_keeps_its_usage_row_and_fails_the_receipt(
    db_session: Session,
    ai_settings: Settings,
    user: User,
    charge: Transaction,
    fake: FakeMessages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_receipt(db_session, ai_settings, user, charge)
    db_session.commit()
    fake.answers = [GOOD]

    def boom(*args: Any) -> None:
        raise RuntimeError("storing failed")

    monkeypatch.setattr(receipt_reading, "fill", boom)
    assert receipt_reading.read_new_receipts(db_session, ai_settings, NOW) == 0

    db_session.rollback()
    assert db_session.scalars(select(AIUsage)).one().feature == "receipt_reading"
    assert db_session.scalars(select(ReceiptReading)).one().status == ReceiptReadingStatus.failed


def test_nul_characters_are_stripped(
    db_session: Session, ai_settings: Settings, user: User, charge: Transaction, fake: FakeMessages
) -> None:
    receipt = stored_receipt(db_session, ai_settings, user, charge)
    fake.answers = [
        {
            **GOOD,
            "merchant": "Chi\x00potle",
            "line_items": [
                {"description": "Burrito\x00 bowl", "amount": "12.50"},
                {"description": "\x00", "amount": "7.50"},
            ],
        }
    ]
    reading = receipt_reading.read_receipt(db_session, ai_settings, receipt, NOW)
    db_session.flush()
    assert reading.merchant == "Chipotle"
    assert reading.line_items == [{"description": "Burrito bowl", "amount": "12.50"}]


def test_off_without_a_key(
    db_session: Session,
    settings: Settings,
    ai_settings: Settings,
    user: User,
    charge: Transaction,
    fake: FakeMessages,
) -> None:
    stored_receipt(db_session, ai_settings, user, charge)
    assert receipt_reading.read_new_receipts(db_session, settings, NOW) == 0
    assert fake.calls == []


# --- GET /api/transactions/{id}/receipts ---


def test_receipts_list_with_readings(
    db_client: TestClient,
    db_session: Session,
    user: User,
    charge: Transaction,
    auth_headers: dict[str, str],
) -> None:
    read = make_receipt(
        user, charge, file_path="2026/10/read.jpg", uploaded_at=datetime(2026, 10, 24, tzinfo=UTC)
    )
    unread = make_receipt(
        user, charge, file_path="2026/10/unread.jpg", uploaded_at=datetime(2026, 10, 25, tzinfo=UTC)
    )
    db_session.add_all([read, unread])
    db_session.flush()
    db_session.add(
        ReceiptReading(
            user_id=user.id,
            receipt_id=read.id,
            status=ReceiptReadingStatus.ready,
            merchant="Chipotle",
            total=Decimal("21.60"),
            line_items=[{"description": "Bowl", "amount": "12.50"}],
            adds_up=False,
            matches_transaction=True,
        )
    )
    db_session.flush()

    response = db_client.get(f"/api/transactions/{charge.id}/receipts", headers=auth_headers)
    assert response.status_code == 200
    first, second = response.json()
    assert first["id"] == str(read.id)
    assert first["reading"]["merchant"] == "Chipotle"
    assert first["reading"]["total"] == "21.60"
    assert first["reading"]["line_items"] == [{"description": "Bowl", "amount": "12.50"}]
    assert first["reading"]["adds_up"] is False
    assert "file_path" not in first
    assert second["reading"] is None


def test_receipts_list_is_owner_only(
    db_client: TestClient,
    db_session: Session,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(other_user)
    db_session.add(account)
    db_session.flush()
    theirs = make_transaction(other_user, account)
    db_session.add(theirs)
    db_session.flush()
    response = db_client.get(f"/api/transactions/{theirs.id}/receipts", headers=auth_headers)
    assert response.status_code == 404
