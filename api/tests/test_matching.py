"""Acceptance and unit tests for #52, the manual-to-bank matcher.

`app.services.matching.match_manual_entries` is exercised directly for the
matching-rule tests (fast, no SimpleFIN payload needed); the acceptance test goes
through `app.services.sync.run_sync` with `httpx.MockTransport`, the same way
#48's own tests do, since that's the real path a matched manual capture travels.

The surviving row keeps the *manual* row's id (`absorb`'s `keep`): the phone
outbox and a late receipt upload still hold that id, so the bank row (`drop`) is
the one that's deleted once its data has moved onto the survivor.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, Base, IncomeEntry, Receipt, Transaction, User
from app.models.enums import Bucket, RunTrigger, Source
from app.schemas.transactions import TransactionListQuery
from app.services import matching, simplefin
from app.services import sync as sync_service
from app.services import transactions as transactions_service
from tests.factories import (
    make_account,
    make_category,
    make_event,
    make_income_entry,
    make_receipt,
    make_reimbursement,
    make_rule,
    make_simplefin_connection,
    make_subscription,
    make_transaction,
)

NOW = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)
ACCESS_URL = "https://demo-user:demo-pass@bridge.simplefin.org/simplefin"


# --- acceptance: a manual capture plus its bank charge ends as one row ----------------


def _txn(txn_id: str, posted: int, amount: str) -> dict[str, Any]:
    return {"id": txn_id, "posted": posted, "amount": amount, "description": "HERTZ RENTAL CAR"}


def _account_set(account_id: str, transactions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "errlist": [],
        "connections": [],
        "accounts": [
            {
                "id": account_id,
                "name": "Everyday Checking",
                "balance": "100.00",
                "balance-date": int(NOW.timestamp()),
                "transactions": transactions,
            }
        ],
    }


def _transport(payload: Any) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


def _source(db_session: Session, id: uuid.UUID) -> Source:
    row = db_session.get(Transaction, id)
    assert row is not None
    return row.source


def test_acceptance_manual_capture_absorbs_the_bank_charge_with_its_receipt(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = make_account(user, simplefin_account_id="act-checking")
    category = make_category(user)
    db_session.add_all([account, category])
    db_session.flush()

    manual = make_transaction(
        user,
        account,
        posted_date=date(2026, 10, 14),
        amount=Decimal("-42.00"),
        description_raw="Manual capture",
        source=Source.manual,
        category_id=category.id,
        bucket=Bucket.business,
        notes="Lunch with a client",
    )
    db_session.add(manual)
    db_session.flush()
    receipt = make_receipt(user, manual)
    connection = make_simplefin_connection(
        user, access_url_encrypted=simplefin.encrypt_access_url(settings, ACCESS_URL)
    )
    db_session.add_all([receipt, connection])
    db_session.flush()

    bank_posted = int(datetime(2026, 10, 16, tzinfo=UTC).timestamp())  # 2 days after the capture
    payload = _account_set("act-checking", [_txn("txn-1", bank_posted, "-42.00")])

    run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.status.value == "ok"
    remaining = db_session.scalars(
        select(Transaction).where(Transaction.account_id == account.id)
    ).all()
    assert len(remaining) == 1
    survivor = remaining[0]
    assert survivor.id == manual.id  # the manual row's id survives, not the bank's
    assert survivor.external_id == "txn-1"
    assert survivor.source == Source.simplefin
    assert survivor.amount == Decimal("-42.00")
    assert survivor.category_id == category.id
    assert survivor.bucket == Bucket.business
    assert survivor.notes == "Lunch with a client"

    moved_receipt = db_session.get(Receipt, receipt.id)
    assert moved_receipt is not None
    assert moved_receipt.transaction_id == manual.id


def test_a_charge_that_posts_on_a_later_sync_still_matches_the_manual_capture(
    db_session: Session, user: User, settings: Settings
) -> None:
    """MV-1: most cards report a charge as pending before it posts, and keep the
    same SimpleFIN id either way. The first sync must not match it (`#50`'s manual
    capture should survive a pending charge), but once the second sync reports it
    posted, it has to match -- `IngestResult.newly_posted_ids` exists precisely
    for this flip, not just a brand-new posted row.
    """
    account = make_account(user, simplefin_account_id="act-checking")
    db_session.add(account)
    db_session.flush()
    manual = make_transaction(
        user,
        account,
        posted_date=date(2026, 10, 14),
        amount=Decimal("-42.00"),
        description_raw="Manual capture",
        source=Source.manual,
    )
    connection = make_simplefin_connection(
        user, access_url_encrypted=simplefin.encrypt_access_url(settings, ACCESS_URL)
    )
    db_session.add_all([manual, connection])
    db_session.flush()

    pending_posted = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp())
    pending_payload = _account_set(
        "act-checking",
        [
            {
                "id": "txn-1",
                "posted": pending_posted,
                "pending": True,
                "amount": "-42.00",
                "description": "HERTZ RENTAL CAR",
            }
        ],
    )
    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(pending_payload),
    )

    # The manual row must survive the pending charge untouched.
    still_manual = db_session.get(Transaction, manual.id)
    assert still_manual is not None
    assert still_manual.source == Source.manual

    posted_payload = _account_set("act-checking", [_txn("txn-1", pending_posted, "-42.00")])
    sync_service.run_sync(
        db_session,
        user.id,
        NOW + timedelta(hours=1),
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(posted_payload),
    )

    remaining = db_session.scalars(
        select(Transaction).where(Transaction.account_id == account.id)
    ).all()
    assert len(remaining) == 1
    survivor = remaining[0]
    assert survivor.id == manual.id
    assert survivor.external_id == "txn-1"
    assert survivor.pending is False


def test_reimbursable_rule_absorbed_by_a_personal_manual_capture_drops_the_push_suffix(
    db_session: Session, user: User, settings: Settings
) -> None:
    """#51/#52 interaction (`_post_process`'s order: rules, then this matcher,
    then `_count_reimbursable`): a rule flags the new bank row `reimbursable`
    before the matcher runs, but the matcher then absorbs it into the manual
    capture's own `bucket=personal` -- untouched, since `bucket` isn't one of
    `absorb`'s fill-if-null fields. The bank row's id (what the rule flagged)
    no longer exists by the time `_count_reimbursable` runs, so the push must
    not claim anything looks reimbursable.
    """
    account = make_account(user, simplefin_account_id="act-checking")
    db_session.add(account)
    db_session.flush()
    rule = make_rule(user, set_merchant=None, set_bucket=Bucket.reimbursable)
    manual = make_transaction(
        user,
        account,
        posted_date=date(2026, 10, 14),
        amount=Decimal("-42.00"),
        description_raw="Manual capture",
        source=Source.manual,
        bucket=Bucket.personal,
    )
    connection = make_simplefin_connection(
        user, access_url_encrypted=simplefin.encrypt_access_url(settings, ACCESS_URL)
    )
    db_session.add_all([rule, manual, connection])
    db_session.flush()

    bank_posted = int(datetime(2026, 10, 16, tzinfo=UTC).timestamp())
    payload = _account_set("act-checking", [_txn("txn-1", bank_posted, "-42.00")])

    _run, push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    survivor = db_session.get(Transaction, manual.id)
    assert survivor is not None
    assert survivor.bucket == Bucket.personal  # the manual capture's own bucket wins
    assert push is not None
    assert "reimbursable" not in push.body


# --- match_manual_entries: the matching rule itself ------------------------------------


def test_amount_off_by_one_cent_does_not_match(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.01"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 0
    assert _source(db_session, manual.id) == Source.manual
    assert _source(db_session, bank.id) == Source.simplefin


def test_date_four_days_apart_does_not_match(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 12),
    )
    db_session.add_all([bank, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 0
    assert _source(db_session, manual.id) == Source.manual


def test_different_account_does_not_match(db_session: Session, user: User) -> None:
    account_a = make_account(user, name="A")
    account_b = make_account(user, name="B")
    db_session.add_all([account_a, account_b])
    db_session.flush()
    bank = make_transaction(
        user,
        account_a,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    manual = make_transaction(
        user,
        account_b,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 0
    assert _source(db_session, manual.id) == Source.manual


def test_two_manual_rows_the_closest_date_wins(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    far = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 14),
    )
    close = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, far, close])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 1
    assert result.survivor_ids == [close.id]
    survivor = db_session.get(Transaction, close.id)  # the closer date, absorbed the bank row
    assert survivor is not None
    assert survivor.source == Source.simplefin
    assert survivor.external_id == "txn-1"
    far_row = db_session.get(Transaction, far.id)  # the other one, untouched
    assert far_row is not None
    assert far_row.source == Source.manual
    assert db_session.get(Transaction, bank.id) is None  # absorbed and deleted


def test_created_at_breaks_a_tie_on_equally_close_dates(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    # Both 2 days from the bank's posted_date -- one earlier, one later -- so only
    # `created_at` can break the tie.
    older = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 14),
        created_at=datetime(2026, 10, 14, 8, 0, tzinfo=UTC),
    )
    newer = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 18),
        created_at=datetime(2026, 10, 18, 8, 0, tzinfo=UTC),
    )
    db_session.add_all([bank, older, newer])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 1
    survivor = db_session.get(Transaction, older.id)
    assert survivor is not None
    assert survivor.source == Source.simplefin
    newer_row = db_session.get(Transaction, newer.id)
    assert newer_row is not None
    assert newer_row.source == Source.manual


def test_two_bank_rows_competing_for_one_manual_row_only_one_wins(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    earlier_bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-earlier",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 14),
    )
    later_bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-later",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([earlier_bank, later_bank, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [earlier_bank.id, later_bank.id])

    assert result.matched_count == 1
    # `_bank_candidates` orders by (posted_date, id): `earlier_bank` is considered
    # first and takes the only manual row available.
    survivor = db_session.get(Transaction, manual.id)
    assert survivor is not None
    assert survivor.external_id == "txn-earlier"
    assert db_session.get(Transaction, earlier_bank.id) is None  # absorbed
    later_row = db_session.get(Transaction, later_bank.id)
    assert later_row is not None
    assert later_row.source == Source.simplefin  # left as an ordinary bank row


def test_pending_bank_row_does_not_match(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
        pending=True,
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 0
    assert _source(db_session, manual.id) == Source.manual


def test_other_users_manual_row_never_matches(
    db_session: Session, user: User, other_user: User
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    # Same account id as `bank`, despite belonging to `other_user` -- the DB doesn't
    # enforce same-user FKs (`.claude/rules/api.md`), so this is what proves the
    # matcher's own `user_id` scoping, not just the account filter, keeps it out.
    foreign_manual = make_transaction(
        other_user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, foreign_manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 0
    assert _source(db_session, foreign_manual.id) == Source.manual


def test_manual_split_parent_is_not_matched(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    manual_parent = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, manual_parent])
    db_session.flush()
    child = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-20.00"),
        posted_date=date(2026, 10, 15),
        parent_id=manual_parent.id,
    )
    db_session.add(child)
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 0
    assert _source(db_session, manual_parent.id) == Source.manual


def test_split_child_bank_row_is_not_matched(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank_parent = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-parent",
        amount=Decimal("-62.00"),
        posted_date=date(2026, 10, 16),
    )
    db_session.add(bank_parent)
    db_session.flush()
    bank_child = make_transaction(
        user,
        account,
        source=Source.simplefin,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
        parent_id=bank_parent.id,
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank_child, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank_child.id])

    assert result.matched_count == 0
    assert _source(db_session, manual.id) == Source.manual


# --- absorb: copy semantics and inbound references ------------------------------------


def test_bank_row_fills_only_the_fields_the_manual_row_left_null(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    category = make_category(user, name="Bank Category")
    event = make_event(user)
    db_session.add_all([account, category, event])
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
        category_id=category.id,
        event_id=event.id,
        merchant="Bank Merchant",
        notes="bank notes",
        reviewed=True,
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
        category_id=None,
        event_id=None,
        merchant=None,
        notes=None,
        reviewed=False,
    )
    db_session.add_all([bank, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 1
    survivor = db_session.get(Transaction, manual.id)
    assert survivor is not None
    # Every field the manual row left null is filled from the bank row.
    assert survivor.category_id == category.id
    assert survivor.event_id == event.id
    assert survivor.merchant == "Bank Merchant"
    assert survivor.notes == "bank notes"
    # `reviewed` is the manual row's own flag, untouched by the bank's value.
    assert survivor.reviewed is False


def test_absorbing_keeps_a_subscription_link_from_the_bank_row(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    subscription = make_subscription(user)
    db_session.add_all([account, subscription])
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-11.99"),
        posted_date=date(2026, 10, 16),
        subscription_id=subscription.id,
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-11.99"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, manual])
    db_session.flush()

    matching.match_manual_entries(db_session, user.id, [bank.id])

    survivor = db_session.get(Transaction, manual.id)
    assert survivor is not None
    assert survivor.subscription_id == subscription.id


def test_absorbing_keeps_a_report_link_from_either_row(db_session: Session, user: User) -> None:
    """`reimbursement_id` is fill-if-null: a link on the dropped bank row survives
    when the kept manual row has none, and the kept row's own link wins otherwise.
    """
    account = make_account(user)
    kept_report = make_reimbursement(user)
    dropped_report = make_reimbursement(user)
    db_session.add_all([account, kept_report, dropped_report])
    db_session.flush()
    linked_bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
        reimbursement_id=dropped_report.id,
    )
    unlinked_manual = make_transaction(
        user,
        account,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    other_bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-2",
        amount=Decimal("-77.00"),
        posted_date=date(2026, 10, 16),
        reimbursement_id=dropped_report.id,
    )
    linked_manual = make_transaction(
        user,
        account,
        amount=Decimal("-77.00"),
        posted_date=date(2026, 10, 15),
        reimbursement_id=kept_report.id,
    )
    db_session.add_all([linked_bank, unlinked_manual, other_bank, linked_manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [linked_bank.id, other_bank.id])

    assert result.matched_count == 2
    filled = db_session.get(Transaction, unlinked_manual.id)
    kept = db_session.get(Transaction, linked_manual.id)
    assert filled is not None and kept is not None
    assert filled.reimbursement_id == dropped_report.id
    assert kept.reimbursement_id == kept_report.id


def test_manual_rows_own_values_win_over_the_banks(db_session: Session, user: User) -> None:
    account = make_account(user)
    bank_category = make_category(user, name="Bank Category")
    manual_category = make_category(user, name="Manual Category")
    db_session.add_all([account, bank_category, manual_category])
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
        category_id=bank_category.id,
        merchant="Bank Merchant",
        notes="bank notes",
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
        category_id=manual_category.id,
        merchant="Manual Merchant",
        notes="manual notes",
    )
    db_session.add_all([bank, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 1
    survivor = db_session.get(Transaction, manual.id)
    assert survivor is not None
    assert survivor.category_id == manual_category.id
    assert survivor.merchant == "Manual Merchant"
    assert survivor.notes == "manual notes"


def test_several_receipts_on_the_bank_row_all_move_to_the_survivor(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, manual])
    db_session.flush()
    receipts = [
        make_receipt(user, bank, file_path=f"2026/10/{i}.jpg", sha256=str(i) * 64) for i in range(3)
    ]
    db_session.add_all(receipts)
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 1
    for receipt in receipts:
        moved = db_session.get(Receipt, receipt.id)
        assert moved is not None
        assert moved.transaction_id == manual.id


def test_income_entry_linked_to_the_bank_row_follows_it_to_the_survivor(
    db_session: Session, user: User
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("500.00"),
        posted_date=date(2026, 10, 16),
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("500.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, manual])
    db_session.flush()
    income = make_income_entry(user, transaction_id=bank.id)
    db_session.add(income)
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 1
    refreshed = db_session.get(IncomeEntry, income.id)
    assert refreshed is not None
    assert refreshed.transaction_id == manual.id


def test_transfer_pair_id_moves_in_both_directions(db_session: Session, user: User) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    other_side = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-other-side",
        amount=Decimal("500.00"),
    )
    db_session.add(other_side)
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
        transfer_pair_id=other_side.id,
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank, manual])
    db_session.flush()
    other_side.transfer_pair_id = bank.id
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 1
    survivor = db_session.get(Transaction, manual.id)
    other_side_refreshed = db_session.get(Transaction, other_side.id)
    assert survivor is not None
    assert other_side_refreshed is not None
    # The survivor inherits the bank row's own pointer...
    assert survivor.transfer_pair_id == other_side.id
    # ...and the pointer the other side held back at the bank row is repointed.
    assert other_side_refreshed.transfer_pair_id == manual.id


def test_transfer_pair_asymmetry_skips_the_match_instead_of_breaking_a_pair(
    db_session: Session, user: User
) -> None:
    """Round 2 adversarial finding: if `keep` and `drop` both already have their
    own `transfer_pair_id`, absorbing would have to pick one and silently drop
    the other's half of its pair. Skip the match instead -- both rows are left
    exactly as they were.
    """
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    other_side_of_bank = make_transaction(
        user, account, source=Source.simplefin, external_id="txn-other-1", amount=Decimal("500.00")
    )
    other_side_of_manual = make_transaction(
        user, account, source=Source.simplefin, external_id="txn-other-2", amount=Decimal("600.00")
    )
    db_session.add_all([other_side_of_bank, other_side_of_manual])
    db_session.flush()
    bank = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-1",
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 16),
        transfer_pair_id=other_side_of_bank.id,
    )
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 10, 15),
        transfer_pair_id=other_side_of_manual.id,
    )
    db_session.add_all([bank, manual])
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 0
    bank_row = db_session.get(Transaction, bank.id)
    manual_row = db_session.get(Transaction, manual.id)
    assert bank_row is not None
    assert bank_row.source == Source.simplefin
    assert bank_row.transfer_pair_id == other_side_of_bank.id  # untouched
    assert manual_row is not None
    assert manual_row.source == Source.manual
    assert manual_row.transfer_pair_id == other_side_of_manual.id  # untouched


# --- savepoint behavior ------------------------------------------------------------------


def test_a_failing_pair_rolls_back_its_own_savepoint(
    db_session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    bank_1 = Transaction(
        id=uuid.UUID(int=1),
        user_id=user.id,
        account_id=account.id,
        posted_date=date(2026, 10, 16),
        amount=Decimal("-10.00"),
        description_raw="FIRST",
        source=Source.simplefin,
        external_id="txn-1",
    )
    manual_1 = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-10.00"),
        posted_date=date(2026, 10, 15),
    )
    bank_2 = Transaction(
        id=uuid.UUID(int=2),
        user_id=user.id,
        account_id=account.id,
        posted_date=date(2026, 10, 16),
        amount=Decimal("-20.00"),
        description_raw="SECOND",
        source=Source.simplefin,
        external_id="txn-2",
    )
    manual_2 = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-20.00"),
        posted_date=date(2026, 10, 15),
    )
    db_session.add_all([bank_1, manual_1, bank_2, manual_2])
    db_session.flush()

    original = matching.absorb
    calls = 0

    def _flaky(db: Session, *, keep: Transaction, drop: Transaction) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            # Make real progress, then blow up before it completes.
            db.delete(drop)
            db.flush()
            raise RuntimeError("boom")
        original(db, keep=keep, drop=drop)

    monkeypatch.setattr(matching, "absorb", _flaky)

    with pytest.raises(RuntimeError):
        matching.match_manual_entries(db_session, user.id, [bank_1.id, bank_2.id])

    # The first pair (processed in `(posted_date, id)` order, so `bank_1` first)
    # went through before the failure.
    assert db_session.get(Transaction, bank_1.id) is None
    survivor_1 = db_session.get(Transaction, manual_1.id)
    assert survivor_1 is not None
    assert survivor_1.source == Source.simplefin

    # The second pair's savepoint rolled back the delete it had already made.
    assert db_session.get(Transaction, bank_2.id) is not None
    survivor_2 = db_session.get(Transaction, manual_2.id)
    assert survivor_2 is not None
    assert survivor_2.source == Source.manual


@pytest.fixture
def committed_account(engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """A user and account committed to the real engine, visible across sessions."""
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    with Session(engine) as setup:
        race_user = User(id=user_id, display_name="Race")
        setup.add(race_user)
        setup.flush()
        setup.add(make_account(race_user, id=account_id))
        setup.commit()
    try:
        yield user_id, account_id
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_skip_locked_leaves_a_concurrently_patched_manual_row_unmatched(
    engine: Engine, committed_account: tuple[uuid.UUID, uuid.UUID]
) -> None:
    user_id, account_id = committed_account
    manual_id = uuid.uuid4()
    bank_id = uuid.uuid4()
    with Session(engine) as setup:
        setup.add(
            Transaction(
                id=manual_id,
                user_id=user_id,
                account_id=account_id,
                posted_date=date(2026, 10, 15),
                amount=Decimal("-42.00"),
                description_raw="Manual capture",
                source=Source.manual,
            )
        )
        setup.add(
            Transaction(
                id=bank_id,
                user_id=user_id,
                account_id=account_id,
                posted_date=date(2026, 10, 16),
                amount=Decimal("-42.00"),
                description_raw="HERTZ RENTAL CAR",
                source=Source.simplefin,
                external_id="txn-1",
            )
        )
        setup.commit()

    with Session(engine) as locker, Session(engine) as matcher:
        locked = locker.scalars(
            select(Transaction).where(Transaction.id == manual_id).with_for_update()
        ).one()
        assert locked is not None  # holds the row lock, uncommitted on purpose

        result = matching.match_manual_entries(matcher, user_id, [bank_id])

        assert result.matched_count == 0  # skipped, not blocked
        locker.rollback()
        matcher.commit()

    with Session(engine) as verify:
        remaining_manual = verify.get(Transaction, manual_id)
        remaining_bank = verify.get(Transaction, bank_id)
        assert remaining_manual is not None
        assert remaining_manual.source == Source.manual
        assert remaining_bank is not None
        assert remaining_bank.source == Source.simplefin


def test_skip_locked_never_loses_a_concurrent_patch_to_the_bank_row(
    engine: Engine, committed_account: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Round 2 adversarial finding: `_bank_candidates` reads the bank row with a
    plain, unlocked `SELECT`. Without `lock_bank_row` re-locking and refreshing
    it right before `absorb`, a `PATCH` to the bank row committed in the window
    between that read and the delete would be silently lost along with the rest
    of `drop`. Locking it (`FOR UPDATE SKIP LOCKED`, same choice as the manual
    side) means the pair is skipped instead -- the PATCH's own commit always
    lands, either before this pass ever sees the row or after this pass has
    already backed off.
    """
    user_id, account_id = committed_account
    manual_id = uuid.uuid4()
    bank_id = uuid.uuid4()
    with Session(engine) as setup:
        setup.add(
            Transaction(
                id=manual_id,
                user_id=user_id,
                account_id=account_id,
                posted_date=date(2026, 10, 15),
                amount=Decimal("-42.00"),
                description_raw="Manual capture",
                source=Source.manual,
            )
        )
        setup.add(
            Transaction(
                id=bank_id,
                user_id=user_id,
                account_id=account_id,
                posted_date=date(2026, 10, 16),
                amount=Decimal("-42.00"),
                description_raw="HERTZ RENTAL CAR",
                source=Source.simplefin,
                external_id="txn-1",
            )
        )
        setup.commit()

    with Session(engine) as locker, Session(engine) as matcher:
        locked_bank = locker.scalars(
            select(Transaction).where(Transaction.id == bank_id).with_for_update()
        ).one()
        locked_bank.merchant = "Set by a concurrent PATCH"  # uncommitted on purpose

        result = matching.match_manual_entries(matcher, user_id, [bank_id])

        assert result.matched_count == 0  # skipped, not blocked -- and nothing lost
        locker.commit()
        matcher.commit()

    with Session(engine) as verify:
        bank_row = verify.get(Transaction, bank_id)
        manual_row = verify.get(Transaction, manual_id)
        assert bank_row is not None
        assert bank_row.source == Source.simplefin  # never absorbed
        assert bank_row.merchant == "Set by a concurrent PATCH"  # the PATCH's write survived
        assert manual_row is not None
        assert manual_row.source == Source.manual


