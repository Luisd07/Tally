"""Acceptance tests for #71 (the missing-receipt list) and #70 (the tax summary) on
the Tax screen.
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.deps import get_app_settings
from app.models import Account, Category, Transaction, User
from app.models.enums import Bucket, IncomeType
from tests.factories import (
    make_account,
    make_category,
    make_income_entry,
    make_mileage_rate,
    make_mileage_trip,
    make_odometer_reading,
    make_receipt,
    make_transaction,
)

MISSING_RECEIPTS = "/api/tax/missing-receipts"
TAX_SUMMARY = "/api/tax/summary"


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _client_with_1099(
    app: FastAPI, db_session: Session, settings: Settings, on: bool
) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_app_settings] = lambda: settings.model_copy(
        update={"reimbursements_on_1099": on}
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def db_client_1099_on(
    app: FastAPI, db_session: Session, settings: Settings
) -> Iterator[TestClient]:
    """Same as `db_client`, but with `reimbursements_on_1099` explicitly on."""
    yield from _client_with_1099(app, db_session, settings, on=True)


@pytest.fixture
def db_client_1099_off(
    app: FastAPI, db_session: Session, settings: Settings
) -> Iterator[TestClient]:
    """Same as `db_client`, but with `reimbursements_on_1099` explicitly off."""
    yield from _client_with_1099(app, db_session, settings, on=False)


def test_a_business_transaction_without_a_receipt_is_listed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, bucket=Bucket.business)
    _persist(db_session, transaction)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(transaction.id)]
    assert body["count"] == 1


def test_once_it_has_a_receipt_it_isnt_listed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    transaction = make_transaction(user, account, bucket=Bucket.business)
    _persist(db_session, transaction)
    receipt = make_receipt(user, transaction)
    _persist(db_session, receipt)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["count"] == 0


def test_personal_and_transfer_buckets_are_excluded(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    personal = make_transaction(user, account, bucket=Bucket.personal)
    transfer = make_transaction(user, account, bucket=Bucket.transfer)
    income = make_transaction(user, account, bucket=Bucket.income, amount=Decimal("100.00"))
    _persist(db_session, personal, transfer, income)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert response.json()["items"] == []


def test_reimbursable_bucket_only_appears_with_the_bucket_filter(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    reimbursable = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, reimbursable)

    default_response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)
    filtered_response = db_client.get(
        MISSING_RECEIPTS,
        params={"year": 2026, "bucket": "reimbursable"},
        headers=auth_headers,
    )

    assert default_response.json()["items"] == []
    assert [item["id"] for item in filtered_response.json()["items"]] == [str(reimbursable.id)]


def test_bucket_filter_rejects_a_bucket_that_never_needs_a_receipt(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = db_client.get(
        MISSING_RECEIPTS, params={"year": 2026, "bucket": "personal"}, headers=auth_headers
    )

    assert response.status_code == 422


def test_pending_transactions_are_excluded(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    pending = make_transaction(user, account, bucket=Bucket.business, pending=True)
    _persist(db_session, pending)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert response.json()["items"] == []


def test_positive_amounts_are_excluded(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    refund = make_transaction(user, account, bucket=Bucket.business, amount=Decimal("12.34"))
    _persist(db_session, refund)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert response.json()["items"] == []


def test_a_split_parent_with_children_is_excluded_but_its_children_are_listed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, bucket=Bucket.business, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    child_a = make_transaction(
        user, account, bucket=Bucket.business, amount=Decimal("-60.00"), parent_id=parent.id
    )
    child_b = make_transaction(
        user, account, bucket=Bucket.business, amount=Decimal("-40.00"), parent_id=parent.id
    )
    _persist(db_session, child_a, child_b)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(child_a.id), str(child_b.id)}
    assert str(parent.id) not in ids


def test_a_split_child_whose_parent_has_a_receipt_is_not_listed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, bucket=Bucket.business, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    receipt = make_receipt(user, parent)
    child = make_transaction(
        user, account, bucket=Bucket.business, amount=Decimal("-60.00"), parent_id=parent.id
    )
    _persist(db_session, receipt, child)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert response.json()["items"] == []


def test_a_split_child_with_its_own_receipt_is_not_listed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, bucket=Bucket.business, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    child = make_transaction(
        user, account, bucket=Bucket.business, amount=Decimal("-60.00"), parent_id=parent.id
    )
    _persist(db_session, child)
    receipt = make_receipt(user, child)
    _persist(db_session, receipt)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert response.json()["items"] == []


def test_a_split_child_with_no_receipt_on_itself_or_its_parent_is_listed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, bucket=Bucket.business, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    child = make_transaction(
        user, account, bucket=Bucket.business, amount=Decimal("-60.00"), parent_id=parent.id
    )
    _persist(db_session, child)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert [item["id"] for item in response.json()["items"]] == [str(child.id)]


def test_a_split_parent_with_no_children_yet_is_listed(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    unsplit = make_transaction(user, account, bucket=Bucket.business)
    _persist(db_session, unsplit)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    assert [item["id"] for item in response.json()["items"]] == [str(unsplit.id)]


def test_year_filters_to_the_calendar_year(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    in_year = make_transaction(
        user, account, bucket=Bucket.business, posted_date=date(2026, 12, 31)
    )
    next_year = make_transaction(
        user, account, bucket=Bucket.business, posted_date=date(2027, 1, 1)
    )
    _persist(db_session, in_year, next_year)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(in_year.id)}


def test_an_invalid_cursor_is_422(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    response = db_client.get(
        MISSING_RECEIPTS, params={"year": 2026, "cursor": "not-base64!!"}, headers=auth_headers
    )

    assert response.status_code == 422


def test_year_out_of_range_is_422_not_500(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = db_client.get(MISSING_RECEIPTS, params={"year": 999999999}, headers=auth_headers)

    assert response.status_code == 422


def test_year_is_required(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    response = db_client.get(MISSING_RECEIPTS, headers=auth_headers)

    assert response.status_code == 422


def test_paging_returns_every_row_exactly_once_and_the_count_covers_every_page(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    rows = [
        make_transaction(user, account, bucket=Bucket.business, posted_date=date(2026, 10, 1))
        for _ in range(5)
    ]
    _persist(db_session, *rows)
    expected_ids = {str(row.id) for row in rows}

    seen: list[str] = []
    count = None
    cursor: str | None = None
    for _ in range(10):
        params = {"year": "2026", "limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        response = db_client.get(MISSING_RECEIPTS, params=params, headers=auth_headers)
        page = response.json()
        if count is None:
            count = page["count"]
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert sorted(seen) == sorted(expected_ids)
    assert len(seen) == len(set(seen))
    assert count == 5


def test_another_users_rows_are_excluded(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    other_account = make_account(other_user)
    _persist(db_session, account, other_account)
    mine = make_transaction(user, account, bucket=Bucket.business)
    theirs = make_transaction(other_user, other_account, bucket=Bucket.business)
    _persist(db_session, mine, theirs)

    response = db_client.get(MISSING_RECEIPTS, params={"year": 2026}, headers=auth_headers)

    body = response.json()
    ids = {item["id"] for item in body["items"]}
    assert ids == {str(mine.id)}
    assert body["count"] == 1


# --- #70: GET /api/tax/summary -----------------------------------------------------


def _line(body: dict[str, Any], tax_line: str) -> dict[str, Any]:
    return next(row for row in body["expenses_by_line"] if row["tax_line"] == tax_line)


def test_meals_at_50_percent_are_half_deductible(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Travel - Food", tax_line="travel_food", deductible_pct=50)
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=category.id, bucket=Bucket.business, amount=Decimal("-40.00")
        ),
    )

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    assert response.status_code == 200
    line = _line(response.json(), "travel_food")
    assert line == {
        "tax_line": "travel_food",
        "spent": "40.00",
        "deductible_pct": 50,
        "deductible": "20.00",
    }


def test_different_categories_on_one_line_apply_their_own_pct_then_sum(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    full = make_category(user, name="Office full", tax_line="office", deductible_pct=100)
    half = make_category(user, name="Office half", tax_line="office", deductible_pct=50)
    account = make_account(user)
    _persist(db_session, account, full, half)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=full.id, bucket=Bucket.business, amount=Decimal("-50.00")
        ),
        make_transaction(
            user, account, category_id=half.id, bucket=Bucket.business, amount=Decimal("-30.00")
        ),
    )

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    line = _line(response.json(), "office")
    # 50.00 at 100% (=50.00) plus 30.00 at 50% (=15.00), summed then quantized once:
    # 65.00 deductible on 80.00 spent, an 81% blended rate.
    assert line == {
        "tax_line": "office",
        "spent": "80.00",
        "deductible_pct": 81,
        "deductible": "65.00",
    }


def test_reimbursements_off_excludes_reimbursable_expenses_and_income(
    db_client_1099_off: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Travel - Airfare", tax_line="travel", deductible_pct=100)
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=category.id, bucket=Bucket.business, amount=Decimal("-20.00")
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.reimbursable,
            amount=Decimal("-100.00"),
        ),
        make_income_entry(
            user, type=IncomeType.event_pay, expected=None, received=Decimal("500.00")
        ),
        make_income_entry(
            user, type=IncomeType.reimbursement, expected=None, received=Decimal("100.00")
        ),
    )

    response = db_client_1099_off.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    body = response.json()
    assert body["reimbursements_included"] is False
    assert body["expenses_by_line"] == [
        {"tax_line": "travel", "spent": "20.00", "deductible_pct": 100, "deductible": "20.00"}
    ]
    assert body["gross_receipts"] == "500.00"
    assert body["total_deductible"] == "20.00"


def test_reimbursements_on_includes_reimbursable_expenses_and_income(
    db_client_1099_on: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Travel - Airfare", tax_line="travel", deductible_pct=100)
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=category.id, bucket=Bucket.business, amount=Decimal("-20.00")
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.reimbursable,
            amount=Decimal("-100.00"),
        ),
        make_income_entry(
            user, type=IncomeType.event_pay, expected=None, received=Decimal("500.00")
        ),
        make_income_entry(
            user, type=IncomeType.reimbursement, expected=None, received=Decimal("100.00")
        ),
    )

    response = db_client_1099_on.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    body = response.json()
    assert body["reimbursements_included"] is True
    assert body["expenses_by_line"] == [
        {"tax_line": "travel", "spent": "120.00", "deductible_pct": 100, "deductible": "120.00"}
    ]
    assert body["gross_receipts"] == "600.00"
    assert body["total_deductible"] == "120.00"


def test_a_split_parent_is_excluded_but_its_children_are_counted(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Gear", tax_line="supplies", deductible_pct=100)
    _persist(db_session, account, category)
    parent = make_transaction(user, account, bucket=Bucket.business, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    child_a = make_transaction(
        user,
        account,
        category_id=category.id,
        bucket=Bucket.business,
        amount=Decimal("-60.00"),
        parent_id=parent.id,
    )
    child_b = make_transaction(
        user,
        account,
        category_id=category.id,
        bucket=Bucket.business,
        amount=Decimal("-40.00"),
        parent_id=parent.id,
    )
    _persist(db_session, child_a, child_b)

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    line = _line(response.json(), "supplies")
    assert line["spent"] == "100.00"
    assert line["deductible"] == "100.00"


def test_pending_business_transactions_are_excluded_from_the_summary(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Gear", tax_line="supplies", deductible_pct=100)
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.business,
            amount=Decimal("-60.00"),
            pending=True,
        ),
    )

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    assert response.json()["expenses_by_line"] == []


def test_a_refund_only_category_on_a_shared_line_does_not_500_over_100_percent(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    full = make_category(user, name="Office full", tax_line="office", deductible_pct=100)
    half = make_category(user, name="Office half", tax_line="office", deductible_pct=50)
    account = make_account(user)
    _persist(db_session, account, full, half)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=full.id, bucket=Bucket.business, amount=Decimal("-120.00")
        ),
        make_transaction(
            user, account, category_id=half.id, bucket=Bucket.business, amount=Decimal("20.00")
        ),
    )

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    assert response.status_code == 200
    # 120.00 at 100% less a 20.00 refund at 50%: 110.00 deductible on 100.00 spent.
    assert _line(response.json(), "office") == {
        "tax_line": "office",
        "spent": "100.00",
        "deductible_pct": None,
        "deductible": "110.00",
    }


def test_a_refund_only_category_on_a_shared_line_does_not_500_below_zero_percent(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    none = make_category(user, name="Office none", tax_line="office", deductible_pct=0)
    full = make_category(user, name="Office full", tax_line="office", deductible_pct=100)
    account = make_account(user)
    _persist(db_session, account, none, full)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=none.id, bucket=Bucket.business, amount=Decimal("-20.00")
        ),
        make_transaction(
            user, account, category_id=full.id, bucket=Bucket.business, amount=Decimal("10.00")
        ),
    )

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    assert response.status_code == 200
    # 20.00 at 0% less a 10.00 refund at 100%: -10.00 deductible on 10.00 spent.
    assert _line(response.json(), "office") == {
        "tax_line": "office",
        "spent": "10.00",
        "deductible_pct": None,
        "deductible": "-10.00",
    }


def test_a_refund_nets_against_spend_in_the_same_category(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Gear", tax_line="supplies", deductible_pct=100)
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=category.id, bucket=Bucket.business, amount=Decimal("-50.00")
        ),
        make_transaction(
            user, account, category_id=category.id, bucket=Bucket.business, amount=Decimal("10.00")
        ),
    )

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    line = _line(response.json(), "supplies")
    assert line == {
        "tax_line": "supplies",
        "spent": "40.00",
        "deductible_pct": 100,
        "deductible": "40.00",
    }


def test_year_boundaries_use_the_calendar_year(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Gear", tax_line="supplies", deductible_pct=100)
    _persist(db_session, account, category)
    in_year = make_transaction(
        user,
        account,
        category_id=category.id,
        bucket=Bucket.business,
        amount=Decimal("-10.00"),
        posted_date=date(2026, 12, 31),
    )
    next_year = make_transaction(
        user,
        account,
        category_id=category.id,
        bucket=Bucket.business,
        amount=Decimal("-99.00"),
        posted_date=date(2027, 1, 1),
    )
    _persist(db_session, in_year, next_year)

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    line = _line(response.json(), "supplies")
    assert line["spent"] == "10.00"


def test_another_users_rows_are_excluded_from_the_summary(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(user)
    other_account = make_account(other_user)
    category = make_category(user, name="Gear", tax_line="supplies", deductible_pct=100)
    other_category = make_category(other_user, name="Gear", tax_line="supplies", deductible_pct=100)
    _persist(db_session, account, other_account, category, other_category)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=category.id, bucket=Bucket.business, amount=Decimal("-10.00")
        ),
        make_transaction(
            other_user,
            other_account,
            category_id=other_category.id,
            bucket=Bucket.business,
            amount=Decimal("-999.00"),
        ),
    )

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    line = _line(response.json(), "supplies")
    assert line["spent"] == "10.00"


def test_uncategorized_business_spend_is_reported_separately(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    no_tax_line = make_category(user, name="Misc", tax_line=None)
    _persist(db_session, account, no_tax_line)
    _persist(
        db_session,
        make_transaction(user, account, bucket=Bucket.business, amount=Decimal("-15.00")),
        make_transaction(
            user,
            account,
            category_id=no_tax_line.id,
            bucket=Bucket.business,
            amount=Decimal("-25.00"),
        ),
    )

    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    body = response.json()
    assert body["expenses_by_line"] == []
    assert body["uncategorized_business"] == {"spent": "40.00", "count": 2}


def test_notes_settle_mileage_and_subscriptions_and_leave_66_open(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers)

    notes = response.json()["notes"]
    assert [note for note in notes if "undecided" in note] == [
        next(note for note in notes if "#66" in note)
    ]
    assert any("standard mileage rate (#67)" in note for note in notes)
    assert any("personal" in note and "#68" in note for note in notes)


# --- #167: line 9 from the mileage log ----------------------------------------------


def _rates_2026(db_session: Session, user: User) -> None:
    _persist(
        db_session,
        make_mileage_rate(user, effective_from=date(2026, 1, 1), rate=Decimal("0.7250")),
        make_mileage_rate(user, effective_from=date(2026, 7, 1), rate=Decimal("0.7600")),
    )


def _business(user: User, account: Account, category: Category, amount: str) -> Transaction:
    return make_transaction(
        user, account, category_id=category.id, bucket=Bucket.business, amount=Decimal(amount)
    )


def test_line_9_prices_each_trip_at_the_rate_in_force_on_its_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _rates_2026(db_session, user)
    _persist(
        db_session,
        make_mileage_trip(user, date=date(2026, 6, 30), miles=Decimal("10.0")),
        make_mileage_trip(user, date=date(2026, 7, 1), miles=Decimal("10.0")),
    )

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    # 10.0 x 0.725 (=7.25) on 06-30 plus 10.0 x 0.76 (=7.60) on 07-01.
    assert body["mileage"] == {
        "business_miles": "20.0",
        "mileage_amount": "14.85",
        "parking_tolls": "0.00",
        "unpriced_miles": "0.0",
        "excluded_actual_car_costs": "0.00",
        "rates": [
            {"effective_from": "2026-01-01", "rate": "0.7250", "miles": "10.0"},
            {"effective_from": "2026-07-01", "rate": "0.7600", "miles": "10.0"},
        ],
        "odometer_start": None,
        "odometer_end": None,
        "total_miles": None,
    }
    assert _line(body, "car_truck") == {
        "tax_line": "car_truck",
        "spent": "14.85",
        "deductible_pct": 100,
        "deductible": "14.85",
    }
    assert body["total_deductible"] == "14.85"


def test_line_9_adds_parking_and_tolls_and_excludes_actual_car_costs(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    parking = make_category(user, name="Parking", tax_line="car_parking_tolls", deductible_pct=50)
    gas = make_category(user, name="Gas", tax_line="car_truck", deductible_pct=100)
    office = make_category(user, name="Office", tax_line="office", deductible_pct=100)
    _persist(db_session, account, parking, gas, office)
    _rates_2026(db_session, user)
    _persist(
        db_session,
        make_mileage_trip(user, date=date(2026, 3, 1), miles=Decimal("20.0")),
        _business(user, account, parking, "-20.00"),
        _business(user, account, gas, "-60.00"),
        _business(user, account, office, "-10.00"),
    )

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    # 20.0 x 0.725 = 14.50 of mileage, plus 20.00 parking at 50% (=10.00).
    assert [line["tax_line"] for line in body["expenses_by_line"]] == ["car_truck", "office"]
    assert _line(body, "car_truck") == {
        "tax_line": "car_truck",
        "spent": "34.50",
        "deductible_pct": 71,
        "deductible": "24.50",
    }
    assert body["total_deductible"] == "34.50"
    assert body["mileage"]["parking_tolls"] == "20.00"
    assert body["mileage"]["excluded_actual_car_costs"] == "60.00"
    assert any(
        "$60.00" in note and "not deductible under the standard mileage rate" in note
        for note in body["notes"]
    )
    assert not any("Tag your parking" in note for note in body["notes"])


def test_parking_alone_makes_the_car_truck_line(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    parking = make_category(user, name="Tolls", tax_line="car_parking_tolls", deductible_pct=100)
    _persist(db_session, account, parking)
    _persist(db_session, _business(user, account, parking, "-8.00"))

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    assert body["expenses_by_line"] == [
        {"tax_line": "car_truck", "spent": "8.00", "deductible_pct": 100, "deductible": "8.00"}
    ]


def test_actual_car_costs_alone_make_no_line(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    gas = make_category(user, name="Gas", tax_line="car_truck", deductible_pct=100)
    _persist(db_session, account, gas)
    _persist(db_session, _business(user, account, gas, "-45.00"))

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    assert body["expenses_by_line"] == []
    assert body["total_deductible"] == "0.00"
    assert body["uncategorized_business"] == {"spent": "0.00", "count": 0}
    assert body["mileage"]["excluded_actual_car_costs"] == "45.00"


def test_mileage_amount_is_summed_unrounded_then_quantized_once(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _rates_2026(db_session, user)
    _persist(
        db_session,
        *(make_mileage_trip(user, date=date(2026, 2, d), miles=Decimal("0.1")) for d in (1, 2, 3)),
    )

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    # 3 x 0.0725 = 0.2175 -> 0.22; rounding each trip first would give 0.21.
    assert body["mileage"]["mileage_amount"] == "0.22"
    assert _line(body, "car_truck")["deductible"] == "0.22"


def test_a_trip_before_every_rate_is_unpriced_and_named_in_a_note(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(
        db_session,
        make_mileage_rate(user, effective_from=date(2026, 3, 1), rate=Decimal("0.7250")),
        make_mileage_trip(user, date=date(2026, 2, 14), miles=Decimal("5.0")),
        make_mileage_trip(user, date=date(2026, 3, 1), miles=Decimal("2.0")),
    )

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    mileage = body["mileage"]
    assert mileage["business_miles"] == "7.0"
    assert mileage["unpriced_miles"] == "5.0"
    assert mileage["mileage_amount"] == "1.45"
    assert mileage["rates"] == [{"effective_from": "2026-03-01", "rate": "0.7250", "miles": "2.0"}]
    assert any(
        "no mileage rate" in note and "(1 date, 2026-02-14)" in note for note in body["notes"]
    )


def test_the_unpriced_note_gives_a_count_and_the_first_and_last_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(
        db_session,
        *(
            make_mileage_trip(user, date=date(2026, 1, d), miles=Decimal("1.0"))
            for d in (20, 3, 11, 3, 28)
        ),
    )

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    (note,) = [note for note in body["notes"] if "no mileage rate" in note]
    assert "5.0 business miles" in note
    assert "(4 dates, 2026-01-03 to 2026-01-28)" in note
    assert "2026-01-11" not in note


def test_a_year_with_no_priced_miles_has_no_unpriced_note(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    assert body["mileage"]["business_miles"] == "0.0"
    assert body["mileage"]["mileage_amount"] == "0.00"
    assert body["expenses_by_line"] == []
    assert not any("no mileage rate" in note for note in body["notes"])


def test_each_trip_counts_in_its_own_year_only(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(
        db_session,
        make_mileage_rate(user, effective_from=date(2025, 1, 1), rate=Decimal("0.7000")),
        make_mileage_trip(user, date=date(2025, 12, 31), miles=Decimal("10.0")),
        make_mileage_trip(user, date=date(2026, 1, 1), miles=Decimal("1.0")),
        make_mileage_trip(user, date=date(2027, 1, 1), miles=Decimal("100.0")),
    )

    y2025 = db_client.get(TAX_SUMMARY, params={"year": 2025}, headers=auth_headers).json()
    y2026 = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    assert y2025["mileage"]["business_miles"] == "10.0"
    assert y2025["mileage"]["mileage_amount"] == "7.00"
    assert y2026["mileage"]["business_miles"] == "1.0"
    assert y2026["mileage"]["mileage_amount"] == "0.70"


def test_another_users_trips_and_odometer_are_excluded(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    _rates_2026(db_session, other_user)
    _persist(
        db_session,
        make_mileage_trip(other_user, date=date(2026, 3, 1)),
        make_odometer_reading(other_user, end_reading=Decimal("50000.0")),
    )

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    assert body["mileage"]["business_miles"] == "0.0"
    assert body["mileage"]["odometer_start"] is None
    assert body["expenses_by_line"] == []


def test_odometer_readings_and_total_miles(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(
        db_session,
        make_odometer_reading(
            user, start_reading=Decimal("41250.0"), end_reading=Decimal("52000.5")
        ),
        make_odometer_reading(user, year=2025, start_reading=Decimal("30000.0")),
    )

    y2026 = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()
    y2025 = db_client.get(TAX_SUMMARY, params={"year": 2025}, headers=auth_headers).json()

    assert y2026["mileage"]["odometer_start"] == "41250.0"
    assert y2026["mileage"]["odometer_end"] == "52000.5"
    assert y2026["mileage"]["total_miles"] == "10750.5"
    assert y2025["mileage"]["odometer_start"] == "30000.0"
    assert y2025["mileage"]["odometer_end"] is None
    assert y2025["mileage"]["total_miles"] is None


def test_trips_without_tagged_car_categories_ask_for_tagging(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _rates_2026(db_session, user)
    _persist(db_session, make_mileage_trip(user, date=date(2026, 3, 1)))

    body = db_client.get(TAX_SUMMARY, params={"year": 2026}, headers=auth_headers).json()

    assert any(
        "car_parking_tolls" in note and "car_truck" in note and "Tag" in note
        for note in body["notes"]
    )


def test_summary_year_out_of_range_is_422_not_500(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = db_client.get(TAX_SUMMARY, params={"year": 999999999}, headers=auth_headers)

    assert response.status_code == 422


def test_summary_year_must_be_an_integer(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = db_client.get(TAX_SUMMARY, params={"year": "abc"}, headers=auth_headers)

    assert response.status_code == 422


def test_summary_year_is_required(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    response = db_client.get(TAX_SUMMARY, headers=auth_headers)

    assert response.status_code == 422


def test_business_spend_reconciles_with_the_transactions_list(
    db_client_1099_off: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """The acceptance criterion: total business `spent` (every line plus
    uncategorized) equals the sum of `-amount` from `GET /api/transactions?
    bucket=business&pending=false&exclude_split_parents=true` over every month of
    the year.
    """
    account = make_account(user)
    airfare = make_category(user, name="Travel - Airfare", tax_line="travel", deductible_pct=100)
    food = make_category(user, name="Travel - Food", tax_line="travel_food", deductible_pct=50)
    _persist(db_session, account, airfare, food)
    parent = make_transaction(user, account, bucket=Bucket.business, amount=Decimal("-100.00"))
    _persist(db_session, parent)
    _persist(
        db_session,
        make_transaction(
            user, account, category_id=airfare.id, bucket=Bucket.business, amount=Decimal("-200.00")
        ),
        make_transaction(
            user,
            account,
            category_id=food.id,
            bucket=Bucket.business,
            amount=Decimal("-40.00"),
            posted_date=date(2026, 3, 15),
        ),
        make_transaction(user, account, bucket=Bucket.business, amount=Decimal("-15.00")),
        make_transaction(
            user,
            account,
            bucket=Bucket.business,
            amount=Decimal("10.00"),
            posted_date=date(2026, 6, 1),
        ),
        make_transaction(
            user, account, bucket=Bucket.business, amount=Decimal("-999.00"), pending=True
        ),
        make_transaction(
            user,
            account,
            category_id=airfare.id,
            bucket=Bucket.business,
            amount=Decimal("-60.00"),
            parent_id=parent.id,
        ),
        make_transaction(user, account, bucket=Bucket.reimbursable, amount=Decimal("-500.00")),
    )

    summary = db_client_1099_off.get(
        TAX_SUMMARY, params={"year": 2026}, headers=auth_headers
    ).json()
    summary_total = sum(Decimal(line["spent"]) for line in summary["expenses_by_line"])
    summary_total += Decimal(summary["uncategorized_business"]["spent"])

    transactions_total = Decimal("0.00")
    for month in range(1, 13):
        response = db_client_1099_off.get(
            "/api/transactions",
            params={
                "month": f"2026-{month:02d}",
                "bucket": "business",
                "pending": "false",
                "exclude_split_parents": "true",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        for item in response.json()["items"]:
            transactions_total += -Decimal(item["amount"])

    assert summary_total == transactions_total
    assert summary_total == Decimal("305.00")
