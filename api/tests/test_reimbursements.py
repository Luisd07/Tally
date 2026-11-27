"""Acceptance tests for #64: expense-report endpoints and the owed-to-you total."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Reimbursement, Transaction, User
from app.models.enums import Bucket, IncomeType, ReimbursementStatus
from app.services.reimbursements import owed_to_you
from tests.factories import make_account, make_income_entry, make_reimbursement, make_transaction

REIMBURSEMENTS = "/api/reimbursements"


def _persist(db_session: Session, *rows: object) -> None:
    db_session.add_all(rows)
    db_session.flush()


# --- create -------------------------------------------------------------------------


def test_create_returns_201_with_an_empty_draft(
    db_client: TestClient, user: User, auth_headers: dict[str, str]
) -> None:
    r = db_client.post(REIMBURSEMENTS, json={"note": "Pit crew tools"}, headers=auth_headers)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert body["note"] == "Pit crew tools"
    assert body["submitted_date"] is None
    assert body["paid_date"] is None
    assert body["income_entry_id"] is None
    assert body["total"] == "0.00"
    assert body["transaction_count"] == 0


def test_create_attaches_the_given_transactions(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    t1 = make_transaction(user, account, amount=Decimal("-10.00"), bucket=Bucket.reimbursable)
    t2 = make_transaction(user, account, amount=Decimal("-25.50"), bucket=Bucket.reimbursable)
    _persist(db_session, t1, t2)

    r = db_client.post(
        REIMBURSEMENTS,
        json={"transaction_ids": [str(t1.id), str(t2.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total"] == "35.50"
    assert body["transaction_count"] == 2
    db_session.refresh(t1)
    db_session.refresh(t2)
    assert t1.reimbursement_id == uuid.UUID(body["id"])
    assert t2.reimbursement_id == uuid.UUID(body["id"])


def test_create_is_idempotent_and_does_not_reattach_on_replay(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    txn = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, txn)
    report_id = str(uuid.uuid4())

    first = db_client.post(REIMBURSEMENTS, json={"id": report_id}, headers=auth_headers)
    second = db_client.post(
        REIMBURSEMENTS,
        json={"id": report_id, "transaction_ids": [str(txn.id)]},
        headers=auth_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["transaction_count"] == 0
    db_session.refresh(txn)
    assert txn.reimbursement_id is None


def test_create_with_another_users_id_is_a_conflict(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = make_reimbursement(other_user)
    _persist(db_session, theirs)

    r = db_client.post(REIMBURSEMENTS, json={"id": str(theirs.id)}, headers=auth_headers)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


# --- attach rules ---------------------------------------------------------------------


def test_create_rejects_a_non_reimbursable_transaction(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    txn = make_transaction(user, account, bucket=Bucket.personal)
    _persist(db_session, txn)

    r = db_client.post(
        REIMBURSEMENTS, json={"transaction_ids": [str(txn.id)]}, headers=auth_headers
    )

    assert r.status_code == 422


def test_create_rejects_a_split_parent(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, parent)
    child = make_transaction(user, account, bucket=Bucket.reimbursable, parent_id=parent.id)
    _persist(db_session, child)

    r = db_client.post(
        REIMBURSEMENTS, json={"transaction_ids": [str(parent.id)]}, headers=auth_headers
    )

    assert r.status_code == 422


def test_create_rejects_another_users_transaction(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    other_account = make_account(other_user)
    _persist(db_session, other_account)
    theirs = make_transaction(other_user, other_account, bucket=Bucket.reimbursable)
    _persist(db_session, theirs)

    r = db_client.post(
        REIMBURSEMENTS, json={"transaction_ids": [str(theirs.id)]}, headers=auth_headers
    )

    assert r.status_code == 404


def test_attaching_a_transaction_already_in_another_report_is_a_conflict(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    txn = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, txn)
    db_client.post(REIMBURSEMENTS, json={"transaction_ids": [str(txn.id)]}, headers=auth_headers)

    r = db_client.post(
        REIMBURSEMENTS, json={"transaction_ids": [str(txn.id)]}, headers=auth_headers
    )

    assert r.status_code == 409


# --- status lifecycle ------------------------------------------------------------------


def test_submit_requires_submitted_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(user)
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}", json={"status": "submitted"}, headers=auth_headers
    )

    assert r.status_code == 422


def test_submit_with_date_succeeds(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(user)
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "submitted", "submitted_date": "2026-10-10"},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"
    assert r.json()["submitted_date"] == "2026-10-10"


def test_mark_paid_requires_paid_date(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    entry = make_income_entry(user, type=IncomeType.reimbursement, expected=Decimal("50.00"))
    _persist(db_session, report, entry)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "paid", "income_entry_id": str(entry.id)},
        headers=auth_headers,
    )

    assert r.status_code == 422


def test_mark_paid_requires_income_entry_id(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "paid", "paid_date": "2026-10-15"},
        headers=auth_headers,
    )

    assert r.status_code == 422


def test_mark_paid_succeeds_and_links_the_income_entry(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    entry = make_income_entry(user, type=IncomeType.reimbursement, expected=Decimal("50.00"))
    _persist(db_session, report, entry)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "paid", "paid_date": "2026-10-15", "income_entry_id": str(entry.id)},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paid"
    assert body["paid_date"] == "2026-10-15"
    assert body["income_entry_id"] == str(entry.id)


def test_mark_paid_with_another_users_income_entry_is_404(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    theirs = make_income_entry(other_user, type=IncomeType.reimbursement)
    _persist(db_session, report, theirs)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "paid", "paid_date": "2026-10-15", "income_entry_id": str(theirs.id)},
        headers=auth_headers,
    )

    assert r.status_code == 404


def test_mark_paid_with_the_wrong_income_type_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    entry = make_income_entry(user, type=IncomeType.event_pay, expected=Decimal("50.00"))
    _persist(db_session, report, entry)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "paid", "paid_date": "2026-10-15", "income_entry_id": str(entry.id)},
        headers=auth_headers,
    )

    assert r.status_code == 422


def test_paid_date_before_submitted_date_is_422_not_500(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    entry = make_income_entry(user, type=IncomeType.reimbursement, expected=Decimal("50.00"))
    _persist(db_session, report, entry)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "paid", "paid_date": "2026-10-05", "income_entry_id": str(entry.id)},
        headers=auth_headers,
    )

    assert r.status_code == 422


def test_skipping_a_status_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(user)
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "paid", "submitted_date": "2026-10-10", "paid_date": "2026-10-15"},
        headers=auth_headers,
    )

    assert r.status_code == 422


def test_moving_back_to_draft_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}", json={"status": "draft"}, headers=auth_headers
    )

    assert r.status_code == 422


def test_setting_status_to_its_current_value_is_a_no_op(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(user, note="original")
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"status": "draft", "note": "updated"},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["note"] == "updated"


def test_patch_null_on_required_status_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    report = make_reimbursement(user)
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}", json={"status": None}, headers=auth_headers
    )

    assert r.status_code == 422


# --- add/remove transactions ------------------------------------------------------------


def test_add_transaction_ids_while_draft_succeeds(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    txn = make_transaction(user, account, amount=Decimal("-20.00"), bucket=Bucket.reimbursable)
    _persist(db_session, txn)
    report = make_reimbursement(user)
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"add_transaction_ids": [str(txn.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["transaction_count"] == 1
    assert r.json()["total"] == "20.00"


def test_remove_transaction_ids_while_draft_succeeds(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    txn = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, txn)
    report = make_reimbursement(user)
    _persist(db_session, report)
    txn.reimbursement_id = report.id
    db_session.flush()

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"remove_transaction_ids": [str(txn.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["transaction_count"] == 0
    db_session.refresh(txn)
    assert txn.reimbursement_id is None


def test_add_transaction_ids_after_submission_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    txn = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, txn)
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    _persist(db_session, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"add_transaction_ids": [str(txn.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 422


# --- scoping -----------------------------------------------------------------------------


def test_patch_on_missing_and_another_users_id_are_identical_404s(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    theirs = make_reimbursement(other_user)
    _persist(db_session, theirs)

    missing = db_client.patch(
        f"{REIMBURSEMENTS}/{uuid.uuid4()}", json={"note": "x"}, headers=auth_headers
    )
    someone_elses = db_client.patch(
        f"{REIMBURSEMENTS}/{theirs.id}", json={"note": "x"}, headers=auth_headers
    )

    assert missing.status_code == 404
    assert someone_elses.status_code == 404
    missing_body, someone_elses_body = missing.json(), someone_elses.json()
    del missing_body["error"]["request_id"]
    del someone_elses_body["error"]["request_id"]
    assert missing_body == someone_elses_body


def test_list_only_returns_the_callers_own_reports(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    mine = make_reimbursement(user)
    theirs = make_reimbursement(other_user)
    _persist(db_session, mine, theirs)

    r = db_client.get(REIMBURSEMENTS, headers=auth_headers)

    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["items"]]
    assert ids == [str(mine.id)]


# --- list ------------------------------------------------------------------------------


def test_list_filters_by_status(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    draft = make_reimbursement(user)
    submitted = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    _persist(db_session, draft, submitted)

    r = db_client.get(REIMBURSEMENTS, params={"status": "submitted"}, headers=auth_headers)

    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["items"]]
    assert ids == [str(submitted.id)]


def test_list_totals_with_exact_literals(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    report = make_reimbursement(user)
    _persist(db_session, report)
    t1 = make_transaction(
        user,
        account,
        amount=Decimal("-12.34"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    t2 = make_transaction(
        user,
        account,
        amount=Decimal("-0.01"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    _persist(db_session, t1, t2)

    r = db_client.get(REIMBURSEMENTS, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()["items"][0]
    assert body["total"] == "12.35"
    assert body["transaction_count"] == 2


# --- owed_to_you ------------------------------------------------------------------------


def test_owed_to_you_across_statuses_and_unassigned(db_session: Session, user: User) -> None:
    account = make_account(user)
    _persist(db_session, account)

    draft_report = make_reimbursement(user)
    submitted_report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    paid_report = make_reimbursement(
        user,
        status=ReimbursementStatus.paid,
        submitted_date=date(2026, 9, 1),
        paid_date=date(2026, 9, 5),
    )
    _persist(db_session, draft_report, submitted_report, paid_report)

    unassigned = make_transaction(
        user, account, amount=Decimal("-10.00"), bucket=Bucket.reimbursable
    )
    in_draft = make_transaction(
        user,
        account,
        amount=Decimal("-20.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=draft_report.id,
    )
    in_submitted = make_transaction(
        user,
        account,
        amount=Decimal("-30.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=submitted_report.id,
    )
    in_paid = make_transaction(
        user,
        account,
        amount=Decimal("-999.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=paid_report.id,
    )
    not_reimbursable = make_transaction(
        user, account, amount=Decimal("-50.00"), bucket=Bucket.personal
    )
    pending = make_transaction(
        user, account, amount=Decimal("-15.00"), bucket=Bucket.reimbursable, pending=True
    )
    _persist(db_session, unassigned, in_draft, in_submitted, in_paid, not_reimbursable, pending)

    owed = owed_to_you(db_session, user.id)

    assert owed == Decimal("60.00")


def test_owed_to_you_excludes_split_parents(db_session: Session, user: User) -> None:
    account = make_account(user)
    _persist(db_session, account)
    parent = make_transaction(user, account, amount=Decimal("-100.00"), bucket=Bucket.reimbursable)
    _persist(db_session, parent)
    child = make_transaction(
        user, account, amount=Decimal("-100.00"), bucket=Bucket.reimbursable, parent_id=parent.id
    )
    _persist(db_session, child)

    assert owed_to_you(db_session, user.id) == Decimal("100.00")


def test_get_owed_endpoint(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    txn = make_transaction(user, account, amount=Decimal("-42.00"), bucket=Bucket.reimbursable)
    _persist(db_session, txn)

    r = db_client.get(f"{REIMBURSEMENTS}/owed", headers=auth_headers)

    assert r.status_code == 200
    assert r.json() == {"owed": "42.00"}


# --- deletion ---------------------------------------------------------------------------


def test_deleting_a_report_nulls_transaction_reimbursement_id(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    report = make_reimbursement(user)
    _persist(db_session, account, report)
    txn = make_transaction(user, account, bucket=Bucket.reimbursable, reimbursement_id=report.id)
    _persist(db_session, txn)

    db_session.delete(report)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Transaction, txn.id)
    assert found.reimbursement_id is None


def test_deleting_an_income_entry_nulls_reimbursement_income_entry_id(
    db_session: Session, user: User
) -> None:
    entry = make_income_entry(user, type=IncomeType.reimbursement)
    _persist(db_session, entry)
    report = make_reimbursement(
        user,
        status=ReimbursementStatus.paid,
        submitted_date=date(2026, 9, 1),
        paid_date=date(2026, 9, 5),
        income_entry_id=entry.id,
    )
    _persist(db_session, report)

    db_session.delete(entry)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(Reimbursement, report.id)
    assert found.income_entry_id is None


# --- report membership stays consistent with owed-to-you --------------------------------

TRANSACTIONS = "/api/transactions"


def _report_total(db_client: TestClient, headers: dict[str, str], report_id: uuid.UUID) -> str:
    r = db_client.get(REIMBURSEMENTS, headers=headers)
    assert r.status_code == 200, r.text
    return next(item for item in r.json()["items"] if item["id"] == str(report_id))["total"]


def _owed(db_client: TestClient, headers: dict[str, str]) -> str:
    return db_client.get(f"{REIMBURSEMENTS}/owed", headers=headers).json()["owed"]


def test_attaching_a_pending_transaction_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    _persist(db_session, account)
    pending = make_transaction(
        user, account, amount=Decimal("-10.00"), bucket=Bucket.reimbursable, pending=True
    )
    report = make_reimbursement(user)
    _persist(db_session, pending, report)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"add_transaction_ids": [str(pending.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 422, r.text
    db_session.refresh(pending)
    assert pending.reimbursement_id is None


def test_rebucketing_an_attached_row_keeps_total_and_owed_in_agreement(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    _persist(db_session, account, report)
    kept = make_transaction(
        user,
        account,
        amount=Decimal("-30.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    moved = make_transaction(
        user,
        account,
        amount=Decimal("-100.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    _persist(db_session, kept, moved)
    assert _report_total(db_client, auth_headers, report.id) == "130.00"
    assert _owed(db_client, auth_headers) == "130.00"

    r = db_client.patch(
        f"{TRANSACTIONS}/{moved.id}", json={"bucket": "personal"}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    assert _report_total(db_client, auth_headers, report.id) == "30.00"
    assert _owed(db_client, auth_headers) == "30.00"
    listed = db_client.get(REIMBURSEMENTS, headers=auth_headers).json()["items"][0]
    assert listed["transaction_count"] == 1


# --- splitting a transaction that is in a report ----------------------------------------


def _split(db_client: TestClient, headers: dict[str, str], txn_id: uuid.UUID, *amounts: str):
    parts = [{"amount": amount} for amount in amounts]
    return db_client.post(f"{TRANSACTIONS}/{txn_id}/split", json={"parts": parts}, headers=headers)


def test_splitting_a_row_in_a_paid_report_is_409_and_changes_nothing(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    report = make_reimbursement(
        user,
        status=ReimbursementStatus.paid,
        submitted_date=date(2026, 9, 1),
        paid_date=date(2026, 9, 5),
    )
    _persist(db_session, account, report)
    txn = make_transaction(
        user,
        account,
        amount=Decimal("-100.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    _persist(db_session, txn)

    r = _split(db_client, auth_headers, txn.id, "-60.00", "-40.00")

    assert r.status_code == 409, r.text
    assert _owed(db_client, auth_headers) == "0.00"
    assert _report_total(db_client, auth_headers, report.id) == "100.00"
    children = db_session.scalars(select(Transaction).where(Transaction.parent_id == txn.id)).all()
    assert children == []


def test_splitting_a_row_in_a_submitted_report_is_409(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    report = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    _persist(db_session, account, report)
    txn = make_transaction(
        user,
        account,
        amount=Decimal("-100.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    _persist(db_session, txn)

    assert _split(db_client, auth_headers, txn.id, "-60.00", "-40.00").status_code == 409


def test_splitting_a_row_in_a_draft_report_moves_the_link_to_reimbursable_children(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    report = make_reimbursement(user)
    other = make_reimbursement(user)
    _persist(db_session, account, report, other)
    txn = make_transaction(
        user,
        account,
        amount=Decimal("-100.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    _persist(db_session, txn)

    r = db_client.post(
        f"{TRANSACTIONS}/{txn.id}/split",
        json={"parts": [{"amount": "-60.00"}, {"amount": "-40.00", "bucket": "personal"}]},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parent"]["reimbursement_id"] is None
    by_amount = {child["amount"]: child for child in body["children"]}
    assert by_amount["-60.00"]["reimbursement_id"] == str(report.id)
    assert by_amount["-40.00"]["reimbursement_id"] is None
    assert _report_total(db_client, auth_headers, report.id) == "60.00"
    assert _owed(db_client, auth_headers) == "60.00"
    # The child is in a report now, so it can't be filed in a second one.
    again = db_client.patch(
        f"{REIMBURSEMENTS}/{other.id}",
        json={"add_transaction_ids": [by_amount["-60.00"]["id"]]},
        headers=auth_headers,
    )
    assert again.status_code == 409, again.text


def test_resplitting_a_draft_report_row_keeps_the_report_link(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    report = make_reimbursement(user)
    _persist(db_session, account, report)
    txn = make_transaction(
        user,
        account,
        amount=Decimal("-100.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    _persist(db_session, txn)
    assert _split(db_client, auth_headers, txn.id, "-60.00", "-40.00").status_code == 200

    r = _split(db_client, auth_headers, txn.id, "-70.00", "-30.00")

    assert r.status_code == 200, r.text
    assert {c["reimbursement_id"] for c in r.json()["children"]} == {str(report.id)}
    assert _report_total(db_client, auth_headers, report.id) == "100.00"


# --- reading a report's transactions ----------------------------------------------------


def test_transactions_list_filters_by_reimbursement_id_and_exposes_it(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account = make_account(user)
    report = make_reimbursement(user)
    _persist(db_session, account, report)
    first = make_transaction(user, account, bucket=Bucket.reimbursable, reimbursement_id=report.id)
    second = make_transaction(user, account, bucket=Bucket.reimbursable, reimbursement_id=report.id)
    unfiled = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, first, second, unfiled)

    r = db_client.get(
        TRANSACTIONS, params={"reimbursement_id": str(report.id)}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert {item["id"] for item in items} == {str(first.id), str(second.id)}
    assert {item["reimbursement_id"] for item in items} == {str(report.id)}
    everything = db_client.get(TRANSACTIONS, headers=auth_headers).json()["items"]
    by_id = {item["id"]: item for item in everything}
    assert by_id[str(unfiled.id)]["reimbursement_id"] is None


def test_transactions_list_with_another_users_report_id_is_empty(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    foreign_report = make_reimbursement(other_user)
    account = make_account(user)
    _persist(db_session, foreign_report, account)
    mine = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, mine)

    r = db_client.get(
        TRANSACTIONS, params={"reimbursement_id": str(foreign_report.id)}, headers=auth_headers
    )

    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


# --- income entry and date rules --------------------------------------------------------


def _paid_patch(entry_id: uuid.UUID) -> dict[str, str]:
    return {"status": "paid", "paid_date": "2026-10-15", "income_entry_id": str(entry_id)}


def test_an_income_entry_can_mark_only_one_report_paid(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user, type=IncomeType.reimbursement)
    first = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    second = make_reimbursement(
        user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 11)
    )
    _persist(db_session, entry, first, second)
    ok = db_client.patch(
        f"{REIMBURSEMENTS}/{first.id}", json=_paid_patch(entry.id), headers=auth_headers
    )
    assert ok.status_code == 200, ok.text

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{second.id}", json=_paid_patch(entry.id), headers=auth_headers
    )

    assert r.status_code == 409, r.text


def test_the_database_rejects_one_income_entry_on_two_reports(
    db_session: Session, user: User
) -> None:
    entry = make_income_entry(user, type=IncomeType.reimbursement)
    _persist(db_session, entry)
    paid = {
        "status": ReimbursementStatus.paid,
        "submitted_date": date(2026, 10, 1),
        "paid_date": date(2026, 10, 2),
        "income_entry_id": entry.id,
    }
    _persist(db_session, make_reimbursement(user, **paid))

    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(make_reimbursement(user, **paid))
        db_session.flush()


def test_a_draft_cannot_carry_dates_or_an_income_entry(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user, type=IncomeType.reimbursement)
    report = make_reimbursement(user)
    _persist(db_session, entry, report)

    for body in (
        {"paid_date": "2026-10-15"},
        {"submitted_date": "2026-10-10"},
        {"income_entry_id": str(entry.id)},
    ):
        r = db_client.patch(f"{REIMBURSEMENTS}/{report.id}", json=body, headers=auth_headers)
        assert r.status_code == 422, (body, r.text)


def test_a_paid_reports_dates_and_income_link_cannot_change(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    entry = make_income_entry(user, type=IncomeType.reimbursement)
    other_entry = make_income_entry(user, type=IncomeType.reimbursement)
    _persist(db_session, entry, other_entry)
    report = make_reimbursement(
        user,
        status=ReimbursementStatus.paid,
        submitted_date=date(2026, 10, 1),
        paid_date=date(2026, 10, 5),
        income_entry_id=entry.id,
    )
    _persist(db_session, report)

    for body in (
        {"paid_date": "2026-10-06"},
        {"submitted_date": "2026-09-30"},
        {"income_entry_id": str(other_entry.id)},
        {"income_entry_id": None},
    ):
        r = db_client.patch(f"{REIMBURSEMENTS}/{report.id}", json=body, headers=auth_headers)
        assert r.status_code == 422, (body, r.text)

    same = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"paid_date": "2026-10-05", "note": "settled"},
        headers=auth_headers,
    )
    assert same.status_code == 200, same.text
    assert same.json()["note"] == "settled"


# --- attach / detach edge cases ---------------------------------------------------------


def _account_and_report(db_session: Session, user: User, **report_kwargs: object):
    account = make_account(user)
    report = make_reimbursement(user, **report_kwargs)
    _persist(db_session, account, report)
    return account, report


def test_reattaching_a_row_already_in_this_report_is_a_no_op(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account, report = _account_and_report(db_session, user)
    txn = make_transaction(
        user,
        account,
        amount=Decimal("-20.00"),
        bucket=Bucket.reimbursable,
        reimbursement_id=report.id,
    )
    _persist(db_session, txn)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"add_transaction_ids": [str(txn.id), str(txn.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    assert r.json()["transaction_count"] == 1
    assert r.json()["total"] == "20.00"


def test_removing_unlinked_or_other_report_rows_leaves_them_alone(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account, report = _account_and_report(db_session, user)
    other = make_reimbursement(user)
    _persist(db_session, other)
    unlinked = make_transaction(user, account, bucket=Bucket.reimbursable)
    elsewhere = make_transaction(
        user, account, bucket=Bucket.reimbursable, reimbursement_id=other.id
    )
    _persist(db_session, unlinked, elsewhere)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"remove_transaction_ids": [str(unlinked.id), str(elsewhere.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    db_session.refresh(unlinked)
    db_session.refresh(elsewhere)
    assert unlinked.reimbursement_id is None
    assert elsewhere.reimbursement_id == other.id


def test_remove_transaction_ids_on_a_submitted_report_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account, report = _account_and_report(
        db_session, user, status=ReimbursementStatus.submitted, submitted_date=date(2026, 10, 10)
    )
    txn = make_transaction(user, account, bucket=Bucket.reimbursable, reimbursement_id=report.id)
    _persist(db_session, txn)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"remove_transaction_ids": [str(txn.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 422, r.text
    db_session.refresh(txn)
    assert txn.reimbursement_id == report.id


def test_foreign_transaction_id_in_add_or_remove_is_404(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    foreign_account = make_account(other_user)
    _persist(db_session, foreign_account)
    foreign = make_transaction(other_user, foreign_account, bucket=Bucket.reimbursable)
    _, report = _account_and_report(db_session, user)
    _persist(db_session, foreign)

    for key in ("add_transaction_ids", "remove_transaction_ids"):
        r = db_client.patch(
            f"{REIMBURSEMENTS}/{report.id}", json={key: [str(foreign.id)]}, headers=auth_headers
        )
        assert r.status_code == 404, (key, r.text)


def test_adding_and_removing_the_same_id_is_422(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account, report = _account_and_report(db_session, user)
    txn = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, txn)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"add_transaction_ids": [str(txn.id)], "remove_transaction_ids": [str(txn.id)]},
        headers=auth_headers,
    )

    assert r.status_code == 422, r.text
    db_session.refresh(txn)
    assert txn.reimbursement_id is None


def test_a_rejected_patch_does_not_attach_anything(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    account, report = _account_and_report(db_session, user)
    txn = make_transaction(user, account, bucket=Bucket.reimbursable)
    _persist(db_session, txn)

    r = db_client.patch(
        f"{REIMBURSEMENTS}/{report.id}",
        json={"add_transaction_ids": [str(txn.id)], "status": "paid"},
        headers=auth_headers,
    )

    assert r.status_code == 422, r.text
    db_session.refresh(txn)
    assert txn.reimbursement_id is None