# --- GET /api/transactions?unmatched_manual=true (#53's Inbox flag) -------------------


def test_unmatched_manual_filter_flags_rows_at_least_seven_days_old(
    db_session: Session, user: User
) -> None:
    account = make_account(user, simplefin_account_id="act-checking")
    db_session.add(account)
    db_session.flush()
    old_enough = make_transaction(
        user, account, source=Source.manual, posted_date=NOW.date() - timedelta(days=7)
    )
    too_recent = make_transaction(
        user, account, source=Source.manual, posted_date=NOW.date() - timedelta(days=6)
    )
    old_bank_row = make_transaction(
        user,
        account,
        source=Source.simplefin,
        external_id="txn-old",
        posted_date=NOW.date() - timedelta(days=30),
    )
    db_session.add_all([old_enough, too_recent, old_bank_row])
    db_session.flush()

    page = transactions_service.list_transactions(
        db_session, user.id, TransactionListQuery(unmatched_manual=True), NOW
    )

    assert [item.id for item in page.items] == [old_enough.id]


def test_unmatched_manual_filter_excludes_unsynced_accounts_and_split_children(
    db_session: Session, user: User
) -> None:
    synced_account = make_account(user, simplefin_account_id="act-checking")
    cash_account = make_account(user, name="Cash", simplefin_account_id=None)
    db_session.add_all([synced_account, cash_account])
    db_session.flush()
    on_synced_account = make_transaction(
        user, synced_account, source=Source.manual, posted_date=NOW.date() - timedelta(days=10)
    )
    on_cash_account = make_transaction(
        user, cash_account, source=Source.manual, posted_date=NOW.date() - timedelta(days=10)
    )
    db_session.add_all([on_synced_account, on_cash_account])
    db_session.flush()
    split_child = make_transaction(
        user,
        synced_account,
        source=Source.manual,
        posted_date=NOW.date() - timedelta(days=10),
        parent_id=on_synced_account.id,
    )
    db_session.add(split_child)
    db_session.flush()

    page = transactions_service.list_transactions(
        db_session, user.id, TransactionListQuery(unmatched_manual=True), NOW
    )

    assert [item.id for item in page.items] == [on_synced_account.id]


