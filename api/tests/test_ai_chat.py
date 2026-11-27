"""Ask about your money (#174): the tools, proposals (#223) and the chat loop.

The SDK client is replaced by a scripted fake, so no test reaches Anthropic.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.errors import NotFound
from app.main import create_app
from app.models import (
    Account,
    AIAction,
    AISuggestion,
    AIUsage,
    Category,
    ChatConversation,
    Transaction,
    User,
)
from app.models.enums import (
    AIActionSource,
    AIActionStatus,
    AIConfidence,
    AISuggestionStatus,
    Bucket,
    IncomeType,
)
from app.services import ai, ai_actions, ai_chat, ai_chat_tools
from tests.factories import (
    make_account,
    make_category,
    make_income_entry,
    make_rule,
    make_subscription,
    make_transaction,
)

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)


class Block:
    """A response content block with the two things the loop uses: attributes and model_dump."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)
        self._fields = fields

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return dict(self._fields)


def text(value: str) -> Block:
    return Block(type="text", text=value)


def tool(id: str, name: str, **arguments: Any) -> Block:
    return Block(type="tool_use", id=id, name=name, input=arguments)


class Script:
    """Answers each request with the next scripted (stop_reason, blocks), or raises it when
    it's an exception."""

    def __init__(self, *turns: tuple[str, list[Block]] | Exception) -> None:
        self.turns: list[Any] = list(turns)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        stop_reason, blocks = turn
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason=stop_reason,
            usage=SimpleNamespace(
                input_tokens=2000,
                output_tokens=150,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            content=blocks,
        )


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Script:
    holder = Script()
    client = SimpleNamespace(beta=SimpleNamespace(messages=holder))
    monkeypatch.setattr(ai, "make_client", lambda settings: client)
    return holder


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


@pytest.fixture
def gas(db_session: Session, user: User) -> Category:
    row = make_category(user, name="Gas")
    db_session.add(row)
    db_session.flush()
    return row


def add(db: Session, *rows: object) -> None:
    db.add_all(rows)
    db.flush()


# --- tools ---


def test_search_counts_every_match_and_shows_no_account_fields(
    db_session: Session, user: User, other_user: User, account: Account, gas: Category
) -> None:
    rows = [
        make_transaction(
            user,
            account,
            posted_date=date(2026, 9, d),
            description_raw=f"SHELL OIL {d}",
            amount=Decimal("-30.00"),
            category_id=gas.id,
        )
        for d in (3, 10, 17)
    ]
    october = make_transaction(
        user, account, posted_date=date(2026, 10, 1), description_raw="SHELL OIL"
    )
    other_account = make_account(other_user)
    add(db_session, *rows, october, other_account)
    add(db_session, make_transaction(other_user, other_account, description_raw="SHELL OIL"))

    result = ai_chat_tools.search_transactions(
        db_session,
        user.id,
        {
            "text": "shell",
            "start_month": "2026-09",
            "end_month": "2026-09",
            "category": "gas",
            "bucket": "",
        },
    )

    assert result["match_count"] == 3
    assert result["match_sum"] == "-90.00"
    assert [r["date"] for r in result["shown"]] == ["2026-09-17", "2026-09-10", "2026-09-03"]
    assert result["shown"][0]["category"] == "Gas"
    assert result["shown"][0]["category_id"] == str(gas.id)
    assert result["shown"][0]["id"] == str(rows[2].id)
    assert "Sapphire" not in json.dumps(result) and "4242" not in json.dumps(result)


def test_spending_totals_match_the_dashboards_rules(
    db_session: Session, user: User, account: Account, gas: Category
) -> None:
    add(
        db_session,
        make_transaction(
            user,
            account,
            posted_date=date(2026, 9, 3),
            amount=Decimal("-40.00"),
            category_id=gas.id,
        ),
        make_transaction(
            user, account, posted_date=date(2026, 9, 4), amount=Decimal("5.00"), category_id=gas.id
        ),
        make_transaction(
            user,
            account,
            posted_date=date(2026, 9, 5),
            amount=Decimal("-100.00"),
            bucket=Bucket.transfer,
        ),
        make_transaction(
            user, account, posted_date=date(2026, 9, 6), amount=Decimal("-9.00"), pending=True
        ),
        make_transaction(user, account, posted_date=date(2026, 8, 31), amount=Decimal("-7.00")),
    )
    result = ai_chat_tools.spending_totals(
        db_session,
        user.id,
        {"start_month": "2026-09", "end_month": "2026-09", "group_by": "category"},
    )
    assert result == {"total_spent": "35.00", "groups": [{"group": "Gas", "spent": "35.00"}]}
    by_month = ai_chat_tools.spending_totals(
        db_session, user.id, {"start_month": "2026-08", "end_month": "2026-09", "group_by": "month"}
    )
    assert by_month["groups"] == [
        {"group": "2026-08", "spent": "7.00"},
        {"group": "2026-09", "spent": "35.00"},
    ]


