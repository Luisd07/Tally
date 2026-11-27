"""AI CSV mapping guess (#172). The SDK client is replaced, so no test reaches Anthropic."""

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.main import create_app
from app.models import AIUsage, User
from app.services import ai, ai_mapping

CHASE = [
    ["Details", "Posting Date", "Description", "Amount", "Type", "Balance", "Check or Slip #"],
    ["DEBIT", "10/22/2026", "CHIPOTLE 2291 ORLANDO FL", "-18.40", "DEBIT_CARD", "1234.56", ""],
    ["CREDIT", "10/21/2026", "PAYROLL ACME 123456789012", "2500.00", "ACH_CREDIT", "1252.96", ""],
    ["DEBIT", "10/20/2026", "PUBLIX #1234", "-52.10", "DEBIT_CARD", "-1247.04", ""],
    ["DEBIT", "10/19/2026", "SHELL OIL", "-40.00", "DEBIT_CARD", "-1195.00", ""],
    ["DEBIT", "10/18/2026", "NETFLIX", "-15.49", "ACH_DEBIT", "-1155.00", ""],
    ["DEBIT", "10/17/2026", "SIXTH ROW NEVER SENT", "-1.00", "DEBIT_CARD", "-1140.00", ""],
]

GOOD = {
    "has_header": True,
    "date_column": "Posting Date",
    "description_column": "Description",
    "amount_column": "Amount",
    "debit_column": "",
    "credit_column": "",
    "date_format": "%m/%d/%Y",
    "sign_convention": "negative_is_out",
}


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: Any = GOOD

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=400,
                output_tokens=60,
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


def guess(client: TestClient, headers: dict[str, str], records: list[list[str]]) -> Any:
    return client.post("/api/ai/guess-mapping", json={"records": records}, headers=headers)


