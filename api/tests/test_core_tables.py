import uuid
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, Category, Receipt, Transaction, User
from app.models.enums import AccountKind, Bucket
from tests.factories import make_account, make_category, make_transaction


def _receipt(user: User, transaction: Transaction, **kwargs: object) -> Receipt:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "transaction_id": transaction.id,
        "file_path": "receipts/2026/10/one.jpg",
        "mime": "image/jpeg",
        "sha256": "a" * 64,
    }
    defaults.update(kwargs)
    return Receipt(**defaults)


def test_account_round_trips(db_session: Session, user: User) -> None:
    account = make_account(
        user,
        institution="Chase",
        simplefin_account_id="acct-1",
        last4="1234",
        due_day=15,
        planned_payment=Decimal("100.00"),
        apr=Decimal("24.99"),
        kind=AccountKind.credit_card,
    )
    db_session.add(account)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Account, account.id)
    assert found.name == "Everyday Checking"
    assert found.kind is AccountKind.credit_card
    assert found.last4 == "1234"
    assert found.due_day == 15
    assert found.planned_payment == Decimal("100.00")
    assert found.apr == Decimal("24.99")
    assert found.active is True


def test_category_round_trips(db_session: Session, user: User) -> None:
    category = make_category(
        user, tax_line="travel", deductible_pct=50, monthly_budget=Decimal("400.00")
    )
    db_session.add(category)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Category, category.id)
    assert found.tax_line == "travel"
    assert found.deductible_pct == 50
    assert found.monthly_budget == Decimal("400.00")


def test_transaction_amount_round_trips_as_exact_decimal(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    transaction = make_transaction(user, account, amount=Decimal("-12.34"))
    db_session.add(transaction)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Transaction, transaction.id)
    assert found.amount == Decimal("-12.34")
    assert isinstance(found.amount, Decimal)
    assert found.bucket is Bucket.personal
    assert found.pending is False
    assert found.reviewed is False


def test_receipt_round_trips(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    transaction = make_transaction(user, account)
    db_session.add(transaction)
    db_session.flush()
    receipt = _receipt(user, transaction)
    db_session.add(receipt)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Receipt, receipt.id)
    assert found.file_path == "receipts/2026/10/one.jpg"
    assert found.mime == "image/jpeg"
    assert found.sha256 == "a" * 64
    assert found.uploaded_at is not None


def test_duplicate_external_id_on_same_account_is_rejected(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    db_session.add(make_transaction(user, account, external_id="simplefin-1"))
    db_session.flush()
    db_session.add(make_transaction(user, account, external_id="simplefin-1"))
    with pytest.raises(IntegrityError, match="uq_transactions_account_id_external_id"):
        db_session.flush()


def test_multiple_null_external_ids_are_allowed(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    db_session.add_all([make_transaction(user, account, external_id=None) for _ in range(3)])
    db_session.flush()

    rows = db_session.scalars(select(Transaction).where(Transaction.account_id == account.id)).all()
    assert len(rows) == 3


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("due_day", 0, "ck_accounts_due_day_range"),
        ("due_day", 32, "ck_accounts_due_day_range"),
        ("apr", Decimal("-1"), "ck_accounts_apr_range"),
        ("apr", Decimal("100.01"), "ck_accounts_apr_range"),
        ("last4", "12a4", "ck_accounts_last4_digits"),
        ("last4", "123", "ck_accounts_last4_digits"),
    ],
)
def test_account_check_constraints_reject_bad_values(
    db_session: Session, user: User, field: str, value: object, constraint: str
) -> None:
    account = make_account(user, **{field: value})
    db_session.add(account)
    with pytest.raises(IntegrityError, match=constraint):
        db_session.flush()


@pytest.mark.parametrize(
    ("value", "constraint"),
    [(-1, "ck_categories_deductible_pct_range"), (101, "ck_categories_deductible_pct_range")],
)
def test_category_check_constraint_rejects_bad_deductible_pct(
    db_session: Session, user: User, value: int, constraint: str
) -> None:
    category = make_category(user, deductible_pct=value)
    db_session.add(category)
    with pytest.raises(IntegrityError, match=constraint):
        db_session.flush()