def test_income_and_subscriptions(db_session: Session, user: User) -> None:
    add(
        db_session,
        make_income_entry(
            user, date=date(2026, 9, 15), type=IncomeType.event_pay, received=Decimal("500.00")
        ),
        make_subscription(user, name="Netflix", amount=Decimal("-15.49")),
    )
    income = ai_chat_tools.income(
        db_session, user.id, {"start_month": "2026-09", "end_month": "2026-09"}
    )
    assert income["total_received"] == "500.00"
    subs = ai_chat_tools.subscriptions(db_session, user.id, {})
    assert [s["name"] for s in subs["subscriptions"]] == ["Netflix"]
    assert "account_id" not in subs["subscriptions"][0]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        (
            "spending_totals",
            {"start_month": "2026-13", "end_month": "2026-09", "group_by": "month"},
        ),
        (
            "spending_totals",
            {"start_month": "2026-09", "end_month": "2026-01", "group_by": "month"},
        ),
        (
            "spending_totals",
            {"start_month": "2020-01", "end_month": "2026-01", "group_by": "month"},
        ),
        (
            "search_transactions",
            {
                "text": "",
                "start_month": "",
                "end_month": "",
                "category": "Nope",
                "bucket": "",
            },
        ),
        ("tax_summary", {"year": 12}),
        ("delete_everything", {}),
    ],
)
def test_bad_arguments_come_back_as_errors(
    db_session: Session, settings: Settings, user: User, name: str, args: dict[str, Any]
) -> None:
    content, is_error = ai_chat_tools.run(db_session, settings, user.id, name, args)
    assert is_error
    assert "error" in json.loads(content)


def test_every_lookup_is_strict_and_proposals_are_checked_on_the_server() -> None:
    names = {t["name"] for t in ai_chat_tools.TOOLS}
    assert names == {
        "search_transactions",
        "inbox",
        "spending_totals",
        "income",
        "subscriptions",
        "categories",
        "rules",
        "tax_summary",
        "payoff_plan",
        "propose_changes",
    }
    assert all(t["input_schema"]["additionalProperties"] is False for t in ai_chat_tools.TOOLS)
    # Partial patches can't be strict (every property would be required); the server
    # checks a proposal in full instead.
    assert {t["name"] for t in ai_chat_tools.TOOLS if not t["strict"]} == {"propose_changes"}
    [propose] = [t for t in ai_chat_tools.TOOLS if t["name"] == "propose_changes"]
    kinds = {
        step["properties"]["kind"]["const"]
        for step in propose["input_schema"]["properties"]["steps"]["items"]["anyOf"]
    }
    assert kinds == {
        "categorize_transactions",
        "categorize_inbox_groups",
        "mark_reviewed",
        "create_category",
        "update_category",
        "create_rule",
        "create_subscription",
        "update_subscription",
    }


def test_search_and_subscriptions_carry_ids(
    db_session: Session, user: User, account: Account, gas: Category
) -> None:
    plain = make_transaction(user, account, description_raw="MYSTERY")
    sub = make_subscription(user, category_id=gas.id)
    bare = make_subscription(user, name="Gym")
    add(db_session, plain, sub, bare)

    found = ai_chat_tools.search_transactions(
        db_session,
        user.id,
        {"text": "", "start_month": "", "end_month": "", "category": "", "bucket": ""},
    )
    assert found["shown"][0]["id"] == str(plain.id)
    assert found["shown"][0]["category_id"] is None

    items = {
        i["name"]: i for i in ai_chat_tools.subscriptions(db_session, user.id, {})["subscriptions"]
    }
    assert items["Spotify"]["id"] == str(sub.id)
    assert items["Spotify"]["category_id"] == str(gas.id)
    assert items["Gym"]["category_id"] is None


def test_categories_tool_lists_only_your_own(
    db_session: Session, user: User, other_user: User, gas: Category
) -> None:
    budgeted = make_category(user, name="Dining", monthly_budget=Decimal("250.00"), archived=True)
    add(db_session, budgeted, make_category(other_user, name="Secret"))

    result = ai_chat_tools.categories(db_session, user.id, {})

    assert result["categories"] == [
        {
            "id": str(budgeted.id),
            "name": "Dining",
            "kind": "expense",
            "archived": True,
            "monthly_budget": "250.00",
        },
        {
            "id": str(gas.id),
            "name": "Gas",
            "kind": "expense",
            "archived": False,
            "monthly_budget": None,
        },
    ]
    assert "Secret" not in json.dumps(result)


def test_rules_tool_lists_only_your_own(
    db_session: Session, user: User, other_user: User, gas: Category
) -> None:
    mine = make_rule(
        user,
        pattern="SHELL",
        set_category_id=gas.id,
        set_bucket=Bucket.business,
        set_merchant=None,
    )
    add(db_session, mine, make_rule(other_user, pattern="THEIRS"))

    result = ai_chat_tools.rules(db_session, user.id, {})

    assert result["rules"] == [
        {
            "id": str(mine.id),
            "pattern": "SHELL",
            "match_field": "description",
            "match_type": "contains",
            "set_category_id": str(gas.id),
            "set_category": "Gas",
            "set_bucket": "business",
            "set_merchant": None,
            "active": True,
        }
    ]
    assert "THEIRS" not in json.dumps(result)


def test_describe_names_the_new_tools() -> None:
    assert ai_chat.describe(ai_chat.ToolCall("categories", {})) == "Your categories"
    assert ai_chat.describe(ai_chat.ToolCall("rules", {})) == "Your rules"
    assert ai_chat.describe(ai_chat.ToolCall("inbox", {})) == "Your Inbox"
    assert ai_chat.describe(ai_chat.ToolCall("propose_changes", {})) == "Suggested changes"


