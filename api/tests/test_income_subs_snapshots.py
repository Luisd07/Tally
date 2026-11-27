import annotationlib
import datetime as dt
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    BalanceSnapshot,
    IncomeEntry,
    Subscription,
    Transaction,
    User,
)
from app.models.enums import Bucket, Cadence, IncomeType, SubscriptionKind
from tests.factories import (
    make_account,
    make_category,
    make_income_entry,
    make_snapshot,
    make_subscription,
    make_transaction,
)


def test_date_annotation_resolves_to_datetime_date() -> None:
    for model in (IncomeEntry, BalanceSnapshot):
        annotation = annotationlib.get_annotations(model)["date"]
        assert annotation.__args__[0] is dt.date


def test_income_entry_round_trips_as_exact_decimal(db_session: Session, user: User) -> None:
    entry = make_income_entry(
        user, description="October race pay", expected=Decimal("250.00"), received=Decimal("250.00")
    )
    db_session.add(entry)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(IncomeEntry, entry.id)
    assert found.description == "October race pay"
    assert found.expected == Decimal("250.00")
    assert found.received == Decimal("250.00")
    assert isinstance(found.received, Decimal)
    assert found.type is IncomeType.event_pay
    assert found.transaction_id is None


def test_income_entry_rejects_neither_expected_nor_received(
    db_session: Session, user: User
) -> None:
    entry = make_income_entry(user, expected=None, received=None)
    db_session.add(entry)
    with pytest.raises(IntegrityError, match="ck_income_entries_expected_or_received"):
        db_session.flush()


def test_income_entry_allows_received_only(db_session: Session, user: User) -> None:
    entry = make_income_entry(user, expected=None, received=Decimal("100.00"))
    db_session.add(entry)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(IncomeEntry, entry.id)
    assert found.expected is None
    assert found.received == Decimal("100.00")


def test_income_entry_rejects_negative_expected(db_session: Session, user: User) -> None:
    entry = make_income_entry(user, expected=Decimal("-1.00"))
    db_session.add(entry)
    with pytest.raises(IntegrityError, match="ck_income_entries_expected_non_negative"):
        db_session.flush()


def test_income_entry_rejects_negative_received(db_session: Session, user: User) -> None:
    entry = make_income_entry(user, expected=None, received=Decimal("-1.00"))
    db_session.add(entry)
    with pytest.raises(IntegrityError, match="ck_income_entries_received_non_negative"):
        db_session.flush()


def test_deleting_the_matched_transaction_nulls_income_entry(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    transaction = make_transaction(user, account)
    db_session.add(transaction)
    db_session.flush()
    entry = make_income_entry(user, transaction_id=transaction.id)
    db_session.add(entry)
    db_session.flush()

    db_session.delete(transaction)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(IncomeEntry, entry.id)
    assert found.transaction_id is None


def test_income_entry_index_exists(db_session: Session) -> None:
    indexes = inspect(db_session.connection()).get_indexes("income_entries")
    assert {"name": "ix_income_entries_user_id_date", "column_names": ["user_id", "date"]} in [
        {"name": ix["name"], "column_names": ix["column_names"]} for ix in indexes
    ]


def test_subscription_round_trips(db_session: Session, user: User) -> None:
    account = make_account(user)
    category = make_category(user)
    db_session.add_all([account, category])
    db_session.flush()
    subscription = make_subscription(
        user,
        account_id=account.id,
        category_id=category.id,
        next_due=date(2026, 11, 1),
        start_date=date(2026, 1, 1),
    )
    db_session.add(subscription)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Subscription, subscription.id)
    assert found.name == "Spotify"
    assert found.amount == Decimal("-11.99")
    assert isinstance(found.amount, Decimal)
    assert found.cadence is Cadence.monthly
    assert found.bucket is Bucket.personal
    assert found.active is True
    assert found.next_due == date(2026, 11, 1)
    assert found.kind is SubscriptionKind.subscription


