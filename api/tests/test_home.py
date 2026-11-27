"""Acceptance tests for #45: GET /api/home, every Home-screen section in one call."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.enums import AccountKind, Bucket, CategoryKind, ReimbursementStatus, RunStatus
from app.schemas.home import HomeOut, HomeQuery
from app.services.home import get_home
from app.services.reimbursements import owed_to_you
from tests.factories import (
    make_account,
    make_category,
    make_device,
    make_income_entry,
    make_reimbursement,
    make_simplefin_connection,
    make_snapshot,
    make_subscription,
    make_sync_run,
    make_transaction,
)

HOME = "/api/home"
MONTH = "2026-10"
ZERO = Decimal("0.00")


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


def _home(db_session: Session, user: User, now: datetime, month: str = MONTH) -> HomeOut:
    return get_home(db_session, user.id, HomeQuery(month=month), now)


def _bucket(body: Any, name: str) -> Decimal:
    row = next(r for r in body["spending_by_bucket"]["buckets"] if r["bucket"] == name)
    return Decimal(row["spent"])


# --- Spending by bucket ----------------------------------------------------------------


def test_spending_by_bucket_matches_filtered_transactions(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-60.00"), bucket=Bucket.personal)
    _persist(db_session, parent)
    _persist(
        db_session,
        make_transaction(user, account, amount=Decimal("-10.00"), bucket=Bucket.personal),
        make_transaction(user, account, amount=Decimal("-5.25"), bucket=Bucket.personal),
        make_transaction(user, account, amount=Decimal("2.00"), bucket=Bucket.personal),
        make_transaction(user, account, amount=Decimal("-40.00"), bucket=Bucket.business),
        make_transaction(user, account, amount=Decimal("-99.00"), bucket=Bucket.transfer),
        make_transaction(user, account, amount=Decimal("-77.00"), bucket=Bucket.reimbursable),
        make_transaction(user, account, amount=Decimal("500.00"), bucket=Bucket.income),
        make_transaction(
            user, account, amount=Decimal("-8.00"), bucket=Bucket.personal, pending=True
        ),
        make_transaction(
            user,
            account,
            amount=Decimal("-30.00"),
            bucket=Bucket.personal,
            posted_date=date(2026, 9, 30),
        ),
        make_transaction(
            user,
            account,
            amount=Decimal("-60.00"),
            bucket=Bucket.personal,
            parent_id=parent.id,
        ),
    )

    body = db_client.get(HOME, params={"month": MONTH}, headers=auth_headers).json()

    total = ZERO
    for name in ("personal", "business"):
        r = db_client.get(
            "/api/transactions",
            params={
                "month": MONTH,
                "bucket": name,
                "pending": "false",
                "exclude_split_parents": "true",
            },
            headers=auth_headers,
        )
        expected = sum((-Decimal(i["amount"]) for i in r.json()["items"]), ZERO)
        assert _bucket(body, name) == expected
        total += expected
    assert _bucket(body, "personal") == Decimal("73.25")
    assert _bucket(body, "business") == Decimal("40.00")
    assert Decimal(body["spending_by_bucket"]["total"]) == total == Decimal("113.25")


def test_income_category_rows_do_not_count_in_spending_by_bucket(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    paycheck = make_category(user, name="Paycheck", kind=CategoryKind.income)
    gear = make_category(user, name="Gear")
    _persist(db_session, account, paycheck, gear)
    _persist(
        db_session,
        *(
            make_transaction(
                user,
                account,
                category_id=category_id,
                amount=Decimal(amount),
                bucket=Bucket.personal,
            )
            for category_id, amount in (
                (paycheck.id, "2000.00"),
                (gear.id, "-100.00"),
                (gear.id, "20.00"),
                (None, "-30.00"),
            )
        ),
    )

    body = _home(db_session, user, datetime(2026, 10, 15, 12, tzinfo=UTC))

    personal = next(b for b in body.spending_by_bucket.buckets if b.bucket == Bucket.personal)
    assert personal.spent == Decimal("110.00")


def test_empty_month_is_all_zero(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    body = db_client.get(HOME, params={"month": MONTH}, headers=auth_headers).json()

    assert _bucket(body, "personal") == _bucket(body, "business") == ZERO
    assert Decimal(body["owed_to_you"]) == ZERO
    assert body["income"] == {"expected": "0.00", "received": "0.00"}
    assert body["bills_due"] == []
    assert body["last_updated"] is None


# --- Owed to you -----------------------------------------------------------------------


def test_owed_to_you_matches_reimbursements_service(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    draft = make_reimbursement(user)
    submitted = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    paid = make_reimbursement(
        user,
        status=ReimbursementStatus.paid,
        submitted_date=date(2026, 9, 1),
        paid_date=date(2026, 9, 5),
    )
    _persist(db_session, account, draft, submitted, paid)
    _persist(
        db_session,
        make_transaction(user, account, amount=Decimal("-10.00"), bucket=Bucket.reimbursable),
        make_transaction(
            user,
            account,
            amount=Decimal("-20.00"),
            bucket=Bucket.reimbursable,
            reimbursement_id=draft.id,
        ),
        make_transaction(
            user,
            account,
            amount=Decimal("-30.00"),
            bucket=Bucket.reimbursable,
            reimbursement_id=submitted.id,
        ),
        make_transaction(
            user,
            account,
            amount=Decimal("-999.00"),
            bucket=Bucket.reimbursable,
            reimbursement_id=paid.id,
        ),
    )

    body = db_client.get(HOME, params={"month": MONTH}, headers=auth_headers).json()

    assert Decimal(body["owed_to_you"]) == Decimal("60.00")
    assert Decimal(body["owed_to_you"]) == owed_to_you(db_session, user.id)
    assert _bucket(body, "personal") == ZERO


# --- Income, bills, last updated -------------------------------------------------------


def test_income_sums_the_months_entries(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(
        db_session,
        make_income_entry(
            user, date=date(2026, 10, 1), expected=Decimal("100.00"), received=Decimal("90.00")
        ),
        make_income_entry(user, date=date(2026, 10, 31), expected=Decimal("50.50"), received=None),
        make_income_entry(user, date=date(2026, 10, 15), expected=None, received=Decimal("7.00")),
        make_income_entry(
            user, date=date(2026, 11, 1), expected=Decimal("999.00"), received=Decimal("999.00")
        ),
    )

    body = db_client.get(HOME, params={"month": MONTH}, headers=auth_headers).json()

    assert body["income"] == {"expected": "150.50", "received": "97.00"}


def test_bills_due_subscriptions_in_window(db_session: Session, user: User) -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    _persist(
        db_session,
        make_subscription(user, name="Today", amount=Decimal("-5.00"), next_due=date(2026, 10, 10)),
        make_subscription(user, name="Edge", amount=Decimal("-6.00"), next_due=date(2026, 10, 17)),
        make_subscription(user, name="Late", next_due=date(2026, 10, 18)),
        make_subscription(user, name="Past", next_due=date(2026, 10, 9)),
        make_subscription(user, name="Inactive", next_due=date(2026, 10, 12), active=False),
        make_subscription(user, name="NoDate", next_due=None),
    )

    bills = _home(db_session, user, now).bills_due

    assert [(b.kind, b.name, b.due_date, b.amount) for b in bills] == [
        ("subscription", "Today", date(2026, 10, 10), Decimal("5.00")),
        ("subscription", "Edge", date(2026, 10, 17), Decimal("6.00")),
    ]


@pytest.mark.parametrize(
    ("now", "included"),
    [
        (datetime(2026, 3, 10, 2, 0, tzinfo=UTC), True),
        (datetime(2026, 3, 10, 6, 0, tzinfo=UTC), False),
    ],
)
def test_today_is_the_date_in_the_app_timezone(
    db_session: Session, user: User, now: datetime, included: bool
) -> None:
    _persist(db_session, make_subscription(user, name="Evening", next_due=date(2026, 3, 9)))

    bills = _home(db_session, user, now).bills_due

    assert [b.name for b in bills] == (["Evening"] if included else [])


def test_bills_due_card_due_day_and_sorting(db_session: Session, user: User) -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    card = make_account(
        user,
        name="Visa",
        kind=AccountKind.credit_card,
        due_day=14,
        planned_payment=Decimal("200.00"),
    )
    _persist(
        db_session,
        card,
        make_account(user, name="Passed", kind=AccountKind.credit_card, due_day=9),
        make_account(user, name="Far", kind=AccountKind.credit_card, due_day=25),
        make_account(user, name="NoDay", kind=AccountKind.credit_card),
        make_account(user, name="Checking", kind=AccountKind.checking, due_day=12),
        make_account(user, name="Closed", kind=AccountKind.credit_card, due_day=12, active=False),
        make_subscription(user, name="Early", next_due=date(2026, 10, 12)),
        make_subscription(user, name="Late", next_due=date(2026, 10, 16)),
    )

    bills = _home(db_session, user, now).bills_due

    assert [(b.kind, b.name, b.due_date) for b in bills] == [
        ("subscription", "Early", date(2026, 10, 12)),
        ("card", "Visa", date(2026, 10, 14)),
        ("subscription", "Late", date(2026, 10, 16)),
    ]
    assert bills[1].id == card.id
    assert bills[1].amount == Decimal("200.00")


def test_bills_due_includes_a_loan_with_a_due_day(db_session: Session, user: User) -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    loan = make_account(
        user,
        name="Car",
        kind=AccountKind.loan,
        due_day=14,
        planned_payment=Decimal("650.00"),
    )
    _persist(db_session, loan)

    [bill] = _home(db_session, user, now).bills_due

    assert (bill.kind, bill.id, bill.due_date, bill.amount) == (
        "card",
        loan.id,
        date(2026, 10, 14),
        Decimal("650.00"),
    )


def test_card_due_day_31_clamps_in_a_30_day_month(db_session: Session, user: User) -> None:
    _persist(db_session, make_account(user, name="Visa", kind=AccountKind.credit_card, due_day=31))

    bills = _home(db_session, user, datetime(2026, 11, 25, tzinfo=UTC), "2026-11").bills_due

    assert [(b.name, b.due_date, b.amount) for b in bills] == [("Visa", date(2026, 11, 30), None)]


@pytest.mark.parametrize(
    ("now", "due_day", "expected"),
    [
        (datetime(2026, 10, 28, tzinfo=UTC), 3, [date(2026, 11, 3)]),
        (datetime(2026, 12, 29, tzinfo=UTC), 2, [date(2027, 1, 2)]),
        (datetime(2027, 1, 28, tzinfo=UTC), 31, [date(2027, 1, 31)]),
        (datetime(2026, 2, 26, tzinfo=UTC), 31, [date(2026, 2, 28)]),
        (datetime(2027, 1, 31, 12, 0, tzinfo=UTC), 30, []),
        (datetime(2026, 10, 28, tzinfo=UTC), 10, []),
    ],
)
def test_card_window_crossing_month_end(
    db_session: Session, user: User, now: datetime, due_day: int, expected: list[date]
) -> None:
    _persist(
        db_session, make_account(user, name="Visa", kind=AccountKind.credit_card, due_day=due_day)
    )

    bills = _home(db_session, user, now).bills_due

    assert [b.due_date for b in bills] == expected


def test_last_updated_is_latest_ok_run(db_session: Session, user: User) -> None:
    latest_ok = datetime(2026, 10, 5, 5, 1, tzinfo=UTC)
    _persist(
        db_session,
        make_sync_run(user, finished_at=datetime(2026, 10, 3, 5, 1, tzinfo=UTC)),
        make_sync_run(user, finished_at=latest_ok),
        make_sync_run(
            user, status=RunStatus.failed, finished_at=datetime(2026, 10, 6, 5, 1, tzinfo=UTC)
        ),
        make_sync_run(
            user, status=RunStatus.partial, finished_at=datetime(2026, 10, 7, 5, 1, tzinfo=UTC)
        ),
        make_sync_run(user, status=RunStatus.running, finished_at=None),
    )

    assert _home(db_session, user, datetime(2026, 10, 8, tzinfo=UTC)).last_updated == latest_ok


# --- One call, validation, scoping -----------------------------------------------------


def test_single_call_returns_every_section(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    _persist(db_session, make_sync_run(user))

    r = db_client.get(HOME, params={"month": MONTH}, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "month",
        "spending_by_bucket",
        "owed_to_you",
        "income",
        "bills_due",
        "last_updated",
        "stale_banks",
        "setup",
    }
    assert body["month"] == MONTH
    assert body["last_updated"] is not None


@pytest.mark.parametrize("params", [{}, {"month": "2026-13"}, {"month": "2026-1"}, {"month": "x"}])
def test_missing_or_malformed_month_is_422(
    db_client: TestClient, auth_headers: dict[str, str], params: dict[str, str]
) -> None:
    assert db_client.get(HOME, params=params, headers=auth_headers).status_code == 422


def test_another_users_data_never_appears(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    account = make_account(other_user)
    _persist(db_session, account)
    _persist(
        db_session,
        make_transaction(other_user, account, amount=Decimal("-10.00"), bucket=Bucket.personal),
        make_transaction(other_user, account, amount=Decimal("-20.00"), bucket=Bucket.reimbursable),
        make_income_entry(other_user, expected=Decimal("100.00"), received=Decimal("100.00")),
        make_subscription(other_user, next_due=datetime.now(UTC).date()),
        make_account(other_user, name="Visa", kind=AccountKind.credit_card, due_day=1),
        make_sync_run(other_user),
    )

    body = db_client.get(HOME, params={"month": MONTH}, headers=auth_headers).json()

    assert Decimal(body["spending_by_bucket"]["total"]) == ZERO
    assert Decimal(body["owed_to_you"]) == ZERO
    assert body["income"] == {"expected": "0.00", "received": "0.00"}
    assert body["bills_due"] == []
    assert body["last_updated"] is None


# --- stale banks ---


def test_stale_banks_names_a_bank_simplefin_stopped_refreshing(
    db_session: Session, user: User
) -> None:
    now = datetime(2026, 10, 27, 15, tzinfo=UTC)
    chase_a = make_account(user, institution="Chase Bank", simplefin_account_id="sf-1")
    chase_b = make_account(user, institution="Chase Bank", simplefin_account_id="sf-2")
    citi = make_account(user, institution="Citibank", simplefin_account_id="sf-3")
    manual = make_account(user, institution="Cash Box")
    _persist(db_session, chase_a, chase_b, citi, manual)
    _persist(
        db_session,
        make_snapshot(user, chase_a, date=date(2026, 10, 24)),
        make_snapshot(user, chase_b, date=date(2026, 10, 23)),
        make_snapshot(user, citi, date=date(2026, 10, 25)),
        make_snapshot(user, manual, date=date(2026, 9, 1)),
    )

    stale = _home(db_session, user, now).stale_banks
    assert [(bank.institution, bank.last_refreshed) for bank in stale] == [
        ("Chase Bank", date(2026, 10, 24))
    ]


def test_a_bank_refreshed_recently_is_not_stale(db_session: Session, user: User) -> None:
    now = datetime(2026, 10, 27, 15, tzinfo=UTC)
    bank = make_account(user, institution="Chase Bank", simplefin_account_id="sf-1")
    _persist(db_session, bank)
    _persist(db_session, make_snapshot(user, bank, date=date(2026, 10, 26)))
    assert _home(db_session, user, now).stale_banks == []


# --- Getting-started setup (#226) -----------------------------------------------------


NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _setup(db_session: Session, user: User) -> dict[str, bool]:
    return _home(db_session, user, NOW).setup.model_dump()


def test_setup_is_all_false_for_a_new_user(db_session: Session, user: User) -> None:
    assert _setup(db_session, user) == {
        "bank_connected": False,
        "inbox_reviewed": False,
        "categories_ready": False,
        "budgets_set": False,
        "notifications_on": False,
    }


def test_setup_bank_connected(db_session: Session, user: User, other_user: User) -> None:
    _persist(db_session, make_simplefin_connection(other_user))
    assert _setup(db_session, user)["bank_connected"] is False
    _persist(db_session, make_simplefin_connection(user))
    assert _setup(db_session, user)["bank_connected"] is True


def test_setup_inbox_reviewed_needs_a_reviewed_row_and_a_small_inbox(
    db_session: Session, user: User, other_user: User
) -> None:
    account = make_account(user)
    other_account = make_account(other_user)
    _persist(db_session, account, other_account)
    _persist(db_session, make_transaction(other_user, other_account, reviewed=True))
    assert _setup(db_session, user)["inbox_reviewed"] is False

    _persist(db_session, make_transaction(user, account, reviewed=True))
    assert _setup(db_session, user)["inbox_reviewed"] is True

    pending = [make_transaction(user, account, reviewed=False) for _ in range(5)]
    _persist(db_session, *pending)
    assert _setup(db_session, user)["inbox_reviewed"] is True

    _persist(db_session, make_transaction(user, account, reviewed=False))
    assert _setup(db_session, user)["inbox_reviewed"] is False


def test_setup_categories_ready_counts_three_live_expense_categories(
    db_session: Session, user: User, other_user: User
) -> None:
    _persist(db_session, *[make_category(other_user, name=f"o{i}") for i in range(3)])
    _persist(
        db_session,
        make_category(user, name="a"),
        make_category(user, name="b"),
        make_category(user, name="archived", archived=True),
        make_category(user, name="income", kind=CategoryKind.income),
    )
    assert _setup(db_session, user)["categories_ready"] is False

    _persist(db_session, make_category(user, name="c"))
    assert _setup(db_session, user)["categories_ready"] is True


def test_setup_budgets_set_needs_a_live_category_with_a_budget(
    db_session: Session, user: User, other_user: User
) -> None:
    _persist(db_session, make_category(other_user, name="o", monthly_budget=Decimal("10.00")))
    _persist(
        db_session,
        make_category(user, name="none"),
        make_category(user, name="old", monthly_budget=Decimal("10.00"), archived=True),
    )
    assert _setup(db_session, user)["budgets_set"] is False

    _persist(db_session, make_category(user, name="b", monthly_budget=Decimal("10.00")))
    assert _setup(db_session, user)["budgets_set"] is True


def test_setup_notifications_on_needs_a_device(
    db_session: Session, user: User, other_user: User
) -> None:
    _persist(db_session, make_device(other_user))
    assert _setup(db_session, user)["notifications_on"] is False
    _persist(db_session, make_device(user, apns_token="c" * 64))
    assert _setup(db_session, user)["notifications_on"] is True
