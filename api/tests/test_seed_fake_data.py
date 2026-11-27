import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuthEvent,
    Category,
    Device,
    Passkey,
    Receipt,
    RefreshToken,
    Subscription,
    Transaction,
    User,
)
from app.models.base import Base
from app.models.enums import Bucket, CategoryKind
from app.schemas.dashboard import DashboardOut, DashboardQuery
from app.schemas.home import HomeOut, HomeQuery
from app.schemas.inbox import InboxPage, InboxQuery
from app.schemas.tax import MissingReceiptListQuery
from app.services.dashboard import get_dashboard
from app.services.home import get_home
from app.services.inbox import list_inbox
from app.services.tax import list_missing_receipts
from scripts import seed_fake_data as seed_mod

# Noon in New York on 2026-11-27, the day the README screenshots were taken.
PINNED_NOW = datetime(2026, 11, 27, 17, 0, tzinfo=UTC)

TABLES = [User, Passkey, RefreshToken, AuthEvent, Device]

PRODUCTION = Settings(
    environment="production",
    database_url=SecretStr("postgresql+psycopg://tally:s3cure@db:5432/tally"),
    jwt_signing_key=SecretStr("k" * 48),
    simplefin_encryption_key=SecretStr(Fernet.generate_key().decode()),
    webauthn_rp_id="api.example.com",
    webauthn_origin="https://api.example.com",
    apns_key_id="TESTKEY123",
    apns_team_id="TESTTEAM456",
    apns_bundle_id="com.example.tally",
    apns_private_key_path=__file__,  # any file that exists; its contents are never read here
    restic_repository="gs:tally-backups-prod:/tally",
    restic_password=SecretStr("s3cure-restic-password"),
)


def _row_counts(db: Session) -> dict[str, int]:
    return {model.__tablename__: len(db.scalars(select(model.id)).all()) for model in TABLES}


def _table_row_count(db: Session, table_name: str) -> int:
    table = Base.metadata.tables[table_name]
    return db.scalar(select(func.count()).select_from(table)) or 0


def test_seed_populates_every_table(db_session: Session) -> None:
    counts = seed_mod.seed(db_session, datetime.now(UTC))
    db_session.flush()

    assert all(count > 0 for count in counts.values())

    expected_tables = set(Base.metadata.tables) - seed_mod.SKIPPED_TABLES
    for table_name in sorted(expected_tables):
        message = f"{table_name} has no rows after seeding"
        assert _table_row_count(db_session, table_name) > 0, message


def test_seed_is_idempotent(db_session: Session) -> None:
    seed_mod.seed(db_session, datetime.now(UTC))
    db_session.flush()
    before = _row_counts(db_session)

    second_run = seed_mod.seed(db_session, datetime.now(UTC))
    db_session.flush()

    assert all(count == 0 for count in second_run.values())
    assert _row_counts(db_session) == before


def test_seed_reuses_an_existing_owner(db_session: Session, user: User) -> None:
    counts = seed_mod.seed(db_session, datetime.now(UTC))
    db_session.flush()

    assert counts["users"] == 0
    assert _row_counts(db_session)["users"] == 1
    assert set(db_session.scalars(select(Passkey.user_id)).all()) == {user.id}


def test_seed_has_auth_events_without_a_user(db_session: Session) -> None:
    seed_mod.seed(db_session, datetime.now(UTC))
    db_session.flush()

    orphaned = db_session.scalars(select(AuthEvent).where(AuthEvent.user_id.is_(None))).all()
    assert len(orphaned) >= 2


def test_main_refuses_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_mod, "get_settings", lambda: PRODUCTION)

    with pytest.raises(SystemExit, match="production"):
        seed_mod.main()


def _noon(day: date) -> datetime:
    """Midday in `APP_TIMEZONE` on `day`, as the UTC instant `seed` takes."""
    return datetime(day.year, day.month, day.day, 17, 0, tzinfo=UTC)


def _owner_id(db: Session) -> uuid.UUID:
    return db.scalars(select(User.id)).one()


def _dashboard(db: Session, month: str) -> DashboardOut:
    return get_dashboard(db, _owner_id(db), DashboardQuery(month=month))


def _home(db: Session, now: datetime) -> HomeOut:
    month = seed_mod._today(now).strftime("%Y-%m")
    return get_home(db, _owner_id(db), HomeQuery(month=month), now)


def _inbox(db: Session, now: datetime) -> InboxPage:
    return list_inbox(db, _owner_id(db), InboxQuery(), now)


def _transaction_dates(db: Session) -> dict[uuid.UUID, date]:
    rows = db.execute(select(Transaction.id, Transaction.posted_date)).all()
    return {row[0]: row[1] for row in rows}


def test_current_month_has_budgets_and_spending(db_session: Session) -> None:
    seed_mod.seed(db_session, PINNED_NOW)
    db_session.flush()

    dashboard = _dashboard(db_session, "2026-11")

    assert dashboard.total_budget == Decimal("3070.00")
    assert Decimal("2500.00") < dashboard.total_spent < dashboard.total_budget
    budgeted = [row for row in dashboard.by_category if row.budget is not None]
    assert len(budgeted) >= 8
    assert all(row.spent > 0 for row in budgeted)
    # The three months before are filled too, so the trend isn't flat.
    assert [month.spent > 0 for month in dashboard.trend] == [False, False, True, True, True, True]
    assert len({month.spent for month in dashboard.trend[2:]}) == 4

    in_month = db_session.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.posted_date >= date(2026, 11, 1),
            Transaction.posted_date <= date(2026, 11, 27),
        )
    )
    assert (in_month or 0) >= 36