def test_subscription_kind_accepts_bill_for_the_car_payment(
    db_session: Session, user: User
) -> None:
    subscription = make_subscription(user, name="Car payment", kind=SubscriptionKind.bill)
    db_session.add(subscription)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Subscription, subscription.id)
    assert found.kind is SubscriptionKind.bill


def test_subscription_rejects_non_negative_amount(db_session: Session, user: User) -> None:
    subscription = make_subscription(user, amount=Decimal("11.99"))
    db_session.add(subscription)
    with pytest.raises(IntegrityError, match="ck_subscriptions_amount_negative"):
        db_session.flush()


def test_subscription_rejects_end_date_before_start_date(db_session: Session, user: User) -> None:
    subscription = make_subscription(user, start_date=date(2026, 6, 1), end_date=date(2026, 1, 1))
    db_session.add(subscription)
    with pytest.raises(IntegrityError, match="ck_subscriptions_end_date_after_start_date"):
        db_session.flush()


def test_subscription_allows_end_date_without_start_date(db_session: Session, user: User) -> None:
    subscription = make_subscription(user, end_date=date(2026, 1, 1))
    db_session.add(subscription)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Subscription, subscription.id)
    assert found.end_date == date(2026, 1, 1)


def test_deleting_an_account_nulls_subscription_account_id(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    subscription = make_subscription(user, account_id=account.id)
    db_session.add(subscription)
    db_session.flush()

    db_session.delete(account)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Subscription, subscription.id)
    assert found.account_id is None


def test_deleting_a_category_nulls_subscription_category_id(
    db_session: Session, user: User
) -> None:
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    subscription = make_subscription(user, category_id=category.id)
    db_session.add(subscription)
    db_session.flush()

    db_session.delete(category)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Subscription, subscription.id)
    assert found.category_id is None


def test_subscription_index_exists(db_session: Session) -> None:
    indexes = inspect(db_session.connection()).get_indexes("subscriptions")
    expected = {
        "name": "ix_subscriptions_user_id_next_due",
        "column_names": ["user_id", "next_due"],
    }
    assert expected in [{"name": ix["name"], "column_names": ix["column_names"]} for ix in indexes]


def test_deleting_a_subscription_nulls_transaction_subscription_id(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    subscription = make_subscription(user)
    db_session.add_all([account, subscription])
    db_session.flush()
    transaction = make_transaction(user, account, subscription_id=subscription.id)
    db_session.add(transaction)
    db_session.flush()

    db_session.delete(subscription)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Transaction, transaction.id)
    assert found.subscription_id is None


def test_balance_snapshot_round_trips(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    snapshot = make_snapshot(user, account)
    db_session.add(snapshot)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(BalanceSnapshot, snapshot.id)
    assert found.balance == Decimal("1234.56")
    assert isinstance(found.balance, Decimal)
    assert found.date == date(2026, 10, 1)


def test_duplicate_snapshot_date_on_same_account_is_rejected(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    db_session.add(make_snapshot(user, account))
    db_session.flush()
    db_session.add(make_snapshot(user, account))
    with pytest.raises(IntegrityError, match="uq_balance_snapshots_account_id_date"):
        db_session.flush()


def test_same_date_on_different_accounts_is_allowed(db_session: Session, user: User) -> None:
    account_one = make_account(user, name="Checking")
    account_two = make_account(user, name="Savings")
    db_session.add_all([account_one, account_two])
    db_session.flush()
    db_session.add(make_snapshot(user, account_one))
    db_session.add(make_snapshot(user, account_two))
    db_session.flush()

    rows = db_session.scalars(
        select(BalanceSnapshot).where(BalanceSnapshot.date == date(2026, 10, 1))
    ).all()
    assert len(rows) == 2


def test_deleting_an_account_cascades_its_snapshots(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    snapshot = make_snapshot(user, account)
    db_session.add(snapshot)
    db_session.flush()

    db_session.delete(account)
    db_session.flush()
    db_session.expunge_all()

    assert db_session.scalars(select(BalanceSnapshot)).all() == []
