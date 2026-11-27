"""#56: a new charge is matched to an active subscription by merchant and amount.

The acceptance scenario drives `app.services.sync.run_sync` with a hand-built
SimpleFIN payload (`tests.test_reconcile._sync`); the eligibility rules call
`app.services.subscription_matching.match_subscriptions` on seeded rows. `NOW` is
2026-10-16.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, Subscription, Transaction, User
from app.models.enums import Bucket, Cadence, ReimbursementStatus, RunTrigger, Source
from app.services import subscription_matching
from tests.factories import (
    make_account,
    make_reimbursement,
    make_rule,
    make_subscription,
    make_transaction,
)
from tests.test_reconcile import OLD, _epoch, _pending, _posted, _rows, _setup, _sync
from tests.test_sync import _account, _txn

DAY = date(2026, 10, 14)


def _sub(db_session: Session, user: User, **kwargs: object) -> Subscription:
    defaults: dict[str, object] = {"next_due": DAY}
    defaults.update(kwargs)
    sub = make_subscription(user, **defaults)
    db_session.add(sub)
    db_session.flush()
    return sub


def _charge(
    db_session: Session,
    user: User,
    account: Account,
    amount: str = "-11.99",
    external_id: str = "ext-1",
    **kwargs: object,
) -> Transaction:
    defaults: dict[str, object] = {
        "source": Source.simplefin,
        "external_id": external_id,
        "amount": Decimal(amount),
        "posted_date": DAY,
        "description_raw": "SPOTIFY USA 8776",
    }
    defaults.update(kwargs)
    txn = make_transaction(user, account, **defaults)
    db_session.add(txn)
    db_session.flush()
    return txn


@pytest.fixture
def account(db_session: Session, user: User) -> Account:
    acct = make_account(user)
    db_session.add(acct)
    db_session.flush()
    return acct


def _warnings() -> tuple[list[str], int]:
    messages: list[str] = []
    return messages, logger.add(messages.append, level="WARNING")


# --- AC-1 --------------------------------------------------------------------------------


def test_a_monthly_charge_links_and_advances_next_due_one_month(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    sub = _sub(db_session, user, name="spotify")
    txn = {**_posted("s-1", DAY, "-11.99"), "description": "SPOTIFY USA 8776"}

    _sync(db_session, user, settings, [_account(transactions=[txn])])

    assert _rows(db_session, account)[0].subscription_id == sub.id
    db_session.refresh(sub)
    assert sub.next_due == date(2026, 11, 14)


def test_amount_within_ten_percent_matches_and_beyond_does_not(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, amount=Decimal("-100.00"), name="Spotify")
    near = _charge(db_session, user, account, "-110.00", "near")
    far = _charge(db_session, user, account, "-110.01", "far")

    assert subscription_matching.match_subscriptions(db_session, user.id, [far.id]) == 0
    assert far.subscription_id is None
    assert subscription_matching.match_subscriptions(db_session, user.id, [near.id]) == 1
    assert near.subscription_id == sub.id


def test_merchant_match_is_case_insensitive_and_normalized(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, name="  apple   music ")
    txn = _charge(db_session, user, account, merchant="Apple Music", description_raw="APL*ITUNES")

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1
    assert txn.subscription_id == sub.id


def test_a_charge_on_any_account_matches(db_session: Session, user: User, account: Account) -> None:
    other = make_account(user, name="Card")
    db_session.add(other)
    db_session.flush()
    sub = _sub(db_session, user, account_id=account.id)
    txn = _charge(db_session, user, other)

    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    assert txn.subscription_id == sub.id


def test_next_due_advances_per_cadence_with_month_end_clamping() -> None:
    advance = subscription_matching.advance_next_due
    assert advance(date(2026, 1, 31), Cadence.monthly) == date(2026, 2, 28)
    assert advance(date(2028, 1, 31), Cadence.monthly) == date(2028, 2, 29)
    assert advance(date(2026, 12, 15), Cadence.monthly) == date(2027, 1, 15)
    assert advance(date(2028, 2, 29), Cadence.yearly) == date(2029, 2, 28)
    assert advance(date(2026, 10, 14), Cadence.weekly) == date(2026, 10, 21)
    assert advance(date(2026, 11, 30), Cadence.quarterly) == date(2027, 2, 28)


def test_a_subscription_without_next_due_starts_from_the_charge_date(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=None)
    txn = _charge(db_session, user, account)

    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    assert sub.next_due == date(2026, 11, 14)


# --- AC-2 --------------------------------------------------------------------------------


def test_two_subscriptions_fitting_one_charge_link_neither(
    db_session: Session, user: User, account: Account
) -> None:
    first = _sub(db_session, user, name="Spotify")
    second = _sub(db_session, user, name="Spotify USA", amount=Decimal("-12.50"))
    txn = _charge(db_session, user, account)
    messages, sink = _warnings()

    try:
        assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 0
    finally:
        logger.remove(sink)

    assert txn.subscription_id is None
    assert first.next_due == second.next_due == DAY
    assert any("fits 2 subscriptions" in message for message in messages)


def test_two_charges_for_one_subscription_link_neither(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user)
    one = _charge(db_session, user, account, external_id="a")
    two = _charge(db_session, user, account, external_id="b")
    messages, sink = _warnings()

    try:
        assert subscription_matching.match_subscriptions(db_session, user.id, [one.id, two.id]) == 0
    finally:
        logger.remove(sink)

    assert one.subscription_id is None and two.subscription_id is None
    assert sub.next_due == DAY
    assert any("fits 2 charges" in message for message in messages)


def test_an_already_linked_charge_is_not_matched_again(
    db_session: Session, user: User, account: Account
) -> None:
    mine = _sub(db_session, user, name="Other", amount=Decimal("-50.00"))
    sub = _sub(db_session, user)
    txn = _charge(db_session, user, account, subscription_id=mine.id)

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 0
    assert txn.subscription_id == mine.id
    assert sub.next_due == DAY


def test_pending_split_transfer_and_inflow_rows_are_never_matched(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user)
    pending = _charge(db_session, user, account, external_id="p", pending=True)
    parent = _charge(db_session, user, account, external_id="par")
    child = _charge(db_session, user, account, external_id="kid", parent_id=parent.id)
    transfer = _charge(db_session, user, account, external_id="t", bucket=Bucket.transfer)
    inflow = _charge(db_session, user, account, "11.99", "in")

    ids = [pending.id, parent.id, child.id, transfer.id, inflow.id]
    assert subscription_matching.match_subscriptions(db_session, user.id, ids) == 0
    assert sub.next_due == DAY


def test_inactive_and_other_users_subscriptions_never_match(
    db_session: Session, user: User, other_user: User, account: Account
) -> None:
    _sub(db_session, user, active=False)
    _sub(db_session, other_user)
    txn = _charge(db_session, user, account)

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 0
    assert txn.subscription_id is None


# --- MV-1: the pending/posted overlap -----------------------------------------------------


def test_a_posted_row_listed_beside_its_pending_hold_is_absorbed_not_linked_early(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    sub = _sub(db_session, user, next_due=OLD)
    pending = _pending(
        db_session, user, account, OLD, amount=Decimal("-11.99"), description_raw="SPOTIFY USA"
    )
    pending_id = pending.id
    still = _txn(
        "pending-1",
        posted=0,
        amount="-11.99",
        description="SPOTIFY USA",
        transacted_at=_epoch(OLD),
    )
    posted = {**_posted("posted-9", OLD, "-11.99"), "description": "SPOTIFY USA"}

    _sync(db_session, user, settings, [_account(transactions=[still, posted])])
    assert all(row.subscription_id is None for row in _rows(db_session, account))
    _sync(
        db_session, user, settings, [_account(transactions=[posted])], trigger=RunTrigger.scheduled
    )

    (row,) = _rows(db_session, account)
    assert row.id == pending_id
    assert row.pending is False
    assert row.subscription_id == sub.id


def test_a_linked_posted_row_is_still_merged_into_its_vanished_hold(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    sub = _sub(db_session, user, next_due=OLD)
    pending = _pending(
        db_session,
        user,
        account,
        OLD,
        amount=Decimal("-11.99"),
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    pending_id = pending.id
    _charge(
        db_session,
        user,
        account,
        external_id="posted-9",
        posted_date=OLD,
        subscription_id=sub.id,
    )

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[_posted("posted-9", OLD, "-11.99")])],
        trigger=RunTrigger.scheduled,
    )

    (row,) = _rows(db_session, account)
    assert row.id == pending_id
    assert row.subscription_id == sub.id


# --- MV-2: next_due is anchored to the charge ---------------------------------------------


def test_a_stale_next_due_catches_up_to_after_the_charge(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=date(2026, 8, 14))
    txn = _charge(db_session, user, account)

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1

    assert sub.next_due == date(2026, 11, 14)


def test_an_older_charge_links_without_moving_next_due_forward(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=date(2026, 11, 14))
    txn = _charge(db_session, user, account)

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1

    assert txn.subscription_id == sub.id
    assert sub.next_due == date(2026, 11, 14)


def test_a_yearly_backfilled_charge_does_not_push_next_due_a_year_off(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, cadence=Cadence.yearly, next_due=date(2027, 3, 1))
    txn = _charge(db_session, user, account, posted_date=date(2026, 3, 1))

    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    assert txn.subscription_id == sub.id
    assert sub.next_due == date(2027, 3, 1)


def test_an_early_charge_pays_the_coming_cycle(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=date(2026, 10, 20))
    txn = _charge(db_session, user, account)

    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    assert sub.next_due == date(2026, 11, 20)


# --- MV-3: word boundaries and the date gate ----------------------------------------------


@pytest.mark.parametrize(
    ("name", "description"),
    [("Apple", "PINEAPPLE EXPRESS CAFE"), ("Max", "MAXWELL HARDWARE")],
)
def test_a_name_inside_another_word_does_not_match(
    db_session: Session, user: User, account: Account, name: str, description: str
) -> None:
    _sub(db_session, user, name=name)
    txn = _charge(db_session, user, account, description_raw=description)

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 0
    assert txn.subscription_id is None


def test_a_name_on_word_boundaries_matches_the_merchant(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, name="Apple Music", amount=Decimal("-10.99"))
    txn = _charge(
        db_session, user, account, "-10.99", description_raw="APL*ITUNES", merchant="Apple Music."
    )

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1
    assert txn.subscription_id == sub.id


def test_a_charge_far_from_every_due_date_does_not_match(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, name="Amazon", next_due=date(2026, 11, 3))
    txn = _charge(db_session, user, account, description_raw="AMAZON MKTPL")

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 0
    assert txn.subscription_id is None


def test_a_charge_within_a_week_of_a_due_date_matches(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=date(2026, 10, 21))
    txn = _charge(db_session, user, account)

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1
    assert txn.subscription_id == sub.id


# --- MV-4: ambiguity per cycle ------------------------------------------------------------


def test_a_backfill_with_a_charge_per_cycle_links_every_one(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=None)
    charges = [
        _charge(db_session, user, account, external_id=f"s{m}", posted_date=date(2026, m, 5))
        for m in range(1, 11)
    ]

    linked = subscription_matching.match_subscriptions(
        db_session, user.id, [txn.id for txn in charges]
    )

    assert linked == 10
    assert all(txn.subscription_id == sub.id for txn in charges)
    assert sub.next_due == date(2026, 11, 5)


def test_two_charges_in_one_cycle_link_neither_but_a_later_cycle_still_links(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, next_due=None)
    one = _charge(db_session, user, account, external_id="a", posted_date=date(2026, 9, 10))
    two = _charge(db_session, user, account, external_id="b", posted_date=date(2026, 9, 13))
    later = _charge(db_session, user, account, external_id="c", posted_date=date(2026, 10, 10))
    messages, sink = _warnings()

    try:
        linked = subscription_matching.match_subscriptions(
            db_session, user.id, [one.id, two.id, later.id]
        )
    finally:
        logger.remove(sink)

    assert linked == 1
    assert one.subscription_id is None and two.subscription_id is None
    assert later.subscription_id == sub.id
    assert any("fits 2 charges" in message for message in messages)


# --- #139: match_text ---------------------------------------------------------------------


def test_match_text_links_a_charge_the_name_alone_would_not(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(
        db_session, user, name="iCloud", amount=Decimal("-9.99"), match_text="APPLE.COM/BILL"
    )
    txn = _charge(db_session, user, account, "-10.49", description_raw="APPLE.COM/BILL 866-712")

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1
    assert txn.subscription_id == sub.id


def test_the_name_alone_does_not_match_an_unrelated_descriptor(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, name="iCloud", amount=Decimal("-9.99"))
    txn = _charge(db_session, user, account, "-10.49", description_raw="APPLE.COM/BILL 866-712")

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 0


def test_match_text_replaces_the_name_when_set(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, name="Spotify", match_text="APPLE.COM/BILL")
    txn = _charge(db_session, user, account, description_raw="SPOTIFY USA 8776")

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 0


def test_match_text_keeps_the_word_boundary_rule(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, name="Anything", match_text="APPLE")
    txn = _charge(db_session, user, account, description_raw="PINEAPPLE EXPRESS CAFE")

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 0


# --- #129: business share -----------------------------------------------------------------


def _children(db_session: Session, parent: Transaction) -> list[Transaction]:
    return list(
        db_session.scalars(
            select(Transaction)
            .where(Transaction.parent_id == parent.id)
            .order_by(Transaction.amount)
        )
    )


def test_a_partial_business_pct_splits_the_charge_into_business_and_personal(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, amount=Decimal("-20.00"), name="Spotify", business_pct=40)
    txn = _charge(db_session, user, account, "-20.00")

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1

    assert txn.subscription_id == sub.id
    personal, business = _children(db_session, txn)
    assert (business.amount, business.bucket) == (Decimal("-8.00"), Bucket.business)
    assert (personal.amount, personal.bucket) == (Decimal("-12.00"), Bucket.personal)
    assert business.amount + personal.amount == txn.amount


def test_an_auto_split_of_an_unreviewed_charge_puts_the_children_in_the_inbox(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
) -> None:
    _sub(db_session, user, amount=Decimal("-20.00"), name="Spotify", business_pct=40)
    txn = _charge(db_session, user, account, "-20.00")

    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    assert txn.reviewed is True
    children = _children(db_session, txn)
    assert [c.reviewed for c in children] == [False, False]
    r = db_client.get("/api/inbox", headers=auth_headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert {i["id"] for i in items} == {str(c.id) for c in children}
    assert not any(i["needs_resplit"] for i in items)


def test_an_auto_split_of_a_rule_reviewed_charge_keeps_the_children_reviewed(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, amount=Decimal("-20.00"), name="Spotify", business_pct=40)
    db_session.add(make_rule(user, pattern="SPOTIFY", set_merchant="Spotify"))
    txn = _charge(db_session, user, account, "-20.00", reviewed=True, merchant="Spotify")

    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    assert [c.reviewed for c in _children(db_session, txn)] == [True, True]


@pytest.mark.parametrize(
    ("amount", "pct", "expected"),
    [
        ("-20.00", 40, ("-8.00", "-12.00")),
        ("-9.99", 33, ("-3.30", "-6.69")),
        ("-0.03", 50, ("-0.02", "-0.01")),
        ("-0.05", 50, ("-0.02", "-0.03")),
        ("-0.01", 50, None),
        ("-0.04", 10, None),
        ("-0.03", 99, None),
        ("-10.00", 99, ("-9.90", "-0.10")),
        ("-10.00", 1, ("-0.10", "-9.90")),
    ],
)
def test_split_amounts_rounds_half_even_and_rejects_a_zero_part(
    amount: str, pct: int, expected: tuple[str, str] | None
) -> None:
    result = subscription_matching._split_amounts(Decimal(amount), pct)
    assert result == (None if expected is None else (Decimal(expected[0]), Decimal(expected[1])))


def test_split_children_always_sum_exactly_to_the_parent(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, amount=Decimal("-9.99"), name="Spotify", business_pct=33)
    txn = _charge(db_session, user, account, "-9.99")

    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    children = _children(db_session, txn)
    assert sum((c.amount for c in children), Decimal(0)) == Decimal("-9.99")
    assert {c.amount for c in children} == {Decimal("-3.30"), Decimal("-6.69")}


def test_the_business_share_shows_in_the_tax_summary(
    db_client: TestClient,
    db_session: Session,
    user: User,
    account: Account,
    auth_headers: dict[str, str],
) -> None:
    _sub(db_session, user, amount=Decimal("-20.00"), name="Spotify", business_pct=40)
    txn = _charge(db_session, user, account, "-20.00")
    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    r = db_client.get("/api/tax/summary", params={"year": DAY.year}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()["uncategorized_business"] == {"spent": "8.00", "count": 1}


def test_a_full_business_pct_marks_the_charge_business_without_splitting(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, name="Spotify", business_pct=100)
    txn = _charge(db_session, user, account)

    subscription_matching.match_subscriptions(db_session, user.id, [txn.id])

    assert txn.bucket == Bucket.business
    assert txn.reviewed is False
    assert _children(db_session, txn) == []


@pytest.mark.parametrize("pct", [None, 0])
def test_a_zero_or_missing_business_pct_leaves_the_charge_alone(
    db_session: Session, user: User, account: Account, pct: int | None
) -> None:
    _sub(db_session, user, name="Spotify", business_pct=pct)
    txn = _charge(db_session, user, account)

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1

    assert txn.bucket == Bucket.personal
    assert _children(db_session, txn) == []


def test_a_charge_classified_by_hand_is_linked_but_not_split(
    db_session: Session, user: User, account: Account
) -> None:
    sub = _sub(db_session, user, name="Spotify", business_pct=40)
    txn = _charge(db_session, user, account, bucket=Bucket.reimbursable)

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1

    assert txn.subscription_id == sub.id
    assert txn.bucket == Bucket.reimbursable
    assert _children(db_session, txn) == []


def test_a_charge_in_a_filed_report_is_linked_but_left_unsplit_and_logged(
    db_session: Session, user: User, account: Account
) -> None:
    report = make_reimbursement(user, status=ReimbursementStatus.submitted, submitted_date=DAY)
    db_session.add(report)
    db_session.flush()
    sub = _sub(db_session, user, name="Spotify", business_pct=40)
    txn = _charge(db_session, user, account, reimbursement_id=report.id)
    messages, handler = _warnings()

    try:
        assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1
    finally:
        logger.remove(handler)

    assert txn.subscription_id == sub.id
    assert _children(db_session, txn) == []
    assert any("left it unsplit" in m for m in messages)


def test_a_zero_part_share_is_linked_but_not_split(
    db_session: Session, user: User, account: Account
) -> None:
    _sub(db_session, user, amount=Decimal("-0.04"), name="Spotify", business_pct=10)
    txn = _charge(db_session, user, account, "-0.04")

    assert subscription_matching.match_subscriptions(db_session, user.id, [txn.id]) == 1

    assert _children(db_session, txn) == []