def test_guess_is_returned_for_review(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    response = guess(ai_client, auth_headers, CHASE)
    assert response.status_code == 200
    assert response.json() == {
        "date_column": "Posting Date",
        "amount_column": "Amount",
        "debit_column": None,
        "credit_column": None,
        "description_column": "Description",
        "date_format": "%m/%d/%Y",
        "sign_convention": "negative_is_out",
        "has_header": True,
    }


def test_only_the_header_and_five_rows_go_with_every_number_but_dates_masked(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    guess(ai_client, auth_headers, CHASE)
    [sent] = fake.calls
    text = sent["messages"][0]["content"]
    assert "CHIPOTLE" in text and "NETFLIX" in text
    assert "SIXTH ROW NEVER SENT" not in text
    assert "10/22/2026" in text  # dates go as they are
    assert "-##.##" in text and "####.##" in text  # amounts and balances only as shapes
    for figure in ("18.40", "1234.56", "1252.96", "123456789012", "1234"):
        assert figure not in text
    assert "PAYROLL ACME ############" in text


@pytest.mark.parametrize(
    ("cell", "sent"),
    [
        ("10/22/2026", "10/22/2026"),
        ("2026-10-22", "2026-10-22"),
        ("2026-10-22 14:05", "2026-10-22 14:05"),
        ("20261022", "20261022"),
        ("22.10.26", "22.10.26"),
        ("10/22/2026 14:05:09", "10/22/2026 14:05:09"),
        ("2026-10-22T14:05", "2026-10-22T14:05"),
        ("123-45-6789", "###-##-####"),
        ("4111-11-1111", "####-##-####"),
        ("1234.56.7890", "####.##.####"),
        ("20481234", "########"),
        ("19283746", "########"),
        ("20123456", "########"),
        ("13/45/2026", "##/##/####"),
        ("-18.40", "-##.##"),
        ("1,234.56", "#,###.##"),
        ("123-456-789", "###-###-###"),
        ("4111 1111 1111 1111", "#### #### #### ####"),
        ("Member Number", "Member Number"),
    ],
)
def test_redact_masks_every_digit_except_dates(cell: str, sent: str) -> None:
    assert ai_mapping.redact([[cell]]) == [[sent]]


def test_account_like_refs_beside_dates_are_masked() -> None:
    rows = ai_mapping.redact(
        [["Date", "Ref"], ["10/22/2026", "20481234"], ["10/23/2026", "4111-11-1111"]]
    )
    assert rows == [["Date", "Ref"], ["10/22/2026", "########"], ["10/23/2026", "####-##-####"]]


def test_a_headerless_first_row_is_masked_too() -> None:
    rows = ai_mapping.redact([["10/22/2026", "-18.40", "1234.56", "CHIPOTLE 2291"]])
    assert rows == [["10/22/2026", "-##.##", "####.##", "CHIPOTLE ####"]]


def test_headerless_guess_is_checked_against_the_real_rows(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    records = [
        ["10/22/2026", "-18.40", "1234.56", "CHIPOTLE"],
        ["10/21/2026", "-2.00", "1252.96", "X"],
    ]
    fake.answer = {
        **GOOD,
        "has_header": False,
        "date_column": "0",
        "amount_column": "1",
        "description_column": "3",
    }
    body = guess(ai_client, auth_headers, records).json()
    assert (body["has_header"], body["amount_column"]) == (False, "1")


def test_debit_credit_guess(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    records = [
        ["Date", "Memo", "Debit", "Credit"],
        ["2026-10-22", "CHIPOTLE", "18.40", ""],
        ["2026-10-21", "PAYMENT", "", "200.00"],
    ]
    fake.answer = {
        **GOOD,
        "date_column": "Date",
        "description_column": "Memo",
        "amount_column": "",
        "debit_column": "Debit",
        "credit_column": "Credit",
        "date_format": "%Y-%m-%d",
    }
    body = guess(ai_client, auth_headers, records).json()
    assert (body["amount_column"], body["debit_column"], body["credit_column"]) == (
        None,
        "Debit",
        "Credit",
    )


@pytest.mark.parametrize(
    "answer",
    [
        {**GOOD, "date_column": "Not A Column"},  # not in the header
        {**GOOD, "date_format": "%d/%m"},  # no year
        {**GOOD, "date_format": "%Y-%m-%d"},  # doesn't read the sample's dates
        {**GOOD, "debit_column": "Amount"},  # amount and debit both set
        "not json",
    ],
)
def test_a_guess_that_doesnt_fit_the_file_is_422(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages, answer: Any
) -> None:
    fake.answer = answer
    response = guess(ai_client, auth_headers, CHASE)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_guess"


def test_header_only_file_is_422_without_a_call(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    response = guess(ai_client, auth_headers, [CHASE[0], ["", ""]])
    assert response.status_code == 422
    assert fake.calls == []


def test_without_ai_it_is_503(
    db_client: TestClient, user: User, auth_headers: dict[str, str], fake: FakeMessages
) -> None:
    assert guess(db_client, auth_headers, CHASE).status_code == 503
    assert fake.calls == []


def test_too_many_cells_in_a_row_is_422_without_a_call(
    ai_client: TestClient,
    user: User,
    auth_headers: dict[str, str],
    fake: FakeMessages,
    db_session: Session,
) -> None:
    records = [["x"] * 10_000 for _ in range(6)]
    assert guess(ai_client, auth_headers, records).status_code == 422
    assert fake.calls == []
    assert db_session.scalar(select(func.count()).where(AIUsage.user_id == user.id)) == 0


def test_a_sample_over_the_size_cap_is_422_without_a_call(
    ai_client: TestClient,
    user: User,
    auth_headers: dict[str, str],
    fake: FakeMessages,
    db_session: Session,
) -> None:
    records = [["x" * 500] * 100 for _ in range(6)]
    response = guess(ai_client, auth_headers, records)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_guess"
    assert fake.calls == []
    assert db_session.scalar(select(func.count()).where(AIUsage.user_id == user.id)) == 0
