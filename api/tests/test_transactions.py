"""Acceptance tests for #32: manual transaction capture, listing and editing.

Account/category rows are built directly in these tests (not through `/api/accounts` or
`/api/categories`, which #31 owns).
"""

import threading
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.models import Account, Receipt, Transaction, User
from app.models.enums import AccountKind, Bucket, Source
from app.schemas.transactions import TransactionCreate, TransactionSplit, TransactionSplitPart
from app.services import transactions as transactions_service
from app.services.transactions import create_transaction, split_transaction
from tests.factories import make_account, make_category, make_event, make_receipt, make_transaction
from tests.locks import wait_until_blocked_on_lock


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _body(account: Account, **kwargs: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "account_id": str(account.id),
        "posted_date": "2026-10-01",
        "amount": "-12.34",
        "description_raw": "HERTZ RENTAL CAR",
    }
    body.update(kwargs)
    return body


# --- POST /api/transactions ------------------------------------------------------------


def test_create_returns_201_with_source_manual_and_no_external_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)

    response = db_client.post("/api/transactions", json=_body(account), headers=auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["source"] == "manual"
    assert data["external_id"] is None
    assert data["pending"] is False


def test_repeating_the_id_returns_the_existing_row_ignoring_the_new_body(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    body = _body(account)

    first = db_client.post("/api/transactions", json=body, headers=auth_headers)
    second = db_client.post(
        "/api/transactions",
        json={**body, "amount": "-999.99", "description_raw": "DIFFERENT"},
        headers=auth_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()


@pytest.fixture
def committed_account(engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """A user and account committed to the real engine, visible to the two race sessions."""
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    with Session(engine) as setup:
        race_user = User(id=user_id, display_name="Race")
        setup.add(race_user)
        setup.flush()
        account_kwargs = {"id": account_id, "name": "Checking", "kind": AccountKind.checking}
        setup.add(make_account(race_user, **account_kwargs))
        setup.commit()
    try:
        yield user_id, account_id
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_concurrent_creates_with_the_same_id_insert_exactly_one_row(
    engine: Engine, committed_account: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Needs committed rows, so it runs outside the rolled-back test transaction."""
    user_id, account_id = committed_account
    tx_id = uuid.uuid4()
    body = TransactionCreate(
        id=tx_id,
        account_id=account_id,
        posted_date=date(2026, 10, 1),
        amount=Decimal("-12.34"),
        description_raw="HERTZ RENTAL CAR",
    )

    with Session(engine) as first, Session(engine) as second:
        first_row, first_created = create_transaction(first, user_id, body)
        assert first_created is True  # holds the insert lock, uncommitted

        second_pid = second.scalar(text("SELECT pg_backend_pid()"))
        assert second_pid is not None
        result: list[tuple[Transaction, bool]] = []
        racer = threading.Thread(
            target=lambda: result.append(create_transaction(second, user_id, body))
        )
        racer.start()
        wait_until_blocked_on_lock(engine, second_pid)
        first.commit()
        racer.join(timeout=10)

        second_row, second_created = result[0]
        assert second_created is False
        assert second_row.id == first_row.id == tx_id

    with Session(engine) as verify:
        rows = verify.scalars(select(Transaction).where(Transaction.id == tx_id)).all()
        assert len(rows) == 1


def test_create_with_id_owned_by_another_user_returns_409(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)
    theirs = make_transaction(other_user, other_account)
    _persist(db_session, theirs)
    account = make_account(user)
    _persist(db_session, account)

    response = db_client.post(
        "/api/transactions", json=_body(account, id=str(theirs.id)), headers=auth_headers
    )

    assert response.status_code == 409


def test_create_with_another_users_account_id_returns_404(
    db_client: TestClient,
    db_session: Session,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)

    response = db_client.post("/api/transactions", json=_body(other_account), headers=auth_headers)

    assert response.status_code == 404


def test_create_with_another_users_category_id_returns_404(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_category = make_category(other_user)
    account = make_account(user)
    _persist(db_session, other_category, account)

    response = db_client.post(
        "/api/transactions",
        json=_body(account, category_id=str(other_category.id)),
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_create_without_category_defaults_unreviewed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)

    response = db_client.post("/api/transactions", json=_body(account), headers=auth_headers)

    assert response.json()["reviewed"] is False


def test_create_with_category_defaults_reviewed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)

    response = db_client.post(
        "/api/transactions",
        json=_body(account, category_id=str(category.id)),
        headers=auth_headers,
    )

    assert response.json()["reviewed"] is True


def test_amount_round_trips_exactly_as_decimal(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)

    response = db_client.post(
        "/api/transactions", json=_body(account, amount="-1234567890.12"), headers=auth_headers
    )

    assert Decimal(response.json()["amount"]) == Decimal("-1234567890.12")


@pytest.mark.parametrize(
    ("field", "value"),
    [("description_raw", "x" * 501), ("merchant", "x" * 201), ("notes", "x" * 2001)],
)
def test_overlength_strings_are_rejected(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    field: str,
    value: str,
) -> None:
    account = make_account(user)
    _persist(db_session, account)

    response = db_client.post(
        "/api/transactions", json=_body(account, **{field: value}), headers=auth_headers
    )

    assert response.status_code == 422


def test_create_rejects_unknown_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)

    response = db_client.post(
        "/api/transactions", json=_body(account, event_id=str(uuid.uuid4())), headers=auth_headers
    )

    assert response.status_code == 422


# --- GET /api/transactions --------------------------------------------------------------


def test_list_filters_by_month(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    september = make_transaction(user, account, posted_date=date(2026, 9, 30))
    october = make_transaction(user, account, posted_date=date(2026, 10, 1))
    _persist(db_session, september, october)

    response = db_client.get("/api/transactions", params={"month": "2026-10"}, headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(october.id)}
    assert str(september.id) not in ids


def test_list_filters_by_month_across_the_december_rollover(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    december = make_transaction(user, account, posted_date=date(2026, 12, 31))
    january = make_transaction(user, account, posted_date=date(2027, 1, 1))
    _persist(db_session, december, january)

    response = db_client.get("/api/transactions", params={"month": "2026-12"}, headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(december.id)}
    assert str(january.id) not in ids


def test_list_rejects_a_badly_formatted_month(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))
    response = db_client.get("/api/transactions", params={"month": "2026-13"}, headers=auth_headers)

    assert response.status_code == 422


@pytest.mark.parametrize("month", ["0000-01", "9999-12"])
def test_list_rejects_a_month_year_outside_dates_range(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], month: str
) -> None:
    _persist(db_session, make_account(user))
    response = db_client.get("/api/transactions", params={"month": month}, headers=auth_headers)

    assert response.status_code == 422


def test_list_filters_by_account_bucket_category_and_reviewed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account_a = make_account(user, name="A")
    account_b = make_account(user, name="B")
    category = make_category(user)
    _persist(db_session, account_a, account_b, category)
    matching = make_transaction(
        user, account_a, bucket=Bucket.business, category_id=category.id, reviewed=True
    )
    other_account = make_transaction(user, account_b, bucket=Bucket.business, reviewed=True)
    other_bucket = make_transaction(user, account_a, bucket=Bucket.personal, reviewed=True)
    other_reviewed = make_transaction(
        user, account_a, bucket=Bucket.business, category_id=category.id, reviewed=False
    )
    _persist(db_session, matching, other_account, other_bucket, other_reviewed)

    response = db_client.get(
        "/api/transactions",
        params={
            "account_id": str(account_a.id),
            "bucket": "business",
            "category_id": str(category.id),
            "reviewed": "true",
        },
        headers=auth_headers,
    )

    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [str(matching.id)]


def test_list_filters_by_pending(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    settled = make_transaction(user, account, pending=False)
    still_pending = make_transaction(user, account, pending=True)
    _persist(db_session, settled, still_pending)

    response = db_client.get("/api/transactions", params={"pending": "false"}, headers=auth_headers)

    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [str(settled.id)]


def test_list_filters_by_event_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    event = make_event(user)
    _persist(db_session, account, event)
    at_the_event = make_transaction(user, account, event_id=event.id)
    not_at_the_event = make_transaction(user, account, event_id=None)
    _persist(db_session, at_the_event, not_at_the_event)

    response = db_client.get(
        "/api/transactions", params={"event_id": str(event.id)}, headers=auth_headers
    )

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(at_the_event.id)}


def test_list_excludes_another_users_transactions(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    other_account = make_account(other_user)
    _persist(db_session, account, other_account)
    mine = make_transaction(user, account)
    theirs = make_transaction(other_user, other_account)
    _persist(db_session, mine, theirs)

    response = db_client.get("/api/transactions", headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(mine.id)}


def test_list_search_matches_description_merchant_and_notes_case_insensitively(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    by_description = make_transaction(user, account, description_raw="Hertz Rental Car")
    by_merchant = make_transaction(
        user, account, description_raw="x", merchant="Enterprise Rental Car"
    )
    by_notes = make_transaction(user, account, description_raw="x", notes="rental deposit")
    unmatched = make_transaction(user, account, description_raw="Grocery Store")
    _persist(db_session, by_description, by_merchant, by_notes, unmatched)

    response = db_client.get("/api/transactions", params={"q": "RENTAL"}, headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(by_description.id), str(by_merchant.id), str(by_notes.id)}


def test_list_search_escapes_sql_wildcards(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    literal = make_transaction(user, account, description_raw="50% off coupon")
    not_literal = make_transaction(user, account, description_raw="50X off coupon")
    _persist(db_session, literal, not_literal)

    response = db_client.get("/api/transactions", params={"q": "50%"}, headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(literal.id)}


def test_list_search_rejects_an_overlong_query(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = db_client.get("/api/transactions", params={"q": "a" * 201}, headers=auth_headers)

    assert response.status_code == 422


def test_list_paging_returns_every_row_exactly_once_with_ties_on_posted_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    same_day = [make_transaction(user, account, posted_date=date(2026, 10, 1)) for _ in range(5)]
    _persist(db_session, *same_day)
    expected_ids = {str(row.id) for row in same_day}

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        response = db_client.get("/api/transactions", params=params, headers=auth_headers)
        page = response.json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert sorted(seen) == sorted(expected_ids)
    assert len(seen) == len(set(seen))


def test_list_rejects_an_invalid_cursor(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))
    response = db_client.get(
        "/api/transactions", params={"cursor": "not-base64!!"}, headers=auth_headers
    )

    assert response.status_code == 422


def test_list_limit_validation(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))
    response = db_client.get("/api/transactions", params={"limit": "501"}, headers=auth_headers)

    assert response.status_code == 422


# --- PATCH /api/transactions/{id} --------------------------------------------------------


def test_patch_partial_update_only_touches_given_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, bucket=Bucket.personal, notes="original")
    _persist(db_session, transaction)

    response = db_client.patch(
        f"/api/transactions/{transaction.id}",
        json={"merchant": "Hertz"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["merchant"] == "Hertz"
    assert data["bucket"] == "personal"
    assert data["notes"] == "original"


def test_patch_clears_category_with_explicit_null(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    transaction = make_transaction(user, account, category_id=category.id)
    _persist(db_session, transaction)

    response = db_client.patch(
        f"/api/transactions/{transaction.id}",
        json={"category_id": None},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["category_id"] is None


@pytest.mark.parametrize("field", ["bucket", "reviewed"])
def test_patch_null_on_required_field_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    response = db_client.patch(
        f"/api/transactions/{transaction.id}", json={field: None}, headers=auth_headers
    )

    assert response.status_code == 422


def test_patch_rejects_unknown_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, amount=Decimal("-12.34"))
    _persist(db_session, transaction)

    response = db_client.patch(
        f"/api/transactions/{transaction.id}",
        json={"amount": "-999.99"},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_patch_category_id_must_be_the_users_own(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_category = make_category(other_user)
    account = make_account(user)
    _persist(db_session, other_category, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    response = db_client.patch(
        f"/api/transactions/{transaction.id}",
        json={"category_id": str(other_category.id)},
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_patch_on_another_users_row_404s_identically_to_missing(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)
    theirs = make_transaction(other_user, other_account)
    _persist(db_session, theirs)

    missing = db_client.patch(
        f"/api/transactions/{uuid.uuid4()}", json={"notes": "x"}, headers=auth_headers
    )
    someone_elses = db_client.patch(
        f"/api/transactions/{theirs.id}", json={"notes": "x"}, headers=auth_headers
    )

    assert missing.status_code == 404
    assert someone_elses.status_code == 404
    missing_body, someone_elses_body = missing.json(), someone_elses.json()
    del missing_body["error"]["request_id"]
    del someone_elses_body["error"]["request_id"]
    assert missing_body == someone_elses_body


def test_patch_maps_a_concurrent_delete_to_404_not_500(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MV-8: a row that existed when `update_transaction` loaded it but is deleted
    by someone else before its own `UPDATE` runs surfaces as `StaleDataError`, not
    a missing row -- `apply_patch` is monkeypatched to simulate exactly that race,
    since reproducing it for real needs two sessions and `app.services.matching`
    no longer deletes a matched manual row at all (the survivor keeps its id).
    """
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    def _raise_stale(*args: object, **kwargs: object) -> Transaction:
        raise StaleDataError("simulated concurrent delete")

    monkeypatch.setattr(transactions_service, "apply_patch", _raise_stale)

    response = db_client.patch(
        f"/api/transactions/{transaction.id}", json={"notes": "x"}, headers=auth_headers
    )

    assert response.status_code == 404


def test_patch_sets_and_clears_event_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    event = make_event(user)
    _persist(db_session, account, event)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    set_response = db_client.patch(
        f"/api/transactions/{transaction.id}",
        json={"event_id": str(event.id)},
        headers=auth_headers,
    )
    clear_response = db_client.patch(
        f"/api/transactions/{transaction.id}", json={"event_id": None}, headers=auth_headers
    )

    assert set_response.status_code == 200
    assert set_response.json()["event_id"] == str(event.id)
    assert clear_response.status_code == 200
    assert clear_response.json()["event_id"] is None


def test_patch_event_id_must_be_the_users_own(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_event = make_event(other_user)
    account = make_account(user)
    _persist(db_session, other_event, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    response = db_client.patch(
        f"/api/transactions/{transaction.id}",
        json={"event_id": str(other_event.id)},
        headers=auth_headers,
    )

    assert response.status_code == 404


# --- POST /api/transactions/bulk ---------------------------------------------------------


def _bulk_body(ids: list[uuid.UUID], **patch: object) -> dict[str, object]:
    return {"ids": [str(id) for id in ids], "patch": patch}


def test_bulk_updates_category_bucket_and_reviewed_for_every_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    rows = [
        make_transaction(user, account, bucket=Bucket.personal, reviewed=False) for _ in range(5)
    ]
    _persist(db_session, *rows)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body(
            [row.id for row in rows],
            category_id=str(category.id),
            bucket="business",
            reviewed=True,
        ),
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["updated"] == 5
    assert [item["id"] for item in data["items"]] == [str(row.id) for row in rows]
    for item in data["items"]:
        assert item["category_id"] == str(category.id)
        assert item["bucket"] == "business"
        assert item["reviewed"] is True


def test_bulk_with_one_foreign_id_returns_404_and_changes_nothing(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    other_account = make_account(other_user)
    _persist(db_session, account, other_account)
    mine_a = make_transaction(user, account, bucket=Bucket.personal)
    mine_b = make_transaction(user, account, bucket=Bucket.personal)
    theirs = make_transaction(other_user, other_account, bucket=Bucket.personal)
    _persist(db_session, mine_a, mine_b, theirs)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body([mine_a.id, mine_b.id, theirs.id], bucket="business"),
        headers=auth_headers,
    )

    assert response.status_code == 404
    db_session.expire_all()
    rows = db_session.scalars(
        select(Transaction).where(Transaction.id.in_([mine_a.id, mine_b.id]))
    ).all()
    assert {row.bucket for row in rows} == {Bucket.personal}


def test_bulk_rejects_a_duplicate_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body([transaction.id, transaction.id], bucket="business"),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_bulk_rejects_more_than_500_ids(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))
    ids = [uuid.uuid4() for _ in range(501)]

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body(ids, bucket="business"),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_bulk_rejects_an_empty_patch(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    response = db_client.post(
        "/api/transactions/bulk",
        json={"ids": [str(transaction.id)], "patch": {}},
        headers=auth_headers,
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["bucket", "reviewed"])
def test_bulk_rejects_null_on_a_required_field(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str], field: str
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body([transaction.id], **{field: None}),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_bulk_rejects_unknown_fields_in_the_patch(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, amount=Decimal("-12.34"))
    _persist(db_session, transaction)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body([transaction.id], amount="-999.99"),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_bulk_category_id_must_be_the_users_own(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_category = make_category(other_user)
    account = make_account(user)
    _persist(db_session, other_category, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body([transaction.id], category_id=str(other_category.id)),
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_bulk_clears_event_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    event = make_event(user)
    _persist(db_session, account, event)
    a = make_transaction(user, account, event_id=event.id)
    b = make_transaction(user, account, event_id=event.id)
    _persist(db_session, a, b)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body([a.id, b.id], event_id=None),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert all(item["event_id"] is None for item in response.json()["items"])


def test_bulk_updates_updated_at(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    transaction = make_transaction(user, account, bucket=Bucket.personal, updated_at=stale)
    _persist(db_session, transaction)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body([transaction.id], bucket="business"),
        headers=auth_headers,
    )

    assert response.status_code == 200
    new_updated_at = datetime.fromisoformat(response.json()["items"][0]["updated_at"])
    assert new_updated_at > stale


def test_bulk_route_is_not_captured_by_the_id_route(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account)
    _persist(db_session, transaction)

    response = db_client.post(
        "/api/transactions/bulk",
        json=_bulk_body([transaction.id], notes="bulk"),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["updated"] == 1


# --- POST /api/transactions/{id}/split ---------------------------------------------------


def _split_body(*parts: Mapping[str, object]) -> dict[str, object]:
    return {"parts": list(parts)}


def test_split_into_business_and_personal_sums_exactly(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user)
    _persist(db_session, account, category)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body(
            {"amount": "-60.00", "bucket": "business", "category_id": str(category.id)},
            {"amount": "-40.00", "bucket": "personal"},
        ),
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["parent"]["id"] == str(parent.id)
    assert data["parent"]["is_split"] is True
    children = {child["amount"]: child for child in data["children"]}
    assert set(children) == {"-60.00", "-40.00"}
    business = children["-60.00"]
    assert business["bucket"] == "business"
    assert business["category_id"] == str(category.id)
    for child in data["children"]:
        assert child["parent_id"] == str(parent.id)
        assert child["account_id"] == str(account.id)
        assert child["reviewed"] is True
        assert child["is_split"] is False


def test_split_rejects_a_sum_mismatch(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-39.99"}),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_split_rejects_a_sign_mismatch(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "60.00"}, {"amount": "-160.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_split_rejects_a_zero_part(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "0.00"}, {"amount": "-100.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 422


@pytest.mark.parametrize("count", [1, 21])
def test_split_rejects_too_few_or_too_many_parts(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    count: int,
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    parts = [{"amount": "-1.00"} for _ in range(count)]

    response = db_client.post(
        f"/api/transactions/{parent.id}/split", json=_split_body(*parts), headers=auth_headers
    )

    assert response.status_code == 422


def test_split_of_a_pending_transaction_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"), pending=True)
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_split_of_a_child_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    child = make_transaction(user, account, amount=Decimal("-60.00"), parent_id=parent.id)
    _persist(db_session, child)

    response = db_client.post(
        f"/api/transactions/{child.id}/split",
        json=_split_body({"amount": "-30.00"}, {"amount": "-30.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_resplitting_replaces_the_children(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)

    first = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )
    first_child_ids = {child["id"] for child in first.json()["children"]}

    second = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-25.00"}, {"amount": "-75.00"}),
        headers=auth_headers,
    )

    assert second.status_code == 200
    second_amounts = {child["amount"] for child in second.json()["children"]}
    assert second_amounts == {"-25.00", "-75.00"}
    second_child_ids = {child["id"] for child in second.json()["children"]}
    assert first_child_ids.isdisjoint(second_child_ids)

    db_session.expire_all()
    remaining = db_session.scalars(
        select(Transaction).where(Transaction.parent_id == parent.id)
    ).all()
    assert {str(row.id) for row in remaining} == second_child_ids


def test_resplitting_with_a_receipt_on_a_child_returns_409_and_changes_nothing(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)

    first = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )
    first_child_ids = {child["id"] for child in first.json()["children"]}
    child = db_session.get(Transaction, uuid.UUID(next(iter(first_child_ids))))
    assert child is not None
    receipt = make_receipt(user, child)
    _persist(db_session, receipt)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-25.00"}, {"amount": "-75.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 409
    db_session.expire_all()
    remaining = db_session.scalars(
        select(Transaction).where(Transaction.parent_id == parent.id)
    ).all()
    assert {str(row.id) for row in remaining} == first_child_ids
    assert db_session.get(Receipt, receipt.id) is not None


def test_split_another_users_transaction_returns_404(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)
    theirs = make_transaction(other_user, other_account, amount=Decimal("-100.00"))
    _persist(db_session, theirs)

    response = db_client.post(
        f"/api/transactions/{theirs.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_split_with_another_users_category_in_a_part_returns_404(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_category = make_category(other_user)
    account = make_account(user)
    _persist(db_session, other_category, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body(
            {"amount": "-60.00", "category_id": str(other_category.id)}, {"amount": "-40.00"}
        ),
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_split_keeps_the_parents_external_id_and_children_get_none(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(
        user, account, amount=Decimal("-100.00"), source=Source.simplefin, external_id="ext-1"
    )
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )

    data = response.json()
    assert data["parent"]["external_id"] == "ext-1"
    assert all(child["external_id"] is None for child in data["children"])


def test_list_shows_parent_id_and_is_split(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    child = make_transaction(user, account, amount=Decimal("-60.00"), parent_id=parent.id)
    _persist(db_session, child)

    response = db_client.get("/api/transactions", headers=auth_headers)

    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[str(parent.id)]["is_split"] is True
    assert by_id[str(parent.id)]["parent_id"] is None
    assert by_id[str(child.id)]["is_split"] is False
    assert by_id[str(child.id)]["parent_id"] == str(parent.id)


def test_list_exclude_split_parents_drops_only_the_split_parent(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    child = make_transaction(user, account, amount=Decimal("-60.00"), parent_id=parent.id)
    unsplit = make_transaction(user, account, amount=Decimal("-10.00"))
    _persist(db_session, child, unsplit)

    response = db_client.get(
        "/api/transactions", params={"exclude_split_parents": "true"}, headers=auth_headers
    )

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(child.id), str(unsplit.id)}


def test_split_marks_the_parent_reviewed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"), reviewed=False)
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["parent"]["reviewed"] is True


def test_split_a_positive_amount_transaction(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("100.00"))
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "60.00"}, {"amount": "40.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    amounts = {child["amount"] for child in response.json()["children"]}
    assert amounts == {"60.00", "40.00"}


def test_patch_on_a_split_parent_returns_is_split_true(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    split_response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )
    assert split_response.status_code == 200

    response = db_client.patch(
        f"/api/transactions/{parent.id}", json={"notes": "updated"}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["is_split"] is True


def test_split_parts_inherit_the_parents_category_bucket_and_event_when_omitted(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user)
    event = make_event(user)
    _persist(db_session, account, category, event)
    parent = make_transaction(
        user,
        account,
        amount=Decimal("-100.00"),
        category_id=category.id,
        bucket=Bucket.business,
        event_id=event.id,
    )
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )

    assert response.status_code == 200
    for child in response.json()["children"]:
        assert child["category_id"] == str(category.id)
        assert child["bucket"] == "business"
        assert child["event_id"] == str(event.id)


def test_split_part_can_override_the_inherited_category_bucket_and_event(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    parent_category = make_category(user, name="Parent Category")
    override_category = make_category(user, name="Override Category")
    parent_event = make_event(user)
    override_event = make_event(
        user, name="Other Event", start_date=date(2026, 5, 1), end_date=date(2026, 5, 2)
    )
    _persist(db_session, account, parent_category, override_category, parent_event, override_event)
    parent = make_transaction(
        user,
        account,
        amount=Decimal("-100.00"),
        category_id=parent_category.id,
        bucket=Bucket.business,
        event_id=parent_event.id,
    )
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body(
            {
                "amount": "-60.00",
                "category_id": str(override_category.id),
                "bucket": "personal",
                "event_id": str(override_event.id),
            },
            {"amount": "-40.00"},
        ),
        headers=auth_headers,
    )

    assert response.status_code == 200
    children = {child["amount"]: child for child in response.json()["children"]}
    overridden = children["-60.00"]
    assert overridden["category_id"] == str(override_category.id)
    assert overridden["bucket"] == "personal"
    assert overridden["event_id"] == str(override_event.id)
    inherited = children["-40.00"]
    assert inherited["category_id"] == str(parent_category.id)
    assert inherited["bucket"] == "business"
    assert inherited["event_id"] == str(parent_event.id)


def test_split_part_explicit_null_clears_the_inherited_category_and_event(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user)
    event = make_event(user)
    _persist(db_session, account, category, event)
    parent = make_transaction(
        user, account, amount=Decimal("-100.00"), category_id=category.id, event_id=event.id
    )
    _persist(db_session, parent)

    response = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body(
            {"amount": "-60.00", "category_id": None, "event_id": None}, {"amount": "-40.00"}
        ),
        headers=auth_headers,
    )

    assert response.status_code == 200
    children = {child["amount"]: child for child in response.json()["children"]}
    assert children["-60.00"]["category_id"] is None
    assert children["-60.00"]["event_id"] is None
    assert children["-40.00"]["category_id"] == str(category.id)
    assert children["-40.00"]["event_id"] == str(event.id)


def test_resplitting_does_not_touch_another_users_row_sharing_the_parent_id(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    other_account = make_account(other_user)
    _persist(db_session, account, other_account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    first_split = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )
    assert first_split.status_code == 200

    # The DB doesn't enforce same-user parent_id (api.md); simulate another user's row
    # that happens to point at this parent, to prove the re-split query is scoped by
    # user_id rather than trusting parent_id alone.
    theirs = make_transaction(
        other_user, other_account, amount=Decimal("-999.00"), parent_id=parent.id
    )
    _persist(db_session, theirs)

    second_split = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-25.00"}, {"amount": "-75.00"}),
        headers=auth_headers,
    )

    assert second_split.status_code == 200
    db_session.expire_all()
    assert db_session.get(Transaction, theirs.id) is not None


def test_resplitting_is_still_blocked_by_a_receipt_a_different_user_owns(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    """`receipts.transaction_id` is `ON DELETE RESTRICT` regardless of who owns the
    receipt row, so the 409 guard must not miss one just because `Receipt.user_id`
    differs from the split's caller -- that would trade a clean 409 for a raw
    `IntegrityError` once the delete actually ran.
    """
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    first_split = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-60.00"}, {"amount": "-40.00"}),
        headers=auth_headers,
    )
    assert first_split.status_code == 200
    child_id = uuid.UUID(first_split.json()["children"][0]["id"])
    child = db_session.get(Transaction, child_id)
    assert child is not None
    theirs_receipt = make_receipt(other_user, child)
    _persist(db_session, theirs_receipt)

    second_split = db_client.post(
        f"/api/transactions/{parent.id}/split",
        json=_split_body({"amount": "-25.00"}, {"amount": "-75.00"}),
        headers=auth_headers,
    )

    assert second_split.status_code == 409
    db_session.expire_all()
    assert db_session.get(Transaction, child_id) is not None
    assert db_session.get(Receipt, theirs_receipt.id) is not None


def test_concurrent_splits_of_the_same_parent_leave_exactly_one_set_of_children(
    engine: Engine, committed_account: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Needs committed rows, so it runs outside the rolled-back test transaction."""
    user_id, account_id = committed_account
    parent_id = uuid.uuid4()
    with Session(engine) as setup:
        setup.add(
            Transaction(
                id=parent_id,
                user_id=user_id,
                account_id=account_id,
                posted_date=date(2026, 10, 1),
                amount=Decimal("-100.00"),
                description_raw="HERTZ RENTAL CAR",
                source=Source.manual,
            )
        )
        setup.commit()

    first_body = TransactionSplit(
        parts=[
            TransactionSplitPart(amount=Decimal("-60.00")),
            TransactionSplitPart(amount=Decimal("-40.00")),
        ]
    )
    second_body = TransactionSplit(
        parts=[
            TransactionSplitPart(amount=Decimal("-25.00")),
            TransactionSplitPart(amount=Decimal("-75.00")),
        ]
    )

    with Session(engine) as first, Session(engine) as second:
        _, first_children = split_transaction(first, user_id, parent_id, first_body)
        assert len(first_children) == 2  # holds the parent's row lock, uncommitted

        second_pid = second.scalar(text("SELECT pg_backend_pid()"))
        assert second_pid is not None
        result: list[tuple[Transaction, list[Transaction]]] = []
        racer = threading.Thread(
            target=lambda: result.append(split_transaction(second, user_id, parent_id, second_body))
        )
        racer.start()
        wait_until_blocked_on_lock(engine, second_pid)
        first.commit()
        racer.join(timeout=10)
        second.commit()

        _, second_children = result[0]
        assert len(second_children) == 2

    with Session(engine) as verify:
        remaining = verify.scalars(
            select(Transaction).where(Transaction.parent_id == parent_id)
        ).all()
        assert len(remaining) == 2
        assert sum((row.amount for row in remaining), Decimal("0.00")) == Decimal("-100.00")
        assert {row.amount for row in remaining} == {Decimal("-25.00"), Decimal("-75.00")}
