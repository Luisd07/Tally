"""Acceptance tests for #69: GET /api/dashboard, spending by category against
budget plus the trailing trend.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.enums import Bucket, CategoryKind
from tests.factories import make_account, make_category, make_transaction

DASHBOARD = "/api/dashboard"


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _category_row(body: Any, category_id: uuid.UUID | None) -> Any:
    id_str = None if category_id is None else str(category_id)
    return next(row for row in body["by_category"] if row["category_id"] == id_str)


def _transactions_spend(
    db_client: TestClient, auth_headers: dict[str, str], *, month: str, category_id: uuid.UUID
) -> Decimal:
    """Sum of `-amount` from `GET /api/transactions`, filtered the same way the
    dashboard's spend aggregate is: settled (`pending=false`) `personal`/`business`
    rows in `month`, for one category.
    """
    total = Decimal("0.00")
    for bucket in ("personal", "business"):
        r = db_client.get(
            "/api/transactions",
            params={
                "month": month,
                "pending": "false",
                "bucket": bucket,
                "category_id": str(category_id),
            },
            headers=auth_headers,
        )
        assert r.status_code == 200
        for item in r.json()["items"]:
            total += -Decimal(item["amount"])
    return total


# --- Classification rules (only `personal`/`business` count as spending) -----------------


def test_only_personal_and_business_buckets_count_as_spending(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Dining")
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-50.00"),
            posted_date=date(2026, 10, 1),
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.business,
            amount=Decimal("-30.00"),
            posted_date=date(2026, 10, 2),
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.reimbursable,
            amount=Decimal("-40.00"),
            posted_date=date(2026, 10, 3),
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.transfer,
            amount=Decimal("-20.00"),
            posted_date=date(2026, 10, 4),
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.income,
            amount=Decimal("100.00"),
            posted_date=date(2026, 10, 5),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    row = _category_row(body, category.id)
    assert row["spent"] == "80.00"
    assert body["total_spent"] == "80.00"


# --- Sign handling and refunds ------------------------------------------------------------


def test_spend_is_reported_as_a_positive_magnitude_with_a_refund_netted_in(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Gear")
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-50.00"),
            posted_date=date(2026, 10, 1),
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("20.00"),
            posted_date=date(2026, 10, 2),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    row = _category_row(r.json(), category.id)
    assert row["spent"] == "30.00"


# --- Pending rows --------------------------------------------------------------------------


def test_pending_rows_are_excluded(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Groceries")
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-50.00"),
            posted_date=date(2026, 10, 1),
            pending=False,
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-1000.00"),
            posted_date=date(2026, 10, 2),
            pending=True,
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    row = _category_row(r.json(), category.id)
    assert row["spent"] == "50.00"


# --- Uncategorized spending ------------------------------------------------------------


def test_uncategorized_spending_is_a_single_null_category_row(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=None,
            bucket=Bucket.personal,
            amount=Decimal("-15.00"),
            posted_date=date(2026, 10, 1),
        ),
        make_transaction(
            user,
            account,
            category_id=None,
            bucket=Bucket.personal,
            amount=Decimal("-5.00"),
            posted_date=date(2026, 10, 2),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    rows = [row for row in body["by_category"] if row["category_id"] is None]
    assert len(rows) == 1
    assert rows[0]["name"] is None
    assert rows[0]["budget"] is None
    assert rows[0]["spent"] == "20.00"
    assert rows[0]["remaining"] is None


# --- Budget with no spend ------------------------------------------------------------


def test_category_with_a_budget_and_no_spend_still_appears(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    category = make_category(user, name="Software", monthly_budget=Decimal("100.00"))
    _persist(db_session, category)

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    row = _category_row(r.json(), category.id)
    assert row["spent"] == "0.00"
    assert row["budget"] == "100.00"
    assert row["remaining"] == "100.00"


def test_archived_category_budget_is_excluded_but_its_spend_still_counts(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    live = make_category(user, name="Live", monthly_budget=Decimal("100.00"))
    old = make_category(user, name="Old", monthly_budget=Decimal("50.00"), archived=True)
    _persist(db_session, account, live, old)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=old.id,
            amount=Decimal("-20.00"),
            bucket=Bucket.personal,
            posted_date=date(2026, 10, 5),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    body = r.json()
    assert body["total_budget"] == "100.00"
    assert body["total_spent"] == "20.00"
    row = _category_row(body, old.id)
    assert row["spent"] == "20.00"
    assert row["budget"] is None


def test_category_with_no_budget_and_no_spend_does_not_appear(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    category = make_category(user, name="Unused")
    _persist(db_session, category)

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    ids = [row["category_id"] for row in r.json()["by_category"]]
    assert str(category.id) not in ids


# --- remaining = budget - spent ------------------------------------------------------------


def test_remaining_is_budget_minus_spent(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Car", monthly_budget=Decimal("100.00"))
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-40.00"),
            posted_date=date(2026, 10, 1),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    row = _category_row(r.json(), category.id)
    assert row["spent"] == "40.00"
    assert row["budget"] == "100.00"
    assert row["remaining"] == "60.00"


# --- Split parents (real splits land in #80 via parent_id) ------------------------------


def test_split_parent_with_children_is_excluded_so_spend_is_not_double_counted(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Travel - Airfare")
    _persist(db_session, account, category)
    parent = make_transaction(
        user,
        account,
        category_id=category.id,
        bucket=Bucket.personal,
        amount=Decimal("-100.00"),
        posted_date=date(2026, 10, 1),
    )
    _persist(db_session, parent)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-40.00"),
            posted_date=date(2026, 10, 1),
            parent_id=parent.id,
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    row = _category_row(r.json(), category.id)
    assert row["spent"] == "40.00"


# --- Reconciliation with GET /api/transactions (#69 acceptance: "Totals match the
# Transactions list") -----------------------------------------------------------------------


def test_spent_matches_the_transactions_list_filtered_the_same_way(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(user, name="Dining")
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-50.00"),
            posted_date=date(2026, 10, 1),
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.business,
            amount=Decimal("-30.00"),
            posted_date=date(2026, 10, 2),
        ),
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-1000.00"),
            posted_date=date(2026, 10, 3),
            pending=True,
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    dashboard_spent = Decimal(_category_row(r.json(), category.id)["spent"])
    list_spent = _transactions_spend(
        db_client, auth_headers, month="2026-10", category_id=category.id
    )

    assert dashboard_spent == list_spent == Decimal("80.00")


def test_spent_diverges_from_the_transactions_list_when_a_split_parent_exists(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """Reconciliation rule: the dashboard excludes a split parent (one with its own
    children) from spend, but `GET /api/transactions` has no way to filter that out
    yet (`parent_id` isn't even in `TransactionOut`, and #80 hasn't added splits
    through the API). Until #80 ships, every transaction reachable through the API
    is leaf-level, so the two totals always agree outside a test like this one that
    pokes `parent_id` directly. Once #80 lands, the list must gain the same
    exclusion (or the client must subtract split parents itself) to keep matching.
    """
    account = make_account(user)
    category = make_category(user, name="Travel - Airfare")
    _persist(db_session, account, category)
    parent = make_transaction(
        user,
        account,
        category_id=category.id,
        bucket=Bucket.personal,
        amount=Decimal("-100.00"),
        posted_date=date(2026, 10, 1),
    )
    _persist(db_session, parent)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-40.00"),
            posted_date=date(2026, 10, 1),
            parent_id=parent.id,
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    dashboard_spent = Decimal(_category_row(r.json(), category.id)["spent"])
    list_spent = _transactions_spend(
        db_client, auth_headers, month="2026-10", category_id=category.id
    )

    assert dashboard_spent == Decimal("40.00")
    assert list_spent == Decimal("140.00")
    assert list_spent - dashboard_spent == Decimal("100.00")  # the excluded parent's own spend


# --- Category kind (only `expense` categories carry a budget here) -------------------------


def test_income_kind_category_budget_is_excluded_from_by_category_and_total_budget(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    budget_only = make_category(
        user, name="Pay Target", kind=CategoryKind.income, monthly_budget=Decimal("5000.00")
    )
    _persist(db_session, budget_only)

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert str(budget_only.id) not in [row["category_id"] for row in body["by_category"]]
    assert body["total_budget"] == "0.00"


def test_income_kind_category_with_spend_is_not_listed_as_spending(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    category = make_category(
        user,
        name="Reimbursed Supplies",
        kind=CategoryKind.income,
        monthly_budget=Decimal("5000.00"),
    )
    _persist(db_session, account, category)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=category.id,
            bucket=Bucket.personal,
            amount=Decimal("-10.00"),
            posted_date=date(2026, 10, 1),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert str(category.id) not in [row["category_id"] for row in body["by_category"]]
    assert body["total_spent"] == "0.00"
    assert body["total_budget"] == "0.00"


# --- Row order and multi-category totals ---------------------------------------------------


def test_by_category_is_ordered_by_name_with_totals_summed_across_rows(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    zebra = make_category(user, name="Zzz Travel", monthly_budget=Decimal("50.00"))
    aardvark = make_category(user, name="Aaa Dining", monthly_budget=Decimal("30.00"))
    unbudgeted = make_category(user, name="Mmm Gear")
    _persist(db_session, account, zebra, aardvark, unbudgeted)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            category_id=aardvark.id,
            bucket=Bucket.personal,
            amount=Decimal("-10.00"),
            posted_date=date(2026, 10, 1),
        ),
        make_transaction(
            user,
            account,
            category_id=unbudgeted.id,
            bucket=Bucket.personal,
            amount=Decimal("-20.00"),
            posted_date=date(2026, 10, 2),
        ),
        make_transaction(
            user,
            account,
            category_id=None,
            bucket=Bucket.personal,
            amount=Decimal("-5.00"),
            posted_date=date(2026, 10, 3),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    names = [row["name"] for row in body["by_category"]]
    assert names == ["Aaa Dining", "Mmm Gear", "Zzz Travel", None]
    assert body["total_budget"] == "80.00"
    assert body["total_spent"] == "35.00"


# --- Trend, zero-filled across a year boundary ------------------------------------------


def test_trend_is_zero_filled_across_a_year_boundary(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    _persist(
        db_session,
        make_transaction(
            user,
            account,
            bucket=Bucket.personal,
            amount=Decimal("-50.00"),
            posted_date=date(2026, 1, 15),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-01", "months": 3}, headers=auth_headers)

    assert r.status_code == 200
    trend = r.json()["trend"]
    assert trend == [
        {"month": "2025-11", "spent": "0.00"},
        {"month": "2025-12", "spent": "0.00"},
        {"month": "2026-01", "spent": "50.00"},
    ]


def test_trend_defaults_to_six_months(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    months = [row["month"] for row in r.json()["trend"]]
    assert months == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]


# --- User scoping ------------------------------------------------------------------------


def test_another_users_rows_are_excluded(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    other_account = make_account(other)
    other_category = make_category(other, name="Dining")
    _persist(db_session, other_account, other_category)
    _persist(
        db_session,
        make_transaction(
            other,
            other_account,
            category_id=other_category.id,
            bucket=Bucket.personal,
            amount=Decimal("-999.00"),
            posted_date=date(2026, 10, 1),
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert body["by_category"] == []
    assert body["total_spent"] == "0.00"
    assert body["total_budget"] == "0.00"


# --- month/months validation ------------------------------------------------------------


def test_missing_month_is_422(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    r = db_client.get(DASHBOARD, headers=auth_headers)
    assert r.status_code == 422


def test_badly_formatted_month_is_422(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    r = db_client.get(DASHBOARD, params={"month": "2026-13"}, headers=auth_headers)
    assert r.status_code == 422


def test_month_out_of_range_is_422(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    r = db_client.get(DASHBOARD, params={"month": "0000-12"}, headers=auth_headers)
    assert r.status_code == 422


def test_months_out_of_range_is_422(db_client: TestClient, auth_headers: dict[str, str]) -> None:
    too_few = db_client.get(
        DASHBOARD, params={"month": "2026-10", "months": 0}, headers=auth_headers
    )
    too_many = db_client.get(
        DASHBOARD, params={"month": "2026-10", "months": 25}, headers=auth_headers
    )
    assert too_few.status_code == 422
    assert too_many.status_code == 422


def test_trend_window_before_year_one_is_422_not_500(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    """`month` alone is a valid `Month`, but the default 6-month trend would start
    in year 0. UB-1/MV-3: this used to raise an unhandled `ValueError` (500).
    """
    r = db_client.get(DASHBOARD, params={"month": "0001-03"}, headers=auth_headers)
    assert r.status_code == 422


def test_trend_window_before_year_one_with_explicit_months_is_422_not_500(
    db_client: TestClient, auth_headers: dict[str, str]
) -> None:
    r = db_client.get(DASHBOARD, params={"month": "0001-06", "months": 24}, headers=auth_headers)
    assert r.status_code == 422


# --- Income-kind categories are not spending ----------------------------------------------


def test_income_category_paycheck_is_not_spending_but_refund_and_uncategorized_still_count(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    paycheck = make_category(user, name="Paycheck", kind=CategoryKind.income)
    gear = make_category(user, name="Gear")
    _persist(db_session, account, paycheck, gear)
    day = date(2026, 10, 5)
    _persist(
        db_session,
        *(
            make_transaction(
                user,
                account,
                category_id=category_id,
                bucket=Bucket.personal,
                amount=Decimal(amount),
                posted_date=day,
            )
            for category_id, amount in (
                (paycheck.id, "2000.00"),
                (gear.id, "-100.00"),
                (gear.id, "20.00"),
                (None, "-30.00"),
            )
        ),
    )

    r = db_client.get(DASHBOARD, params={"month": "2026-10"}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert str(paycheck.id) not in [row["category_id"] for row in body["by_category"]]
    assert _category_row(body, gear.id)["spent"] == "80.00"
    assert _category_row(body, None)["spent"] == "30.00"
    assert body["total_spent"] == "110.00"