# --- schema guard: every inbound FK into transactions.id is accounted for -------------


def test_every_inbound_transaction_fk_is_handled() -> None:
    """Round 2 adversarial finding: walks every FK in the schema that targets
    `transactions.id` and asserts each one is either repointed by `absorb`
    (`matching.REPOINTED_FKS`) or carries a documented reason it's safe to leave
    alone (`matching.FK_ALLOWLIST`). A future inbound FK -- some new table
    referencing a transaction -- fails this test until it's assigned to one of
    the two, instead of silently losing data the way the original
    `income_entries` gap did before this round's fix.
    """
    found: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == "transactions" and fk.column.name == "id":
                    found.add((table.name, column.name))

    repointed = {
        (model.__tablename__, column_name) for model, column_name in matching.REPOINTED_FKS
    }
    allowlisted = set(matching.FK_ALLOWLIST)

    # Catch a stale entry (the column got renamed or dropped) as well as a
    # missing one.
    assert repointed <= found
    assert allowlisted <= found
    unhandled = found - repointed - allowlisted
    assert not unhandled, f"unhandled inbound FK(s) into transactions.id: {unhandled}"


# --- imported statement rows (#54) ----------------------------------------------------


def _csv_scenario(
    db_session: Session, user: User, csv_dates: list[date], bank_dates: list[date]
) -> tuple[list[Transaction], list[Transaction]]:
    account = make_account(user)
    db_session.add(account)
    db_session.flush()
    csv_rows = [
        make_transaction(
            user, account, source=Source.csv, amount=Decimal("-42.00"), posted_date=posted
        )
        for posted in csv_dates
    ]
    bank_rows = [
        make_transaction(
            user,
            account,
            source=Source.simplefin,
            external_id=f"txn-{i}",
            amount=Decimal("-42.00"),
            posted_date=posted,
        )
        for i, posted in enumerate(bank_dates)
    ]
    db_session.add_all([*csv_rows, *bank_rows])
    db_session.flush()
    return csv_rows, bank_rows


