"""AI-proposed actions (#222): checking and storing proposals, listing them, and
confirming, dismissing and undoing them."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.errors import NotFound, UnprocessableEntity
from app.models import (
    AIAction,
    AISuggestion,
    Category,
    ChatConversation,
    Rule,
    Subscription,
    Transaction,
    User,
)
from app.models.enums import AIActionSource, AIActionStatus, AISuggestionStatus
from app.schemas.ai_actions import STEPS, AIActionOut, CategorizeTransactionsStep, dump_steps
from app.services import ai_actions
from tests.factories import make_account, make_category, make_subscription, make_transaction


def _propose(db: Session, user: User, steps: list[Any], **kwargs: Any) -> AIAction:
    return ai_actions.propose_action(
        db, user.id, "Tidy up groceries", steps, AIActionSource.chat, **kwargs
    )


def _messages(exc: pytest.ExceptionInfo[UnprocessableEntity]) -> list[str]:
    return [detail["msg"] for detail in exc.value.details or []]


@pytest.fixture
def rows(db_session: Session, user: User) -> dict[str, Any]:
    account = make_account(user)
    category = make_category(user, name="Groceries")
    subscription = make_subscription(user)
    db_session.add_all([account, category, subscription])
    db_session.flush()
    txn = make_transaction(user, account)
    db_session.add(txn)
    db_session.flush()
    return {"account": account, "category": category, "subscription": subscription, "txn": txn}


def test_propose_each_kind(db_session: Session, user: User, rows: dict[str, Any]) -> None:
    category_id = rows["category"].id
    steps: list[dict[str, Any]] = [
        {
            "kind": "categorize_transactions",
            "ids": [str(rows["txn"].id)],
            "patch": {"category_id": str(category_id), "reviewed": True},
        },
        {"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]},
        {
            "kind": "create_category",
            "category": {"id": str(uuid.uuid4()), "name": "Coffee", "kind": "expense"},
        },
        {
            "kind": "create_rule",
            "rule": {
                "id": str(uuid.uuid4()),
                "priority": 10,
                "match_field": "description",
                "match_type": "contains",
                "pattern": "PUBLIX",
                "set_category_id": str(category_id),
            },
        },
        {
            "kind": "create_subscription",
            "subscription": {
                "id": str(uuid.uuid4()),
                "name": "Netflix",
                "amount": "-15.49",
                "cadence": "monthly",
                "account_id": str(rows["account"].id),
            },
        },
        {
            "kind": "update_subscription",
            "id": str(rows["subscription"].id),
            "patch": {"category_id": None},
        },
    ]

    action = _propose(db_session, user, steps)

    assert action.status == AIActionStatus.proposed
    assert action.source == AIActionSource.chat
    assert action.user_id == user.id
    assert [step["kind"] for step in action.steps] == [step["kind"] for step in steps]
    # Patches keep only what they set: the explicit null stays, nothing else appears.
    assert action.steps[0]["patch"] == {"category_id": str(category_id), "reviewed": True}
    assert action.steps[5]["patch"] == {"category_id": None}
    # The stored JSON parses back into the same steps.
    assert dump_steps(STEPS.validate_python(action.steps)) == action.steps


def test_accepts_step_models(db_session: Session, user: User, rows: dict[str, Any]) -> None:
    step = CategorizeTransactionsStep.model_validate(
        {
            "kind": "categorize_transactions",
            "ids": [rows["txn"].id],
            "patch": {"bucket": "business"},
        }
    )

    action = _propose(db_session, user, [step])

    assert action.steps == [
        {
            "kind": "categorize_transactions",
            "ids": [str(rows["txn"].id)],
            "patch": {"bucket": "business"},
        }
    ]


def test_conversation_is_kept(db_session: Session, user: User, rows: dict[str, Any]) -> None:
    conversation = ChatConversation(user_id=user.id, title="Groceries", messages=[])
    db_session.add(conversation)
    db_session.flush()

    action = _propose(
        db_session,
        user,
        [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}],
        conversation_id=conversation.id,
    )

    assert action.conversation_id == conversation.id


def test_someone_elses_conversation_is_not_found(
    db_session: Session, user: User, other_user: User, rows: dict[str, Any]
) -> None:
    conversation = ChatConversation(user_id=other_user.id, title="Theirs", messages=[])
    db_session.add(conversation)
    db_session.flush()

    with pytest.raises(NotFound):
        _propose(
            db_session,
            user,
            [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}],
            conversation_id=conversation.id,
        )


def test_foreign_and_unknown_ids_are_named(
    db_session: Session, user: User, other_user: User, rows: dict[str, Any]
) -> None:
    theirs = make_category(other_user)
    db_session.add(theirs)
    db_session.flush()
    unknown = uuid.uuid4()

    with pytest.raises(UnprocessableEntity) as exc:
        _propose(
            db_session,
            user,
            [
                {
                    "kind": "categorize_transactions",
                    "ids": [str(rows["txn"].id), str(unknown)],
                    "patch": {"category_id": str(theirs.id)},
                }
            ],
        )

    messages = _messages(exc)
    assert f"Value error, unknown transaction id {unknown}" in messages
    assert f"Value error, unknown category id {theirs.id}" in messages
    assert len(messages) == 2
    assert db_session.query(AIAction).count() == 0


def test_a_later_step_may_use_an_earlier_steps_category(
    db_session: Session, user: User, rows: dict[str, Any]
) -> None:
    new_id = str(uuid.uuid4())
    create = {
        "kind": "create_category",
        "category": {"id": new_id, "name": "Coffee", "kind": "expense"},
    }
    use = {
        "kind": "categorize_transactions",
        "ids": [str(rows["txn"].id)],
        "patch": {"category_id": new_id},
    }

    action = _propose(db_session, user, [create, use])
    assert action.steps[1]["patch"]["category_id"] == new_id

    # Not before it is created.
    with pytest.raises(UnprocessableEntity) as exc:
        _propose(db_session, user, [use, create])
    assert _messages(exc) == [f"Value error, unknown category id {new_id}"]


def test_a_new_id_must_be_unused(
    db_session: Session, user: User, other_user: User, rows: dict[str, Any]
) -> None:
    theirs = make_category(other_user)
    db_session.add(theirs)
    db_session.flush()

    with pytest.raises(UnprocessableEntity) as exc:
        _propose(
            db_session,
            user,
            [
                {
                    "kind": "create_category",
                    "category": {"id": str(theirs.id), "name": "Coffee", "kind": "expense"},
                }
            ],
        )
    assert _messages(exc) == [f"Value error, category id {theirs.id} already exists"]


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [{"kind": "mark_reviewed", "ids": []}],
        [{"kind": "delete_everything"}],
        [{"kind": "create_category", "category": {"name": "Coffee", "kind": "expense"}}],
        [{"kind": "update_subscription", "id": str(uuid.uuid4()), "patch": {}}],
        [{"kind": "update_category", "id": str(uuid.uuid4()), "patch": {}}],
        [{"kind": "update_category", "id": str(uuid.uuid4()), "patch": {"name": None}}],
        [{"kind": "mark_reviewed", "ids": [str(uuid.uuid4())]}] * 21,
    ],
    ids=[
        "none",
        "no-ids",
        "unknown-kind",
        "create-without-id",
        "empty-patch",
        "empty-category-patch",
        "null-category-name",
        "too-many",
    ],
)
def test_malformed_steps_are_rejected(db_session: Session, user: User, steps: list[Any]) -> None:
    with pytest.raises(UnprocessableEntity) as exc:
        _propose(db_session, user, steps)
    assert exc.value.details


def test_effects_count_what_the_steps_change(
    db_session: Session, user: User, rows: dict[str, Any]
) -> None:
    account = rows["account"]
    other = make_transaction(user, account)
    db_session.add(other)
    db_session.flush()
    txn_ids = [str(rows["txn"].id), str(other.id)]
    new_subscription = str(uuid.uuid4())
    steps, _, _ = _bundle(rows)
    steps += [
        {"kind": "mark_reviewed", "ids": txn_ids},
        {
            "kind": "create_subscription",
            "subscription": {
                "id": new_subscription,
                "name": "Netflix",
                "amount": "-15.49",
                "cadence": "monthly",
            },
        },
        # Updating a subscription the action itself creates isn't an update to show.
        {"kind": "update_subscription", "id": new_subscription, "patch": {"amount": "-17"}},
        {
            "kind": "update_subscription",
            "id": str(rows["subscription"].id),
            "patch": {"amount": "-9"},
        },
        # Likewise for a category the action creates.
        {"kind": "update_category", "id": steps[0]["category"]["id"], "patch": {"name": "Cafe"}},
        {
            "kind": "update_category",
            "id": str(rows["category"].id),
            "patch": {"monthly_budget": "400.00"},
        },
    ]

    action = _propose(db_session, user, steps)

    assert AIActionOut.model_validate(action).model_dump()["effects"] == {
        "transactions": 2,
        "categories_created": 1,
        "categories_updated": 1,
        "rules_created": 1,
        "subscriptions_created": 1,
        "subscriptions_updated": 1,
    }


def test_at_most_500_distinct_transactions_across_steps(
    db_session: Session, user: User, rows: dict[str, Any]
) -> None:
    ids = [str(uuid.uuid4()) for _ in range(501)]
    steps: list[Any] = [
        {"kind": "mark_reviewed", "ids": ids[:300]},
        {"kind": "mark_reviewed", "ids": ids[250:]},
    ]

    with pytest.raises(UnprocessableEntity) as exc:
        _propose(db_session, user, steps)

    assert _messages(exc) == ["Value error, an action may change at most 500 distinct transactions"]
    # The same rows in several steps count once.
    _propose(db_session, user, [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}] * 2)


def _stored(db: Session, user: User, status: AIActionStatus, created_at: datetime) -> AIAction:
    action = AIAction(
        user_id=user.id,
        summary=f"{status} action",
        status=status,
        source=AIActionSource.insight,
        steps=[{"kind": "mark_reviewed", "ids": [str(uuid.uuid4())]}],
        created_at=created_at,
    )
    db.add(action)
    db.flush()
    return action


def test_list_filters_by_status_newest_first(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    older = _stored(db_session, user, AIActionStatus.proposed, datetime(2026, 10, 1, tzinfo=UTC))
    newer = _stored(db_session, user, AIActionStatus.proposed, datetime(2026, 10, 2, tzinfo=UTC))
    applied = _stored(db_session, user, AIActionStatus.applied, datetime(2026, 10, 3, tzinfo=UTC))

    every = db_client.get("/api/ai/actions", headers=auth_headers)
    proposed = db_client.get("/api/ai/actions?status=proposed", headers=auth_headers)

    assert every.status_code == 200
    assert [item["id"] for item in every.json()] == [str(applied.id), str(newer.id), str(older.id)]
    assert [item["id"] for item in proposed.json()] == [str(newer.id), str(older.id)]
    assert set(every.json()[0]) == {
        "id",
        "summary",
        "status",
        "source",
        "steps",
        "created_at",
        "applied_at",
        "error",
        "effects",
    }


def test_get_shows_the_steps_as_stored(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    action = _propose(
        db_session,
        user,
        [
            {
                "kind": "update_subscription",
                "id": str(rows["subscription"].id),
                "patch": {"category_id": None},
            }
        ],
    )

    response = db_client.get(f"/api/ai/actions/{action.id}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "proposed"
    assert body["applied_at"] is None
    assert body["steps"] == action.steps


def test_someone_elses_action_is_not_found(
    db_client: TestClient, db_session: Session, other_user: User, auth_headers: dict[str, str]
) -> None:
    theirs = _stored(db_session, other_user, AIActionStatus.proposed, datetime.now(UTC))

    one = db_client.get(f"/api/ai/actions/{theirs.id}", headers=auth_headers)
    every = db_client.get("/api/ai/actions", headers=auth_headers)

    assert one.status_code == 404
    assert one.json()["error"]["code"] == "not_found"
    assert every.json() == []


def _bundle(rows: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    """create_category -> create_rule using it -> categorize the transaction with it."""
    category_id, rule_id = str(uuid.uuid4()), str(uuid.uuid4())
    steps: list[dict[str, Any]] = [
        {
            "kind": "create_category",
            "category": {"id": category_id, "name": "Coffee", "kind": "expense"},
        },
        {
            "kind": "create_rule",
            "rule": {
                "id": rule_id,
                "priority": 10,
                "match_field": "description",
                "match_type": "contains",
                "pattern": "STARBUCKS",
                "set_category_id": category_id,
            },
        },
        {
            "kind": "categorize_transactions",
            "ids": [str(rows["txn"].id)],
            "patch": {"category_id": category_id, "reviewed": True},
        },
    ]
    return steps, category_id, rule_id


def _count(db: Session, model: type[Category] | type[Rule] | type[Subscription], id: str) -> int:
    return db.scalar(select(func.count()).select_from(model).where(model.id == uuid.UUID(id))) or 0


def test_confirm_applies_every_step(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    steps, category_id, rule_id = _bundle(rows)
    action = _propose(db_session, user, steps)

    response = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "applied"
    assert body["applied_at"] is not None
    assert body["error"] is None
    txn = db_session.scalars(select(Transaction).where(Transaction.id == rows["txn"].id)).one()
    assert str(txn.category_id) == category_id
    assert txn.reviewed is True
    assert _count(db_session, Rule, rule_id) == 1

    db_session.refresh(action)
    assert action.undo is not None
    assert action.undo[0] == {"kind": "create_category", "id": category_id}
    assert action.undo[1] == {"kind": "create_rule", "id": rule_id}
    # A patch records the before-value of exactly the fields it set.
    assert action.undo[2] == {
        "kind": "categorize_transactions",
        "rows": [
            {
                "id": str(txn.id),
                "before": {"category_id": None, "reviewed": False},
                "updated_at": txn.updated_at.isoformat(),
            }
        ],
    }


def test_confirm_records_a_subscription_updates_before_values(
    db_session: Session, user: User, rows: dict[str, Any]
) -> None:
    subscription = rows["subscription"]
    action = _propose(
        db_session,
        user,
        [
            {"kind": "update_subscription", "id": str(subscription.id), "patch": {"amount": "-20"}},
            {"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]},
        ],
    )

    ai_actions.confirm_action(db_session, user.id, action.id, datetime.now(UTC))

    assert action.status == AIActionStatus.applied
    assert action.undo == [
        {
            "kind": "update_subscription",
            "id": str(subscription.id),
            "before": {"amount": "-11.99"},
            "updated_at": subscription.updated_at.isoformat(),
        },
        {
            "kind": "mark_reviewed",
            "rows": [
                {
                    "id": str(rows["txn"].id),
                    "before": {"reviewed": False},
                    "updated_at": rows["txn"].updated_at.isoformat(),
                }
            ],
        },
    ]


def test_an_unknown_category_to_update_is_named(db_session: Session, user: User) -> None:
    missing = uuid.uuid4()

    with pytest.raises(UnprocessableEntity) as exc:
        _propose(
            db_session,
            user,
            [{"kind": "update_category", "id": str(missing), "patch": {"archived": True}}],
        )
    assert _messages(exc) == [f"Value error, unknown category id {missing}"]


@pytest.mark.parametrize(
    "patch", [{"kind": "income"}, {"tax_line": "travel"}, {"deductible_pct": 50}]
)
def test_a_category_update_cannot_change_more_than_name_budget_and_archived(
    db_session: Session, user: User, rows: dict[str, Any], patch: dict[str, Any]
) -> None:
    steps = [{"kind": "update_category", "id": str(rows["category"].id), "patch": patch}]

    with pytest.raises(UnprocessableEntity) as exc:
        _propose(db_session, user, steps)
    assert exc.value.details


def test_a_budget_for_an_archived_category_is_refused_unless_it_is_unarchived(
    db_session: Session, user: User, rows: dict[str, Any]
) -> None:
    category = rows["category"]
    category.archived = True
    db_session.flush()
    budget = {"monthly_budget": "250.00"}

    with pytest.raises(UnprocessableEntity) as exc:
        _propose(
            db_session, user, [{"kind": "update_category", "id": str(category.id), "patch": budget}]
        )
    assert _messages(exc) == [
        f"Value error, category {category.id} is archived; unarchive it to change its budget"
    ]

    _propose(
        db_session,
        user,
        [
            {
                "kind": "update_category",
                "id": str(category.id),
                "patch": budget | {"archived": False},
            }
        ],
    )
    _propose(
        db_session,
        user,
        [{"kind": "update_category", "id": str(category.id), "patch": {"name": "Food"}}],
    )


def test_a_category_created_and_updated_in_one_bundle_is_applied_and_undone(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    steps, category_id, _ = _bundle(rows)
    steps.insert(
        1, {"kind": "update_category", "id": category_id, "patch": {"monthly_budget": "120.00"}}
    )

    action = _applied(db_client, db_session, user, auth_headers, steps)

    created = db_session.get(Category, uuid.UUID(category_id))
    assert created is not None
    assert created.monthly_budget == Decimal("120.00")

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 200
    assert _count(db_session, Category, category_id) == 0


@pytest.mark.parametrize(
    ("patch", "before"),
    [
        ({"monthly_budget": "250.00"}, {"monthly_budget": None}),
        ({"archived": True}, {"archived": False}),
    ],
    ids=["budget", "archive"],
)
def test_a_category_update_is_applied_and_undone(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
    patch: dict[str, Any],
    before: dict[str, Any],
) -> None:
    category = rows["category"]
    steps = [{"kind": "update_category", "id": str(category.id), "patch": patch}]

    action = _applied(db_client, db_session, user, auth_headers, steps)

    db_session.refresh(category)
    assert category.monthly_budget == (Decimal("250.00") if "monthly_budget" in patch else None)
    assert category.archived is ("archived" in patch)
    db_session.refresh(action)
    assert action.undo == [
        {
            "kind": "update_category",
            "id": str(category.id),
            "before": before,
            "updated_at": category.updated_at.isoformat(),
        }
    ]

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 200
    db_session.refresh(category)
    assert category.monthly_budget is None
    assert category.archived is False
    assert category.name == "Groceries"


def test_confirm_after_a_manual_category_edit_applies_nothing(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    category = rows["category"]
    steps: list[Any] = [
        {"kind": "update_category", "id": str(category.id), "patch": {"monthly_budget": "300"}}
    ]
    action = _propose(db_session, user, steps)
    patched = db_client.patch(
        f"/api/categories/{category.id}", json={"monthly_budget": "50.00"}, headers=auth_headers
    )
    assert patched.status_code == 200
    _edited_later(db_session, category)

    response = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)

    assert response.status_code == 409
    assert f"category {category.id}" in response.json()["error"]["message"]
    db_session.refresh(category)
    assert category.monthly_budget == Decimal("50.00")
    db_session.refresh(action)
    assert action.status == AIActionStatus.failed


def test_a_failing_step_applies_nothing(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    steps, category_id, rule_id = _bundle(rows)
    action = _propose(db_session, user, steps)
    db_session.delete(rows["txn"])
    db_session.flush()

    response = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)

    assert response.status_code == 409
    message = response.json()["error"]["message"]
    assert message.startswith("Step 3 (categorize_transactions) couldn't be applied")
    assert _count(db_session, Category, category_id) == 0
    assert _count(db_session, Rule, rule_id) == 0
    db_session.refresh(action)
    assert action.status == AIActionStatus.failed
    assert action.error == message
    assert action.undo is None
    assert action.applied_at is None


def test_confirm_after_a_manual_edit_applies_nothing(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    steps, category_id, rule_id = _bundle(rows)
    action = _propose(db_session, user, steps)
    txn = rows["txn"]
    patched = db_client.patch(
        f"/api/transactions/{txn.id}", json={"notes": "Latte"}, headers=auth_headers
    )
    assert patched.status_code == 200
    _edited_later(db_session, txn)

    response = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)

    assert response.status_code == 409
    message = response.json()["error"]["message"]
    assert "changed since this was suggested" in message
    assert f"transaction {txn.id}" in message
    assert _count(db_session, Category, category_id) == 0
    assert _count(db_session, Rule, rule_id) == 0
    assert txn.category_id is None
    db_session.refresh(action)
    assert action.status == AIActionStatus.failed
    assert action.error == message


def test_an_old_proposal_expires(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    action = _propose(db_session, user, [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}])
    action.created_at = datetime.now(UTC) - timedelta(days=8)
    db_session.flush()

    response = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)

    assert response.status_code == 409
    assert "this suggestion expired" in response.json()["error"]["message"].lower()
    assert rows["txn"].reviewed is False
    db_session.refresh(action)
    assert action.status == AIActionStatus.failed


def test_an_unexpected_database_error_fails_the_action(
    db_session: Session, user: User, rows: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("UPDATE transactions", {}, Exception("connection lost"))

    monkeypatch.setattr(ai_actions, "bulk_update_transactions", broken)
    action = _propose(db_session, user, [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}])

    ai_actions.confirm_action(db_session, user.id, action.id, datetime.now(UTC))

    assert action.status == AIActionStatus.failed
    assert action.error == "Step 1 (mark_reviewed) couldn't be applied: it couldn't be saved."
    assert action.undo is None
    assert rows["txn"].reviewed is False


def test_confirm_twice_is_a_conflict(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    action = _propose(db_session, user, [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}])

    first = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)
    second = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_dismissed_actions_cannot_be_confirmed(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    action = _propose(db_session, user, [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}])

    dismissed = db_client.post(f"/api/ai/actions/{action.id}/dismiss", headers=auth_headers)
    confirm = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)
    again = db_client.post(f"/api/ai/actions/{action.id}/dismiss", headers=auth_headers)

    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "dismissed"
    assert confirm.status_code == 409
    assert again.status_code == 409
    assert rows["txn"].reviewed is False


@pytest.mark.parametrize("verb", ["confirm", "dismiss", "undo"])
def test_someone_elses_action_cannot_be_confirmed_dismissed_or_undone(
    db_client: TestClient,
    db_session: Session,
    other_user: User,
    auth_headers: dict[str, str],
    verb: str,
) -> None:
    theirs = _stored(db_session, other_user, AIActionStatus.proposed, datetime.now(UTC))

    response = db_client.post(f"/api/ai/actions/{theirs.id}/{verb}", headers=auth_headers)

    assert response.status_code == 404
    db_session.refresh(theirs)
    assert theirs.status == AIActionStatus.proposed


def _applied(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    steps: list[dict[str, Any]],
) -> AIAction:
    action = _propose(db_session, user, steps)
    response = db_client.post(f"/api/ai/actions/{action.id}/confirm", headers=auth_headers)
    assert response.status_code == 200
    return action


def _edited_later(db: Session, row: Category | Transaction) -> None:
    """In production every request commits, so a later edit gets a later `now()`; here
    every request shares one transaction, so move the timestamp by hand."""
    row.updated_at += timedelta(seconds=1)
    db.flush()


def test_undo_reverts_every_step(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    steps, category_id, rule_id = _bundle(rows)
    subscription_id = str(uuid.uuid4())
    steps += [
        {
            "kind": "create_subscription",
            "subscription": {
                "id": subscription_id,
                "name": "Netflix",
                "amount": "-15.49",
                "cadence": "monthly",
            },
        },
        {
            "kind": "update_subscription",
            "id": str(rows["subscription"].id),
            "patch": {"amount": "-20", "category_id": str(rows["category"].id)},
        },
        {"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]},
    ]
    action = _applied(db_client, db_session, user, auth_headers, steps)

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "undone"
    txn = db_session.scalars(select(Transaction).where(Transaction.id == rows["txn"].id)).one()
    assert txn.category_id is None
    assert txn.reviewed is False
    subscription = rows["subscription"]
    db_session.refresh(subscription)
    assert str(subscription.amount) == "-11.99"
    assert subscription.category_id is None
    assert _count(db_session, Category, category_id) == 0
    assert _count(db_session, Rule, rule_id) == 0
    assert _count(db_session, Subscription, subscription_id) == 0
    db_session.refresh(action)
    assert action.status == AIActionStatus.undone


def test_undo_archives_a_created_category_still_in_use(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    steps, category_id, rule_id = _bundle(rows)
    action = _applied(db_client, db_session, user, auth_headers, steps)
    later = make_transaction(user, rows["account"], category_id=uuid.UUID(category_id))
    db_session.add(later)
    db_session.flush()

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 200
    category = db_session.get_one(Category, uuid.UUID(category_id))
    assert category.archived is True
    assert later.category_id == category.id
    assert rows["txn"].category_id is None
    assert _count(db_session, Rule, rule_id) == 0


def test_undo_keeps_a_created_category_an_ai_suggestion_uses(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    steps, category_id, _ = _bundle(rows)
    action = _applied(db_client, db_session, user, auth_headers, steps)
    suggestion = AISuggestion(
        user_id=user.id,
        transaction_id=rows["txn"].id,
        status=AISuggestionStatus.ready,
        category_id=uuid.UUID(category_id),
    )
    db_session.add(suggestion)
    db_session.flush()

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 200
    category = db_session.get_one(Category, uuid.UUID(category_id))
    assert category.archived is True
    db_session.refresh(suggestion)
    assert suggestion.category_id == category.id


def test_undo_deactivates_a_created_subscription_with_linked_transactions(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    subscription_id = str(uuid.uuid4())
    steps: list[dict[str, Any]] = [
        {
            "kind": "create_subscription",
            "subscription": {
                "id": subscription_id,
                "name": "Netflix",
                "amount": "-15.49",
                "cadence": "monthly",
            },
        }
    ]
    action = _applied(db_client, db_session, user, auth_headers, steps)
    txn = rows["txn"]
    txn.subscription_id = uuid.UUID(subscription_id)
    db_session.flush()

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 200
    subscription = db_session.get_one(Subscription, uuid.UUID(subscription_id))
    db_session.refresh(subscription)
    assert subscription.active is False
    db_session.refresh(txn)
    assert txn.subscription_id == subscription.id


def test_undo_after_a_manual_category_edit_changes_nothing(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    category = rows["category"]
    steps = [
        {"kind": "update_category", "id": str(category.id), "patch": {"monthly_budget": "300"}}
    ]
    action = _applied(db_client, db_session, user, auth_headers, steps)
    patched = db_client.patch(
        f"/api/categories/{category.id}", json={"name": "Food"}, headers=auth_headers
    )
    assert patched.status_code == 200
    _edited_later(db_session, category)

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 409
    assert f"category {category.id} was changed since" in response.json()["error"]["message"]
    db_session.refresh(category)
    assert category.monthly_budget == Decimal("300.00")
    assert category.name == "Food"
    db_session.refresh(action)
    assert action.status == AIActionStatus.applied


@pytest.mark.parametrize("edit", ["transaction", "category", "rule"])
def test_undo_after_a_later_edit_changes_nothing(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
    edit: str,
) -> None:
    steps, category_id, rule_id = _bundle(rows)
    action = _applied(db_client, db_session, user, auth_headers, steps)
    txn = rows["txn"]
    match edit:
        case "transaction":
            patched = db_client.patch(
                f"/api/transactions/{txn.id}", json={"notes": "Latte"}, headers=auth_headers
            )
            assert patched.status_code == 200
            _edited_later(db_session, txn)
            reason = f"transaction {txn.id} was changed since"
        case "category":
            patched = db_client.patch(
                f"/api/categories/{category_id}", json={"name": "Cafes"}, headers=auth_headers
            )
            assert patched.status_code == 200
            _edited_later(db_session, db_session.get_one(Category, uuid.UUID(category_id)))
            reason = f"category {category_id} was changed since"
        case _:
            deleted = db_client.delete(f"/api/rules/{rule_id}", headers=auth_headers)
            assert deleted.status_code == 204
            reason = f"rule {rule_id} no longer exists"

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 409
    assert reason in response.json()["error"]["message"]
    assert str(txn.category_id) == category_id
    assert txn.reviewed is True
    assert _count(db_session, Category, category_id) == 1
    db_session.refresh(action)
    assert action.status == AIActionStatus.applied


def test_undo_twice_is_a_conflict(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    steps = [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}]
    action = _applied(db_client, db_session, user, auth_headers, steps)

    first = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)
    rows["txn"].reviewed = True
    db_session.flush()
    second = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    assert rows["txn"].reviewed is True


def test_only_an_applied_action_can_be_undone(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    rows: dict[str, Any],
) -> None:
    action = _propose(db_session, user, [{"kind": "mark_reviewed", "ids": [str(rows["txn"].id)]}])

    response = db_client.post(f"/api/ai/actions/{action.id}/undo", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["error"]["message"] == (
        "Only an applied action can be undone; this one is proposed."
    )
    assert rows["txn"].reviewed is False