def test_every_expense_category_has_a_budget(db_session: Session) -> None:
    seed_mod.seed(db_session, PINNED_NOW)
    db_session.flush()

    budgets = db_session.scalars(
        select(Category.monthly_budget).where(Category.kind == CategoryKind.expense)
    ).all()
    assert budgets
    assert all(budget is not None and budget > 0 for budget in budgets)


def test_home_numbers_are_driven_by_the_seed(db_session: Session) -> None:
    seed_mod.seed(db_session, PINNED_NOW)
    db_session.flush()

    home = _home(db_session, PINNED_NOW)

    assert home.spending_by_bucket.total == _dashboard(db_session, "2026-11").total_spent
    assert home.owed_to_you == Decimal("874.96")
    assert home.income.received > 0
    assert {bill.kind for bill in home.bills_due} == {"subscription", "card"}
    assert home.stale_banks == []
    assert home.last_updated is not None
    assert home.setup.budgets_set and home.setup.inbox_reviewed

    owner_id = _owner_id(db_session)
    missing = sum(
        list_missing_receipts(
            db_session, owner_id, MissingReceiptListQuery(year=2026, bucket=bucket)
        ).count
        for bucket in (Bucket.business, Bucket.reimbursable)
    )
    assert missing == 9
    # The flight has its receipt, so it is not one of them.
    assert db_session.scalars(select(Receipt.transaction_id)).all() == [
        seed_mod._uuid("transaction:business")
    ]


def test_inbox_has_five_rows_some_with_suggestions(db_session: Session) -> None:
    seed_mod.seed(db_session, PINNED_NOW)
    db_session.flush()

    inbox = _inbox(db_session, PINNED_NOW)

    assert inbox.total == 5
    assert all(not item.reviewed for item in inbox.items)
    sources = [item.suggestion_source for item in inbox.items]
    assert sources.count("history") == 3
    assert sources.count("ai") == 1
    assert sources.count(None) == 1


def test_seed_demonstrates_a_split_a_transfer_and_report_links(db_session: Session) -> None:
    seed_mod.seed(db_session, PINNED_NOW)
    db_session.flush()

    def row(label: str) -> Transaction:
        return db_session.get_one(Transaction, seed_mod._uuid(label))

    parent = row("transaction:split:parent")
    children = [row("transaction:split:child:1"), row("transaction:split:child:2")]
    assert all(child.parent_id == parent.id for child in children)
    assert sum(child.amount for child in children) == parent.amount

    out, into = row("transaction:transfer:out"), row("transaction:transfer:into")
    assert (out.transfer_pair_id, into.transfer_pair_id) == (into.id, out.id)
    assert out.amount == -into.amount

    assert row("transaction:reimbursable:submitted").reimbursement_id is not None
    assert row("transaction:reimbursable:unassigned").reimbursement_id is None


def test_seed_on_a_later_day_inserts_nothing_and_moves_nothing(db_session: Session) -> None:
    seed_mod.seed(db_session, PINNED_NOW)
    db_session.flush()
    dates_before = _transaction_dates(db_session)
    next_due_before = db_session.scalars(select(Subscription.next_due)).all()

    # Twelve days on, in the next month.
    later = seed_mod.seed(db_session, PINNED_NOW + timedelta(days=12))
    db_session.flush()
    db_session.expire_all()

    assert all(count == 0 for count in later.values())
    assert _transaction_dates(db_session) == dates_before
    assert db_session.scalars(select(Subscription.next_due)).all() == next_due_before
    assert _dashboard(db_session, "2026-11").total_budget == Decimal("3070.00")


@pytest.mark.parametrize(
    "today",
    [
        date(2026, 12, 1),
        date(2027, 1, 1),  # the months before are in the previous year
        date(2026, 3, 2),  # right after a 28-day month
        date(2028, 2, 29),
        date(2026, 10, 3),
        date(2026, 12, 31),
    ],
)
def test_seed_works_on_any_day_of_the_month(db_session: Session, today: date) -> None:
    now = _noon(today)
    counts = seed_mod.seed(db_session, now)
    db_session.flush()

    assert all(count > 0 for count in counts.values())
    assert max(_transaction_dates(db_session).values()) == today

    current = _dashboard(db_session, today.strftime("%Y-%m"))
    assert current.total_spent > 0
    assert current.total_budget == Decimal("3070.00")
    # The month before is complete, however little of this one has gone by.
    assert current.trend[-2].spent > Decimal("2000.00")

    assert _inbox(db_session, now).total == 5
    home = _home(db_session, now)
    assert home.owed_to_you == Decimal("874.96")
    assert home.bills_due
    assert all(today <= bill.due_date <= today + timedelta(days=7) for bill in home.bills_due)


def test_today_is_taken_in_the_app_timezone(db_session: Session) -> None:
    # 03:00 UTC on December 1st is still the evening of November 30th in New York.
    seed_mod.seed(db_session, datetime(2026, 12, 1, 3, 0, tzinfo=UTC))
    db_session.flush()

    assert max(_transaction_dates(db_session).values()) == date(2026, 11, 30)
