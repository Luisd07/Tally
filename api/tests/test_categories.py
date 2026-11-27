import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, User
from app.models.enums import CategoryKind
from tests.factories import make_account, make_transaction

CATEGORIES = "/api/categories"


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"name": "Travel - Airfare", "kind": "expense"}
    body.update(overrides)
    return body


def _category(user: User, **kwargs: object) -> Category:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Travel - Airfare",
        "kind": CategoryKind.expense,
    }
    defaults.update(kwargs)
    return Category(**defaults)


def test_list_returns_only_own_categories(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    mine = _category(user, name="Zeta")
    also_mine = _category(user, name="Alpha")
    theirs = _category(other, name="Theirs")
    db_session.add_all([mine, also_mine, theirs])
    db_session.flush()

    r = db_client.get(CATEGORIES, headers=auth_headers)

    assert r.status_code == 200
    assert [row["name"] for row in r.json()] == ["Alpha", "Zeta"]


def test_create_returns_201_with_the_created_category(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(
        CATEGORIES,
        json=_body(tax_line="travel", deductible_pct=50, monthly_budget="400.00"),
        headers=auth_headers,
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Travel - Airfare"
    assert body["kind"] == "expense"
    assert body["tax_line"] == "travel"
    assert body["deductible_pct"] == 50
    assert body["monthly_budget"] == "400.00"
    assert "user_id" not in body


def test_create_is_idempotent_on_a_repeated_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    category_id = str(uuid.uuid4())
    first = db_client.post(CATEGORIES, json=_body(id=category_id), headers=auth_headers)
    second = db_client.post(
        CATEGORIES,
        json=_body(id=category_id, name="A Totally Different Name"),
        headers=auth_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["name"] == "Travel - Airfare"
    assert len(db_session.scalars(select(Category)).all()) == 1


def test_create_with_another_users_id_is_a_conflict(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = _category(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.post(CATEGORIES, json=_body(id=str(theirs.id)), headers=auth_headers)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_duplicate_name_for_the_same_user_is_a_conflict(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    first = db_client.post(CATEGORIES, json=_body(), headers=auth_headers)
    assert first.status_code == 201

    r = db_client.post(CATEGORIES, json=_body(id=str(uuid.uuid4())), headers=auth_headers)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_duplicate_name_is_allowed_across_users(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    db_session.add(_category(other, name="Travel - Airfare"))
    db_session.flush()

    r = db_client.post(CATEGORIES, json=_body(), headers=auth_headers)

    assert r.status_code == 201, r.text


def test_patch_updates_only_given_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    category = _category(user, tax_line="travel", deductible_pct=100)
    db_session.add(category)
    db_session.flush()

    r = db_client.patch(
        f"{CATEGORIES}/{category.id}", json={"deductible_pct": 50}, headers=auth_headers
    )

    assert r.status_code == 200
    body = r.json()
    assert body["deductible_pct"] == 50
    assert body["tax_line"] == "travel"
    assert body["name"] == "Travel - Airfare"


def test_patch_to_a_duplicate_name_is_a_conflict(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    taken = _category(user, name="Groceries")
    mine = _category(user, name="Travel - Lodging")
    db_session.add_all([taken, mine])
    db_session.flush()

    r = db_client.patch(f"{CATEGORIES}/{mine.id}", json={"name": "Groceries"}, headers=auth_headers)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_patch_on_missing_id_is_404(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    r = db_client.patch(
        f"{CATEGORIES}/{uuid.uuid4()}", json={"deductible_pct": 10}, headers=auth_headers
    )
    assert r.status_code == 404


def test_patch_on_another_users_id_is_404(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    theirs = _category(other)
    db_session.add(theirs)
    db_session.flush()

    r = db_client.patch(
        f"{CATEGORIES}/{theirs.id}", json={"deductible_pct": 10}, headers=auth_headers
    )

    assert r.status_code == 404


def test_decimal_amounts_round_trip_exactly(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(CATEGORIES, json=_body(monthly_budget="-1234.56"), headers=auth_headers)
    assert r.status_code == 201, r.text
    assert r.json()["monthly_budget"] == "-1234.56"


VALIDATION_CASES = [
    {"deductible_pct": -1},
    {"deductible_pct": 101},
    {"name": "x" * 101},
    {"name": ""},
    {"tax_line": "x" * 51},
    {"kind": "transfer"},
]
VALIDATION_IDS = [
    "deductible-pct-negative",
    "deductible-pct-over-100",
    "name-too-long",
    "name-empty",
    "tax-line-too-long",
    "unknown-kind",
]


@pytest.mark.parametrize("overrides", VALIDATION_CASES, ids=VALIDATION_IDS)
def test_create_validation_rejects_bad_input(
    db_client: TestClient, user: User, auth_headers: dict[str, str], overrides: dict[str, Any]
) -> None:
    r = db_client.post(CATEGORIES, json=_body(**overrides), headers=auth_headers)
    assert r.status_code == 422


@pytest.mark.parametrize("overrides", VALIDATION_CASES, ids=VALIDATION_IDS)
def test_patch_validation_rejects_bad_input(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    overrides: dict[str, Any],
) -> None:
    category = _category(user)
    db_session.add(category)
    db_session.flush()

    r = db_client.patch(f"{CATEGORIES}/{category.id}", json=overrides, headers=auth_headers)

    assert r.status_code == 422


@pytest.mark.parametrize("field", ["name", "kind", "deductible_pct", "archived"])
def test_patch_null_on_required_field_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    category = _category(user)
    db_session.add(category)
    db_session.flush()

    r = db_client.patch(f"{CATEGORIES}/{category.id}", json={field: None}, headers=auth_headers)

    assert r.status_code == 422


@pytest.mark.parametrize("field", ["tax_line", "monthly_budget"])
def test_patch_null_on_nullable_field_clears_it(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    category = _category(user, tax_line="travel", monthly_budget="400.00")
    db_session.add(category)
    db_session.flush()

    r = db_client.patch(f"{CATEGORIES}/{category.id}", json={field: None}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()[field] is None


def test_create_rejects_unknown_fields(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(CATEGORIES, json=_body(user_id=str(uuid.uuid4())), headers=auth_headers)
    assert r.status_code == 422


def test_create_defaults_to_not_archived(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(CATEGORIES, json=_body(), headers=auth_headers)

    assert r.status_code == 201
    assert r.json()["archived"] is False


def test_patch_archives_and_unarchives(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    category = _category(user)
    db_session.add(category)
    db_session.flush()
    url = f"{CATEGORIES}/{category.id}"

    archived = db_client.patch(url, json={"archived": True}, headers=auth_headers)
    restored = db_client.patch(url, json={"archived": False}, headers=auth_headers)

    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert restored.status_code == 200
    assert restored.json()["archived"] is False


def test_list_includes_archived_with_the_flag(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    active = _category(user, name="Active")
    old = _category(user, name="Old", archived=True)
    db_session.add_all([active, old])
    db_session.flush()

    r = db_client.get(CATEGORIES, headers=auth_headers)

    assert [(c["name"], c["archived"]) for c in r.json()] == [("Active", False), ("Old", True)]


def test_archived_category_stays_on_its_transactions(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = _category(user)
    db_session.add_all([account, category])
    db_session.flush()
    transaction = make_transaction(user, account, category_id=category.id)
    db_session.add(transaction)
    db_session.flush()

    db_client.patch(f"{CATEGORIES}/{category.id}", json={"archived": True}, headers=auth_headers)
    r = db_client.get("/api/transactions", params={"month": "2026-10"}, headers=auth_headers)

    assert [t["category_id"] for t in r.json()["items"]] == [str(category.id)]