def test_deleting_an_account_cascades_its_transactions(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    transaction = make_transaction(user, account)
    db_session.add(transaction)
    db_session.flush()

    db_session.delete(account)
    db_session.flush()
    db_session.expunge_all()

    assert db_session.scalars(select(Transaction)).all() == []


def test_deleting_a_category_nulls_category_id(db_session: Session, user: User) -> None:
    account = make_account(user)
    category = make_category(user)
    db_session.add_all([account, category])
    db_session.flush()
    transaction = make_transaction(user, account, category_id=category.id)
    db_session.add(transaction)
    db_session.flush()

    db_session.delete(category)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Transaction, transaction.id)
    assert found.category_id is None


def test_deleting_a_parent_cascades_its_splits(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    parent = make_transaction(user, account, amount=Decimal("-100.00"))
    db_session.add(parent)
    db_session.flush()
    split = make_transaction(user, account, amount=Decimal("-40.00"), parent_id=parent.id)
    db_session.add(split)
    db_session.flush()

    db_session.delete(parent)
    db_session.flush()
    db_session.expunge_all()

    assert db_session.scalars(select(Transaction)).all() == []


def test_deleting_a_transfer_pair_sets_the_other_side_null(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    out = make_transaction(user, account, amount=Decimal("-100.00"))
    db_session.add(out)
    db_session.flush()
    into = make_transaction(user, account, amount=Decimal("100.00"), transfer_pair_id=out.id)
    db_session.add(into)
    db_session.flush()

    db_session.delete(out)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Transaction, into.id)
    assert found.transfer_pair_id is None


def test_deleting_a_transaction_with_a_receipt_is_restricted(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    transaction = make_transaction(user, account)
    db_session.add(transaction)
    db_session.flush()
    receipt = _receipt(user, transaction)
    db_session.add(receipt)
    db_session.flush()

    db_session.delete(transaction)
    with pytest.raises(IntegrityError, match="fk_receipts_transaction_id_transactions"):
        db_session.flush()


def test_deleting_a_transaction_succeeds_once_its_receipt_moves(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    old = make_transaction(user, account)
    new = make_transaction(user, account)
    db_session.add_all([old, new])
    db_session.flush()
    receipt = _receipt(user, old)
    db_session.add(receipt)
    db_session.flush()

    receipt.transaction_id = new.id
    db_session.flush()
    db_session.delete(old)
    db_session.flush()
    db_session.expunge_all()

    assert db_session.get_one(Receipt, receipt.id).transaction_id == new.id


@pytest.mark.parametrize(
    ("index_name", "columns"),
    [
        ("ix_transactions_user_id_posted_date", ["user_id", "posted_date"]),
        ("ix_transactions_account_id_posted_date", ["account_id", "posted_date"]),
        ("ix_transactions_category_id", ["category_id"]),
        ("ix_transactions_unreviewed", ["user_id"]),
        ("ix_transactions_fingerprint", ["fingerprint"]),
    ],
)
def test_transaction_indexes_exist(
    db_session: Session, index_name: str, columns: list[str]
) -> None:
    indexes = inspect(db_session.connection()).get_indexes("transactions")
    assert {"name": index_name, "column_names": columns} in [
        {"name": ix["name"], "column_names": ix["column_names"]} for ix in indexes
    ]


def test_unreviewed_index_is_partial(db_session: Session) -> None:
    indexes = inspect(db_session.connection()).get_indexes("transactions")
    unreviewed = next(ix for ix in indexes if ix["name"] == "ix_transactions_unreviewed")
    assert unreviewed.get("dialect_options", {}).get("postgresql_where") is not None


def test_account_unique_simplefin_id_is_per_user(db_session: Session, user: User) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    db_session.add(make_account(user, simplefin_account_id="shared-id"))
    db_session.flush()
    db_session.add(make_account(other, simplefin_account_id="shared-id"))
    db_session.flush()

    rows = db_session.scalars(select(Account).where(Account.simplefin_account_id == "shared-id"))
    assert len(rows.all()) == 2
