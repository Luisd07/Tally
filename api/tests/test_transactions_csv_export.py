"""Acceptance tests for #73: `GET /api/transactions?format=csv`.

The CSV path shares `TransactionListQuery`'s filters with the JSON list (#32), so
these tests focus on what's specific to the export -- column order and content,
name resolution, formula-injection escaping, streaming in batches, and the HTTP
envelope -- rather than re-proving every filter JSON already covers.
"""

import csv
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from app.models import Account, Category, Event, Transaction, User
from app.schemas.paging import encode_cursor
from app.schemas.transactions import TransactionListQuery
from app.services import exports
from app.services import transactions as service
from app.services.splits import has_split_children
from tests.factories import make_account, make_category, make_event, make_transaction


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _csv_rows(text: str) -> list[list[str]]:
    # Every response starts with a UTF-8 BOM (see `test_csv_starts_with_a_utf8_bom`);
    # strip it here so the rest of these tests can assert on plain header/cell text.
    return list(csv.reader(io.StringIO(text.removeprefix("﻿"))))


# --- shape: same rows as the JSON list, unpaginated ---------------------------------------


def test_csv_row_count_and_ids_match_the_json_list_for_the_same_filters(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    matching = [
        make_transaction(user, account, description_raw=f"MATCH {i}", reviewed=True)
        for i in range(3)
    ]
    other = make_transaction(user, account, description_raw="OTHER", reviewed=False)
    _persist(db_session, *matching, other)

    json_response = db_client.get(
        "/api/transactions", params={"reviewed": "true"}, headers=auth_headers
    )
    csv_response = db_client.get(
        "/api/transactions", params={"reviewed": "true", "format": "csv"}, headers=auth_headers
    )

    json_ids = {item["id"] for item in json_response.json()["items"]}
    rows = _csv_rows(csv_response.text)
    csv_ids = {row[0] for row in rows[1:]}

    assert json_ids == {str(t.id) for t in matching}
    assert csv_ids == json_ids
    assert len(rows) - 1 == len(matching)


def test_csv_returns_every_row_ignoring_limit_and_cursor(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rows = [make_transaction(user, account) for _ in range(5)]
    _persist(db_session, *rows)
    # A cursor that would, under JSON paging, exclude every one of these rows (all
    # share the same posted_date as the factory default and sort behind this id).
    cursor = encode_cursor(rows[0].posted_date, uuid.UUID(int=0))

    response = db_client.get(
        "/api/transactions",
        params={"format": "csv", "limit": "2", "cursor": cursor},
        headers=auth_headers,
    )

    assert response.status_code == 200
    csv_rows = _csv_rows(response.text)
    assert {row[0] for row in csv_rows[1:]} == {str(r.id) for r in rows}


# --- header and column content -------------------------------------------------------------


def test_csv_header_order(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    header = _csv_rows(response.text)[0]
    assert header == [
        "id",
        "posted_date",
        "transacted_at",
        "account",
        "amount",
        "description",
        "merchant",
        "category",
        "tax_line",
        "bucket",
        "source",
        "pending",
        "reviewed",
        "notes",
        "event",
        "parent_id",
        "is_split",
    ]


def test_csv_resolves_account_category_and_event_names(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user, name="Chase Checking")
    category = make_category(user, name="Travel - Airfare", tax_line="24a Travel")
    event = make_event(user, name="Daytona 200")
    _persist(db_session, account, category, event)
    transaction = make_transaction(
        user, account, category_id=category.id, event_id=event.id, merchant="Delta"
    )
    _persist(db_session, transaction)

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    row = _csv_rows(response.text)[1]
    (
        id_,
        posted_date,
        transacted_at,
        account_name,
        amount,
        description,
        merchant,
        category_name,
        tax_line,
        bucket,
        source,
        pending,
        reviewed,
        notes,
        event_name,
        parent_id,
        is_split,
    ) = row
    assert id_ == str(transaction.id)
    assert posted_date == transaction.posted_date.isoformat()
    assert transacted_at == ""
    assert account_name == "Chase Checking"
    assert amount == "-12.34"
    assert description == "HERTZ RENTAL CAR"
    assert merchant == "Delta"
    assert category_name == "Travel - Airfare"
    assert tax_line == "24a Travel"
    assert bucket == "personal"
    assert source == "manual"
    assert pending == "false"
    assert reviewed == "false"
    assert notes == ""
    assert event_name == "Daytona 200"
    assert parent_id == ""
    assert is_split == "false"


def test_csv_leaves_unset_names_blank(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, category_id=None, event_id=None)
    _persist(db_session, transaction)

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    row = _csv_rows(response.text)[1]
    assert row[7] == ""  # category
    assert row[8] == ""  # tax_line
    assert row[14] == ""  # event


# --- formula-injection escaping -------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_escapes_formula_injection_prefixes_on_text_cells(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    prefix: str,
) -> None:
    account = make_account(user, name=f"{prefix}account")
    category = make_category(user, name=f"{prefix}category", tax_line=f"{prefix}tax")
    event = make_event(user, name=f"{prefix}event")
    _persist(db_session, account, category, event)
    transaction = make_transaction(
        user,
        account,
        category_id=category.id,
        event_id=event.id,
        description_raw=f"{prefix}description",
        merchant=f"{prefix}merchant",
        notes=f"{prefix}notes",
    )
    _persist(db_session, transaction)

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    row = _csv_rows(response.text)[1]
    account_name, description, merchant, category_name = row[3], row[5], row[6], row[7]
    tax_line, notes, event_name = row[8], row[13], row[14]
    assert account_name == f"'{prefix}account"
    assert description == f"'{prefix}description"
    assert merchant == f"'{prefix}merchant"
    assert category_name == f"'{prefix}category"
    assert tax_line == f"'{prefix}tax"
    assert notes == f"'{prefix}notes"
    assert event_name == f"'{prefix}event"


def test_csv_amount_is_never_escaped_even_when_negative(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, amount=Decimal("-42.00"))
    _persist(db_session, transaction)

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    row = _csv_rows(response.text)[1]
    assert row[4] == "-42.00"
    assert Decimal(row[4]) == Decimal("-42.00")


# --- streaming across more than one yield_per batch -----------------------------------------


def test_csv_streams_correctly_across_more_than_one_batch(
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.transactions.EXPORT_BATCH_SIZE", 2)
    account = make_account(user)
    _persist(db_session, account)
    rows = [make_transaction(user, account, description_raw=f"ROW {i}") for i in range(5)]
    _persist(db_session, *rows)

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    csv_rows = _csv_rows(response.text)
    ids = [row[0] for row in csv_rows[1:]]
    assert sorted(ids) == sorted(str(r.id) for r in rows)
    assert len(ids) == len(set(ids))


def test_stream_export_rows_uses_a_server_side_cursor(
    engine: Engine, db_session: Session, user: User
) -> None:
    """`yield_per` has to be an execution option on the statement, not a
    `Result.yield_per()` call after `execute()`: only the former makes psycopg open a
    server-side (named) cursor, which is what actually keeps the whole result from
    shipping to the client before the first row is read.
    """
    account = make_account(user)
    _persist(db_session, account)
    _persist(db_session, make_transaction(user, account))

    seen_cursor_types: list[type] = []

    def _capture(conn: object, cursor: object, *args: object) -> None:
        seen_cursor_types.append(type(cursor))

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        rows = list(
            service.stream_export_rows(
                db_session, user.id, TransactionListQuery(), datetime.now(UTC)
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert rows
    assert any(issubclass(t, psycopg.ServerCursor) for t in seen_cursor_types)


# --- split parents and exclude_split_parents -------------------------------------------------


def test_csv_includes_split_parents_by_default_and_flags_is_split(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    child = make_transaction(user, account, amount=Decimal("-60.00"), parent_id=parent.id)
    _persist(db_session, child)

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    by_id = {row[0]: row for row in _csv_rows(response.text)[1:]}
    assert by_id[str(parent.id)][16] == "true"  # is_split
    assert by_id[str(child.id)][16] == "false"
    assert by_id[str(child.id)][15] == str(parent.id)  # parent_id


def test_csv_exclude_split_parents_drops_only_the_split_parent(
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
        "/api/transactions",
        params={"format": "csv", "exclude_split_parents": "true"},
        headers=auth_headers,
    )

    ids = {row[0] for row in _csv_rows(response.text)[1:]}
    assert ids == {str(child.id), str(unsplit.id)}


# --- scoping -----------------------------------------------------------------------------


def test_csv_excludes_another_users_transactions(
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

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    ids = {row[0] for row in _csv_rows(response.text)[1:]}
    assert ids == {str(mine.id)}


def test_csv_blanks_category_and_event_names_from_another_user(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    """The DB doesn't enforce same-user FKs (`app/services/transactions.py`), so a
    transaction can in principle point at another user's `category_id`/`event_id`.
    `export_select` scopes those joins' `ON` clause by `user_id`, so the row still
    appears (through the outer join) but with blank names instead of leaking the
    other user's category/event.
    """
    account = make_account(user)
    other_category = make_category(other_user)
    other_event = make_event(other_user)
    _persist(db_session, account, other_category, other_event)
    transaction = make_transaction(
        user, account, category_id=other_category.id, event_id=other_event.id
    )
    _persist(db_session, transaction)

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    row = _csv_rows(response.text)[1]
    assert row[0] == str(transaction.id)
    assert row[7] == ""  # category
    assert row[8] == ""  # tax_line
    assert row[14] == ""  # event


# --- writer internals ---------------------------------------------------------------------


def test_stream_transaction_csv_aligns_columns_by_label_not_position(
    db_session: Session, user: User
) -> None:
    """`stream_transaction_csv` has to read each row by its column's label, not its
    position: this builds the same joins as `export_select` but lists the columns in
    a different order, and the writer still has to produce the right value under
    each header.
    """
    account = make_account(user, name="Chase Checking")
    category = make_category(user, name="Travel - Airfare")
    _persist(db_session, account, category)
    transaction = make_transaction(
        user,
        account,
        category_id=category.id,
        merchant="Hertz",
        description_raw="Rental",
    )
    _persist(db_session, transaction)

    reordered = (
        select(
            Transaction.merchant.label("merchant"),
            Transaction.description_raw.label("description"),
            Account.name.label("account"),
            Category.name.label("category"),
            Category.tax_line.label("tax_line"),
            Transaction.id.label("id"),
            Transaction.posted_date.label("posted_date"),
            Transaction.transacted_at.label("transacted_at"),
            Transaction.amount.label("amount"),
            Transaction.bucket.label("bucket"),
            Transaction.source.label("source"),
            Transaction.pending.label("pending"),
            Transaction.reviewed.label("reviewed"),
            Transaction.notes.label("notes"),
            Event.name.label("event"),
            Transaction.parent_id.label("parent_id"),
            has_split_children().label("is_split"),
        )
        .select_from(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(Event, Event.id == Transaction.event_id)
        .where(Transaction.id == transaction.id)
    )
    row = db_session.execute(reordered).one()

    rows = _csv_rows("".join(exports.stream_transaction_csv([row])))
    by_header = dict(zip(rows[0], rows[1], strict=True))
    assert by_header["merchant"] == "Hertz"
    assert by_header["description"] == "Rental"
    assert by_header["account"] == "Chase Checking"
    assert by_header["category"] == "Travel - Airfare"
    assert by_header["id"] == str(transaction.id)


# --- HTTP envelope -------------------------------------------------------------------------


def test_csv_content_type_and_cache_control_headers(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"


def test_csv_starts_with_a_utf8_bom(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """Desktop Excel reads a BOM-less UTF-8 file as the system codepage and mangles
    non-ASCII merchant/account names; the BOM tells it the file is really UTF-8.
    """
    _persist(db_session, make_account(user))

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    assert response.content.startswith(b"\xef\xbb\xbf")


def test_csv_disposition_filename_uses_all_with_no_month_filter(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))

    response = db_client.get("/api/transactions", params={"format": "csv"}, headers=auth_headers)

    assert response.headers["content-disposition"] == (
        'attachment; filename="tally-transactions-all.csv"'
    )


def test_csv_disposition_filename_uses_the_month_filter(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))

    response = db_client.get(
        "/api/transactions", params={"format": "csv", "month": "2026-10"}, headers=auth_headers
    )

    assert response.headers["content-disposition"] == (
        'attachment; filename="tally-transactions-2026-10.csv"'
    )


# --- invalid format ------------------------------------------------------------------------


def test_list_rejects_an_invalid_format_value(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_account(user))

    response = db_client.get("/api/transactions", params={"format": "xml"}, headers=auth_headers)

    assert response.status_code == 422