def test_a_bank_row_is_absorbed_into_its_one_unambiguous_csv_row(
    db_session: Session, user: User
) -> None:
    [csv_row], [bank] = _csv_scenario(db_session, user, [date(2026, 3, 10)], [date(2026, 3, 12)])
    bank_id = bank.id

    result = matching.match_manual_entries(db_session, user.id, [bank_id])

    assert result.survivor_ids == [csv_row.id]
    assert db_session.get(Transaction, bank_id) is None
    assert csv_row.source == Source.simplefin
    assert csv_row.external_id == "txn-0"
    assert csv_row.posted_date == date(2026, 3, 12)


def test_two_csv_candidates_for_one_bank_row_are_left_alone(
    db_session: Session, user: User
) -> None:
    csv_rows, [bank] = _csv_scenario(
        db_session, user, [date(2026, 3, 10), date(2026, 3, 11)], [date(2026, 3, 12)]
    )

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.matched_count == 0
    assert all(row.source == Source.csv for row in csv_rows)


def test_a_csv_row_with_two_bank_candidates_is_left_alone(db_session: Session, user: User) -> None:
    [csv_row], banks = _csv_scenario(
        db_session, user, [date(2026, 3, 10)], [date(2026, 3, 11), date(2026, 3, 12)]
    )

    result = matching.match_manual_entries(db_session, user.id, [b.id for b in banks])

    assert result.matched_count == 0
    assert csv_row.source == Source.csv