def _inbox_rows(user: User, account: Account) -> list[Transaction]:
    """120 unreviewed rows, newest first: 60 Shell, 30 Costco and 30 with no merchant."""
    rows = []
    for i in range(120):
        posted = date(2026, 10, 20) - timedelta(days=i)
        if i % 4 == 3:
            fields: dict[str, Any] = {"description_raw": f"SQ *COFFEE  {i:04d}\n 55512345678"}
        elif i % 2:
            fields = {"merchant": "Costco", "description_raw": f"COSTCO WHSE #{i:04d}"}
        else:
            fields = {"merchant": "Shell", "description_raw": f"SHELL OIL {i:05d}"}
        rows.append(make_transaction(user, account, posted_date=posted, **fields))
    return rows


def _ai_suggestion(
    user: User, row: Transaction, category: Category, bucket: Bucket = Bucket.personal
) -> AISuggestion:
    return AISuggestion(
        user_id=user.id,
        transaction_id=row.id,
        status=AISuggestionStatus.ready,
        category_id=category.id,
        bucket=bucket,
        merchant="Hertz",
        confidence=AIConfidence.high,
        model="claude-sonnet-5-5",
    )


def test_inbox_groups_rows_by_merchant_without_ids(
    db_session: Session, user: User, other_user: User, account: Account, gas: Category
) -> None:
    rows = _inbox_rows(user, account)
    reviewed = make_transaction(user, account, merchant="Shell", reviewed=True, category_id=gas.id)
    reviewed.bucket = Bucket.business
    other_account = make_account(other_user, name="Their Card", last4="9999")
    add(db_session, *rows, reviewed, other_account)
    add(db_session, make_transaction(other_user, other_account, merchant="Shell"))

    result = ai_chat_tools.inbox(db_session, user.id, NOW)

    assert result["total_rows"] == 120 and result["shown_rows"] == 120
    assert result["shown_groups"] == 3 and result["truncated"] is False
    assert result["left_out"] == {"pending": 0, "needs_resplit": 0, "unmatched_manual": 0}
    # Largest group first.
    assert [g["group"] for g in result["groups"]][0] == "Shell"
    groups = {g["group"]: g for g in result["groups"]}
    assert set(groups) == {"Shell", "Costco", "SQ COFFEE"}
    assert {name: g["count"] for name, g in groups.items()} == {
        "Shell": 60,
        "Costco": 30,
        "SQ COFFEE": 30,
    }
    shell = groups["Shell"]
    assert shell == {
        "group": "Shell",
        "count": 60,
        "total": "-740.40",
        "last_date": "2026-10-20",
        "sample_description": "SHELL OIL ####",
        # History: the last reviewed Shell row was Gas, business.
        "suggested_category_id": str(gas.id),
        "bucket_hint": "business",
    }
    assert groups["Costco"]["suggested_category_id"] is None
    assert groups["Costco"]["bucket_hint"] is None
    # One sample per group, whitespace collapsed and long digit runs masked.
    assert groups["SQ COFFEE"]["sample_description"] == "SQ *COFFEE #### ####"
    dumped = json.dumps(result)
    assert "Sapphire" not in dumped and "4242" not in dumped
    assert "Their Card" not in dumped and "9999" not in dumped
    assert not any(str(r.id) in dumped for r in [*rows, reviewed])


def test_inbox_carries_ai_suggestions_and_caps_at_500(
    db_session: Session, user: User, account: Account, gas: Category
) -> None:
    rows = [
        make_transaction(user, account, posted_date=date(2026, 1, 1) + timedelta(days=i % 200))
        for i in range(505)
    ]
    add(db_session, *rows)
    newest = max(rows, key=lambda r: (r.posted_date, r.id))
    add(db_session, _ai_suggestion(user, newest, gas, Bucket.reimbursable))

    result = ai_chat_tools.inbox(db_session, user.id, NOW)

    assert result["total_rows"] == 505 and result["shown_rows"] == 500
    assert result["truncated"] is True
    [group] = result["groups"]
    assert group["group"] == "HERTZ RENTAL CAR" and group["count"] == 500
    assert group["suggested_category_id"] == str(gas.id)
    assert group["bucket_hint"] == "reimbursable" and "mixed" not in group


def test_a_large_inbox_answer_stays_small(
    db_session: Session, user: User, account: Account, gas: Category
) -> None:
    rows = [
        make_transaction(
            user,
            account,
            merchant=f"Neighborhood Merchant Number {i % 220}",
            description_raw=f"POS PURCHASE NEIGHBORHOOD MERCHANT {i % 220} STORE 00{i:04d}",
            posted_date=date(2026, 10, 20) - timedelta(days=i % 90),
        )
        for i in range(300)
    ]
    history = [
        make_transaction(
            user,
            account,
            merchant=f"Neighborhood Merchant Number {i}",
            reviewed=True,
            category_id=gas.id,
        )
        for i in range(220)
    ]
    add(db_session, *rows, *history)

    result = ai_chat_tools.inbox(db_session, user.id, NOW)

    size = len(json.dumps(result))
    assert size < 40_000, size
    assert result["shown_groups"] == ai_chat_tools.MAX_INBOX_GROUPS and result["truncated"]
    counts = [g["count"] for g in result["groups"]]
    assert counts == sorted(counts, reverse=True) and counts[0] == 2
    dumped = json.dumps(result)
    assert not any(str(r.id) in dumped for r in [*rows, *history])


def test_inbox_leaves_out_rows_that_need_the_user(
    db_session: Session, settings: Settings, user: User, account: Account, gas: Category
) -> None:
    synced = make_account(user, name="Checking", simplefin_account_id="act-checking")
    add(db_session, synced)
    plain = make_transaction(user, account, merchant="Plain Co")
    pending = make_transaction(user, account, merchant="Pending Co", pending=True)
    resplit = make_transaction(user, account, merchant="Split Co")
    unmatched = make_transaction(
        user, synced, merchant="Manual Co", posted_date=(NOW - timedelta(days=9)).date()
    )
    add(db_session, plain, pending, resplit, unmatched)
    add(db_session, make_transaction(user, account, reviewed=True, parent_id=resplit.id))

    result = ai_chat_tools.inbox(db_session, user.id, NOW)

    assert result["total_rows"] == 4 and result["shown_rows"] == 1
    assert result["left_out"] == {"pending": 1, "needs_resplit": 1, "unmatched_manual": 1}
    assert [g["group"] for g in result["groups"]] == ["Plain Co"]
    assert result["truncated"] is False

    # Nor can a proposal reach them by name.
    content, is_error = ai_chat_tools.run(
        db_session,
        settings,
        user.id,
        "propose_changes",
        {
            "summary": "File them",
            "steps": [
                {
                    "kind": "categorize_inbox_groups",
                    "groups": ["Pending Co", "Split Co", "Manual Co"],
                    "patch": {"category_id": str(gas.id), "reviewed": True},
                }
            ],
        },
        None,
        NOW,
    )
    assert is_error
    error = json.loads(content)["error"]
    assert "'Pending Co'" in error and "'Split Co'" in error and "'Manual Co'" in error
    assert db_session.scalar(select(func.count()).select_from(AIAction)) == 0


def test_inbox_marks_groups_whose_suggestions_disagree(
    db_session: Session, user: User, account: Account, gas: Category
) -> None:
    travel = make_category(user)
    add(db_session, travel)
    uber = [make_transaction(user, account, description_raw=f"UBER TRIP {i}") for i in range(3)]
    lyft = [make_transaction(user, account, description_raw=f"LYFT RIDE {i}") for i in range(2)]
    add(db_session, *uber, *lyft)
    add(
        db_session,
        _ai_suggestion(user, uber[0], gas),
        _ai_suggestion(user, uber[1], travel),
        _ai_suggestion(user, lyft[0], travel),
        _ai_suggestion(user, lyft[1], travel),
    )

    groups = {g["group"]: g for g in ai_chat_tools.inbox(db_session, user.id, NOW)["groups"]}

    assert groups["UBER TRIP"]["mixed"] is True
    assert "mixed" not in groups["LYFT RIDE"]
    assert groups["LYFT RIDE"]["suggested_category_id"] == str(travel.id)


# --- proposals ---


def _categorize(ids: list[str], category_id: str) -> dict[str, Any]:
    return {"kind": "categorize_transactions", "ids": ids, "patch": {"category_id": category_id}}