def test_a_manual_row_wins_over_a_csv_row(db_session: Session, user: User) -> None:
    [csv_row], [bank] = _csv_scenario(db_session, user, [date(2026, 3, 10)], [date(2026, 3, 12)])
    account = db_session.get(Account, bank.account_id)
    assert account is not None
    manual = make_transaction(
        user,
        account,
        source=Source.manual,
        amount=Decimal("-42.00"),
        posted_date=date(2026, 3, 11),
    )
    db_session.add(manual)
    db_session.flush()

    result = matching.match_manual_entries(db_session, user.id, [bank.id])

    assert result.survivor_ids == [manual.id]
    assert csv_row.source == Source.csv


def test_csv_rows_are_paired_with_bank_rows_by_exact_date(db_session: Session, user: User) -> None:
    csv_rows, banks = _csv_scenario(
        db_session,
        user,
        [date(2026, 3, 10), date(2026, 3, 12)],
        [date(2026, 3, 10), date(2026, 3, 12)],
    )

    result = matching.match_manual_entries(db_session, user.id, [b.id for b in banks])

    assert result.matched_count == 2
    assert {row.id for row in csv_rows} == set(result.survivor_ids)
    assert [row.posted_date for row in csv_rows] == [date(2026, 3, 10), date(2026, 3, 12)]


def test_a_csv_row_before_the_window_start_is_left_alone(db_session: Session, user: User) -> None:
    [csv_row], [bank] = _csv_scenario(db_session, user, [date(2026, 3, 10)], [date(2026, 3, 12)])

    result = matching.match_manual_entries(
        db_session, user.id, [bank.id], window_start=date(2026, 3, 9)
    )

    assert result.matched_count == 0
    assert csv_row.source == Source.csv