def test_a_proposal_is_stored_for_the_chat_and_returned(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    gas: Category,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    row = make_transaction(user, account, description_raw="SHELL OIL")
    add(db_session, row)
    rule_id = str(uuid.uuid4())
    steps = [
        {
            "kind": "create_rule",
            "rule": {
                "id": rule_id,
                "priority": 100,
                "match_field": "description",
                "match_type": "contains",
                "pattern": "SHELL",
                "set_category_id": str(gas.id),
            },
        },
        _categorize([str(row.id)], str(gas.id)),
    ]
    script.turns = [
        ("tool_use", [tool("t1", "propose_changes", summary="File Shell under Gas", steps=steps)]),
        ("end_turn", [text("I suggested filing Shell under Gas; confirm it to apply.")]),
    ]

    response = ai_client.post(
        "/api/ai/chat",
        json={"message": "File Shell under Gas from now on", "actions_supported": True},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["looked_up"] == ["Suggested changes"]
    [shown] = body["actions"]
    assert shown["status"] == "proposed" and shown["summary"] == "File Shell under Gas"
    assert shown["effects"]["transactions"] == 1 and shown["effects"]["rules_created"] == 1
    # A patch lists only the fields it changes.
    assert shown["steps"][1]["patch"] == {"category_id": str(gas.id)}
    [action] = db_session.scalars(select(AIAction)).all()
    assert str(action.id) == shown["id"]
    assert str(action.conversation_id) == body["conversation_id"]
    assert action.source == AIActionSource.chat and action.status == AIActionStatus.proposed
    result = script.calls[1]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "t1" and not result["is_error"]
    assert json.loads(result["content"]) == {
        "action_id": shown["id"],
        "summary": "File Shell under Gas",
        "effects": shown["effects"],
        "status": "proposed",
    }
    # Nothing is applied until the user confirms.
    db_session.refresh(row)
    assert row.category_id is None
    conversation = db_session.get_one(ChatConversation, action.conversation_id)
    assert conversation.questions == 1 and len(conversation.messages) == 4


def test_the_whole_inbox_is_categorized_in_one_proposal_by_group_name(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    gas: Category,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    rows = _inbox_rows(user, account)
    history = make_transaction(user, account, merchant="Shell", reviewed=True, category_id=gas.id)
    history.bucket = Bucket.business
    pending = make_transaction(user, account, merchant="Shell", pending=True)
    add(db_session, *rows, history, pending)
    groceries = str(uuid.uuid4())
    steps = [
        {
            "kind": "create_category",
            "category": {"id": groceries, "name": "Groceries", "kind": "expense"},
        },
        {
            "kind": "categorize_inbox_groups",
            "groups": ["Shell"],
            "patch": {"category_id": str(gas.id), "bucket": "business", "reviewed": True},
        },
        {
            "kind": "categorize_inbox_groups",
            "groups": ["Costco", "SQ COFFEE"],
            "patch": {"category_id": groceries, "reviewed": True},
        },
    ]
    script.turns = [
        ("tool_use", [tool("t1", "inbox")]),
        ("tool_use", [tool("t2", "propose_changes", summary="File your Inbox", steps=steps)]),
        ("end_turn", [text("I suggested filing 120; 1 pending one is left for you.")]),
        ("end_turn", [text("Anything else?")]),
    ]

    response = ai_client.post(
        "/api/ai/chat",
        json={"message": "Categorize my whole Inbox", "actions_supported": True},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["looked_up"] == ["Your Inbox", "Suggested changes"]
    seen = json.loads(script.calls[1]["messages"][-1]["content"][0]["content"])
    assert seen["shown_rows"] == 120 and seen["left_out"]["pending"] == 1
    [shown] = body["actions"]
    assert shown["effects"]["transactions"] == 120
    [action] = db_session.scalars(select(AIAction)).all()
    assert action.status == AIActionStatus.proposed
    # Stored as ordinary steps with ids, so confirming and the app's cards work as before.
    _, shell, others = action.steps
    assert shell == {
        "kind": "categorize_transactions",
        "ids": [str(r.id) for r in rows if r.merchant == "Shell"],
        "patch": {"category_id": str(gas.id), "bucket": "business", "reviewed": True},
    }
    assert others["kind"] == "categorize_transactions"
    assert set(others["ids"]) == {str(r.id) for r in rows if r.merchant != "Shell"}
    assert others["patch"] == {"category_id": groceries, "reviewed": True}
    db_session.refresh(rows[0])
    assert rows[0].category_id is None and not rows[0].reviewed

    again = ai_client.post(
        "/api/ai/chat",
        json={"message": "Thanks", "actions_supported": True},
        headers=auth_headers,
    )
    assert again.status_code == 200
    assert len({json.dumps(call["system"]).encode() for call in script.calls}) == 1


def test_an_unknown_inbox_group_is_an_error_and_stores_nothing(
    db_session: Session, settings: Settings, user: User, account: Account, gas: Category
) -> None:
    add(db_session, *_inbox_rows(user, account))
    args = {
        "summary": "File them",
        "steps": [
            {
                "kind": "categorize_inbox_groups",
                "groups": ["Shell", "Nope Mart"],
                "patch": {"category_id": str(gas.id), "reviewed": True},
            }
        ],
    }

    content, is_error = ai_chat_tools.run(
        db_session, settings, user.id, "propose_changes", args, None, NOW
    )

    assert is_error
    error = json.loads(content)["error"]
    assert "'Nope Mart'" in error and "'Shell'" not in error
    assert db_session.scalar(select(func.count()).select_from(AIAction)) == 0


def test_budgets_are_proposed_as_category_updates(
    db_session: Session, settings: Settings, user: User, gas: Category
) -> None:
    args = {
        "summary": "Set your Gas budget",
        "steps": [
            {"kind": "update_category", "id": str(gas.id), "patch": {"monthly_budget": "190.00"}}
        ],
    }

    content, is_error = ai_chat_tools.run(
        db_session, settings, user.id, "propose_changes", args, None, NOW
    )

    assert not is_error
    result = json.loads(content)
    assert result["effects"]["categories_updated"] == 1
    [action] = db_session.scalars(select(AIAction)).all()
    assert action.steps == [
        {"kind": "update_category", "id": str(gas.id), "patch": {"monthly_budget": "190.00"}}
    ]
    db_session.refresh(gas)
    assert gas.monthly_budget is None


def test_a_follow_up_proposal_belongs_to_the_same_chat(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    gas: Category,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    row = make_transaction(user, account)
    add(db_session, row)
    script.turns = [
        ("end_turn", [text("Hi.")]),
        (
            "tool_use",
            [
                tool(
                    "t1",
                    "propose_changes",
                    summary="Review it",
                    steps=[{"kind": "mark_reviewed", "ids": [str(row.id)]}],
                )
            ],
        ),
        ("end_turn", [text("Suggested.")]),
    ]
    first = ai_client.post(
        "/api/ai/chat", json={"message": "Hi", "actions_supported": True}, headers=auth_headers
    ).json()
    assert first["actions"] == []
    second = ai_client.post(
        "/api/ai/chat",
        json={"message": "Mark it reviewed", "conversation_id": first["conversation_id"]},
        headers=auth_headers,
    ).json()
    assert second["conversation_id"] == first["conversation_id"]
    [action] = db_session.scalars(select(AIAction)).all()
    assert str(action.conversation_id) == first["conversation_id"]
    assert [a["id"] for a in second["actions"]] == [str(action.id)]


def test_an_invalid_proposal_is_an_error_result_and_stores_nothing(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    row = make_transaction(user, account)
    add(db_session, row)
    missing = uuid.uuid4()
    steps = [_categorize([str(row.id)], str(missing))]
    script.turns = [
        ("tool_use", [tool("t1", "propose_changes", summary="Gas", steps=steps)]),
        ("end_turn", [text("That category doesn't exist.")]),
    ]

    response = ai_client.post(
        "/api/ai/chat", json={"message": "File it", "actions_supported": True}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["actions"] == []
    result = script.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"]
    assert f"unknown category id {missing}" in json.loads(result["content"])["error"]
    assert db_session.scalars(select(AIAction)).all() == []


@pytest.mark.parametrize("problem", ["malformed", "foreign", "over_cap"])
def test_a_refused_proposal_names_the_problem_and_leaves_no_action(
    db_session: Session,
    settings: Settings,
    user: User,
    other_user: User,
    account: Account,
    problem: str,
) -> None:
    conversation = ChatConversation(user_id=user.id, title="Chat", messages=[])
    theirs = make_account(other_user)
    add(db_session, conversation, theirs)
    their_row = make_transaction(other_user, theirs)
    add(db_session, their_row)
    steps, expected = {
        "malformed": ([{"kind": "mark_reviewed"}], "steps.0.mark_reviewed.ids: Field required"),
        "foreign": (
            [{"kind": "mark_reviewed", "ids": [str(their_row.id)]}],
            f"unknown transaction id {their_row.id}",
        ),
        "over_cap": (
            [
                {"kind": "mark_reviewed", "ids": [str(uuid.uuid4()) for _ in range(500)]},
                {"kind": "mark_reviewed", "ids": [str(uuid.uuid4())]},
            ],
            "at most 500 distinct transactions",
        ),
    }[problem]

    content, is_error = ai_chat_tools.run(
        db_session,
        settings,
        user.id,
        "propose_changes",
        {"summary": "Tidy up", "steps": steps},
        conversation.id,
    )

    assert is_error
    assert expected in json.loads(content)["error"]
    assert db_session.scalars(select(AIAction)).all() == []


@pytest.mark.parametrize(
    "error", [NotFound("Transaction not found."), OperationalError("", {}, Exception())]
)
def test_a_proposal_that_fails_to_save_is_an_error_result(
    db_session: Session,
    settings: Settings,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fail(*args: Any) -> None:
        raise error

    monkeypatch.setattr(ai_actions, "propose_action", fail)
    content, is_error = ai_chat_tools.run(
        db_session, settings, user.id, "propose_changes", {"summary": "Tidy up", "steps": []}
    )
    assert is_error and json.loads(content)["error"]
    assert db_session.scalars(select(AIAction)).all() == []


# --- the loop ---


def test_answer_from_a_tool_call(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    gas: Category,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    add(
        db_session,
        make_transaction(
            user,
            account,
            posted_date=date(2026, 9, 3),
            amount=Decimal("-40.00"),
            category_id=gas.id,
        ),
    )
    script.turns = [
        (
            "tool_use",
            [
                text("Let me check."),
                tool(
                    "t1",
                    "spending_totals",
                    start_month="2026-09",
                    end_month="2026-09",
                    group_by="category",
                ),
            ],
        ),
        ("end_turn", [text("You spent $40.00 on gas in September 2026.")]),
    ]

    response = ai_client.post(
        "/api/ai/chat",
        json={"message": "How much did I spend on gas in September?"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "You spent $40.00 on gas in September 2026."
    assert body["looked_up"] == ["Spending by category, 2026-09"]
    second = script.calls[1]
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert json.loads(result["content"])["total_spent"] == "40.00"
    assert {t["name"] for t in second["tools"]} == {t["name"] for t in ai_chat.tools(False)}
    assert "Gas" in second["system"][0]["text"]
    # One usage row per request in the loop.
    assert db_session.scalar(select(func.count()).select_from(AIUsage)) == 2


def test_a_follow_up_carries_the_history(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], script: Script
) -> None:
    script.turns = [("end_turn", [text("First answer.")]), ("end_turn", [text("Second answer.")])]
    first = ai_client.post("/api/ai/chat", json={"message": "First?"}, headers=auth_headers).json()
    ai_client.post(
        "/api/ai/chat",
        json={"message": "And then?", "conversation_id": first["conversation_id"]},
        headers=auth_headers,
    )
    sent = script.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"][0]["text"].endswith("\nFirst?")
    assert sent[-1]["content"][0]["text"].endswith("\nAnd then?")
    # The system prompt never changes, so earlier reasoning stays valid.
    assert script.calls[0]["system"] == script.calls[1]["system"]


def test_someone_elses_conversation_id_starts_fresh(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    theirs = ChatConversation(
        user_id=other_user.id,
        title="Theirs",
        messages=[
            {"role": "user", "content": "secret question"},
            {"role": "assistant", "content": [{"type": "text", "text": "secret"}]},
        ],
    )
    add(db_session, theirs)
    script.turns = [("end_turn", [text("Hi.")])]
    response = ai_client.post(
        "/api/ai/chat",
        json={"message": "Hello", "conversation_id": str(theirs.id)},
        headers=auth_headers,
    )
    # Their id is taken, so a new one is made; nothing of theirs is sent.
    assert response.status_code == 200
    [only] = script.calls[0]["messages"]
    assert only["content"][0]["text"].endswith("\nHello")
    assert "secret" not in json.dumps(script.calls[0])


def test_rounds_run_out_then_it_answers_without_tools(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], script: Script
) -> None:
    script.turns = [
        ("tool_use", [tool(f"t{i}", "subscriptions")]) for i in range(ai_chat.MAX_ROUNDS)
    ]
    script.turns.append(("end_turn", [text("Here's what I found.")]))
    body = ai_client.post("/api/ai/chat", json={"message": "Bills?"}, headers=auth_headers).json()
    assert body["reply"] == "Here's what I found."
    assert script.calls[-1]["tool_choice"] == {"type": "none"}
    assert body["looked_up"] == ["Subscriptions and bills"]


def test_a_reply_cut_off_mid_tool_call_is_answered_and_the_next_question_joins_it(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    script.turns = [
        ("max_tokens", [text("Partial"), tool("t1", "subscriptions")]),
        ("end_turn", [text("Next answer.")]),
    ]
    body = ai_client.post("/api/ai/chat", json={"message": "Bills?"}, headers=auth_headers).json()
    assert body["reply"] == "Partial"
    saved = db_session.get(ChatConversation, body["conversation_id"])
    assert saved is not None
    assert [m["role"] for m in saved.messages] == ["user", "assistant", "user"]
    assert saved.messages[-1]["content"][0]["tool_use_id"] == "t1"

    ai_client.post(
        "/api/ai/chat",
        json={"message": "Again?", "conversation_id": body["conversation_id"]},
        headers=auth_headers,
    )
    sent = script.calls[1]["messages"]
    # Roles still alternate: the question joined the tool results' turn.
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert [b["type"] for b in sent[-1]["content"]] == ["tool_result", "text"]


def test_the_system_prompt_is_the_same_bytes_for_every_request(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    script.turns = [("end_turn", [text("One.")]), ("end_turn", [text("Two.")])]
    ai_client.post("/api/ai/chat", json={"message": "First chat"}, headers=auth_headers)
    add(db_session, make_category(user, name="Travel"))
    ai_client.post("/api/ai/chat", json={"message": "Another chat"}, headers=auth_headers)
    first, second = script.calls
    assert json.dumps(first["system"]).encode() == json.dumps(second["system"]).encode()
    assert first["system"][0]["text"] == ai_chat.INSTRUCTIONS


def test_the_date_and_categories_ride_with_each_question(
    ai_client: TestClient, user: User, gas: Category, auth_headers: dict[str, str], script: Script
) -> None:
    script.turns = [("end_turn", [text("Hi.")])]
    ai_client.post("/api/ai/chat", json={"message": "Hello"}, headers=auth_headers)
    [sent] = script.calls
    assert "Today:" not in sent["system"][0]["text"]
    assert sent["messages"][0]["content"][0]["text"].startswith("[Today: ")
    assert "Categories: Gas." in sent["messages"][0]["content"][0]["text"]
    assert sent["cache_control"] == {"type": "ephemeral"}


def test_no_chat_is_saved_when_ai_cant_answer(
    settings: Settings,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    capped = settings.model_copy(
        update={"anthropic_api_key": SecretStr("sk-ant-test"), "ai_monthly_budget_usd": Decimal(0)}
    )
    app = create_app(capped)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        response = client.post("/api/ai/chat", json={"message": "Hi"}, headers=auth_headers)
    assert response.status_code == 503
    assert db_session.scalars(select(ChatConversation)).all() == []


def test_usage_is_kept_when_a_later_step_fails(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    script: Script,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script.turns = [("tool_use", [tool("t1", "subscriptions")])]

    def boom(*args: Any) -> None:
        raise RuntimeError("tool failed")

    monkeypatch.setattr(ai_chat_tools, "run", boom)
    response = ai_client.post("/api/ai/chat", json={"message": "Bills?"}, headers=auth_headers)
    assert response.status_code == 500
    # The request's session is never committed after an error.
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(AIUsage)) == 1
    assert db_session.scalars(select(ChatConversation)).all() == []


def test_nul_characters_are_stripped(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    script.turns = [("end_turn", [text("He\x00llo.")])]
    response = ai_client.post("/api/ai/chat", json={"message": "a\x00b"}, headers=auth_headers)
    assert response.status_code == 200
    saved = db_session.get(ChatConversation, response.json()["conversation_id"])
    assert saved is not None
    assert saved.title == "ab"
    assert saved.messages[0]["content"][0]["text"].endswith("\nab")
    assert saved.messages[1]["content"] == [{"type": "text", "text": "Hello."}]


def test_without_ai_it_is_503(
    db_client: TestClient, user: User, auth_headers: dict[str, str], script: Script
) -> None:
    assert (
        db_client.post("/api/ai/chat", json={"message": "Hi"}, headers=auth_headers).status_code
        == 503
    )
    assert script.calls == []


def test_a_full_chat_is_409(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    full = ChatConversation(
        user_id=user.id,
        title="Full",
        messages=[],
        questions=ai_chat.MAX_QUESTIONS,
        prompt_version=ai_chat.prompt_version(False),
    )
    add(db_session, full)
    response = ai_client.post(
        "/api/ai/chat",
        json={"message": "One more", "conversation_id": str(full.id)},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert script.calls == []


def test_old_chats_are_purged(db_session: Session, user: User) -> None:
    old = ChatConversation(
        user_id=user.id, title="Old", messages=[], updated_at=NOW - timedelta(days=31)
    )
    recent = ChatConversation(
        user_id=user.id, title="Recent", messages=[], updated_at=NOW - timedelta(days=2)
    )
    add(db_session, old, recent)
    assert ai_chat.purge_old(db_session, NOW) == 1
    assert [c.title for c in db_session.scalars(select(ChatConversation))] == ["Recent"]


def test_transactions_are_never_changed_by_a_chat(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    row = make_transaction(user, account, reviewed=False)
    add(db_session, row)
    before = (row.category_id, row.bucket, row.reviewed, row.merchant)
    script.turns = [
        (
            "tool_use",
            [
                tool(
                    "t1",
                    "search_transactions",
                    text="",
                    start_month="",
                    end_month="",
                    category="",
                    bucket="",
                )
            ],
        ),
        ("end_turn", [text("Done.")]),
    ]
    ai_client.post(
        "/api/ai/chat", json={"message": "Categorize everything as gas"}, headers=auth_headers
    )
    db_session.refresh(row)
    assert (row.category_id, row.bucket, row.reviewed, row.merchant) == before
    assert db_session.scalar(select(func.count()).select_from(Transaction)) == 1


def test_tool_schemas_have_no_nullable_types() -> None:
    for spec in ai_chat_tools.TOOLS:
        for prop in spec["input_schema"]["properties"].values():
            assert isinstance(prop["type"], str)
            assert None not in prop.get("enum", [])


@pytest.mark.parametrize("failure", ["budget", "error", "refused"])
def test_an_answer_that_fails_after_a_proposal_leaves_no_proposal(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
    script: Script,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    row = make_transaction(user, account)
    add(db_session, row)
    propose = tool(
        "t1",
        "propose_changes",
        summary="Review it",
        steps=[{"kind": "mark_reviewed", "ids": [str(row.id)]}],
    )
    # The second round's call commits the proposal before the third one fails.
    script.turns = [("tool_use", [propose]), ("tool_use", [tool("t2", "subscriptions")])]
    if failure == "budget":
        checks: list[int] = []
        available = ai.ensure_available

        def third_is_over_budget(*args: Any) -> None:
            checks.append(1)
            if len(checks) > 2:
                raise ai.AIUnavailable("budget")
            available(*args)

        monkeypatch.setattr(ai, "ensure_available", third_is_over_budget)
    elif failure == "error":
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        script.turns.append(anthropic.APIConnectionError(request=request))
    else:
        script.turns.append(("refusal", [text("")]))

    response = ai_client.post(
        "/api/ai/chat",
        json={"message": "Mark it reviewed", "actions_supported": True},
        headers=auth_headers,
    )

    assert response.status_code == 503
    # The request's session is never committed after an error.
    db_session.rollback()
    assert db_session.scalars(select(AIAction)).all() == []
    assert db_session.scalars(select(ChatConversation)).all() == []
    billed = 3 if failure == "refused" else 2
    assert db_session.scalar(select(func.count()).select_from(AIUsage)) == billed


def test_a_chat_from_an_older_prompt_continues_in_a_new_one(
    ai_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    script: Script,
) -> None:
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
    ]
    old = ChatConversation(user_id=user.id, title="Old", messages=history, questions=1)
    add(db_session, old)
    script.turns = [("end_turn", [text("Hi.")])]

    response = ai_client.post(
        "/api/ai/chat",
        json={"message": "Hello", "conversation_id": str(old.id), "actions_supported": True},
        headers=auth_headers,
    )

    assert response.status_code == 200
    new_id = uuid.UUID(response.json()["conversation_id"])
    assert new_id != old.id
    [only] = script.calls[0]["messages"]
    assert only["content"][0]["text"].endswith("\nHello")
    new = db_session.get_one(ChatConversation, new_id)
    assert new.prompt_version == ai_chat.prompt_version(True) and new.actions_supported
    db_session.refresh(old)
    assert old.messages == history and old.questions == 1 and old.prompt_version is None


def test_only_apps_that_show_proposals_get_the_proposing_tool(
    ai_client: TestClient, user: User, auth_headers: dict[str, str], script: Script
) -> None:
    script.turns = [
        ("end_turn", [text("One.")]),
        ("end_turn", [text("Two.")]),
        ("end_turn", [text("Three.")]),
    ]
    ai_client.post("/api/ai/chat", json={"message": "Old app"}, headers=auth_headers)
    new_app = ai_client.post(
        "/api/ai/chat", json={"message": "New app", "actions_supported": True}, headers=auth_headers
    ).json()
    # A chat keeps the tools it started with, whatever a later request says.
    ai_client.post(
        "/api/ai/chat",
        json={"message": "Follow-up", "conversation_id": new_app["conversation_id"]},
        headers=auth_headers,
    )
    without, with_, follow_up = script.calls
    assert "propose_changes" not in {t["name"] for t in without["tools"]}
    assert "propose_changes" in {t["name"] for t in with_["tools"]}
    assert follow_up["tools"] == with_["tools"]
    assert json.dumps(without["system"]).encode() == json.dumps(with_["system"]).encode()
    assert ai_chat.prompt_version(False) != ai_chat.prompt_version(True)
