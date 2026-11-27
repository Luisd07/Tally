"""#61: the transfer matcher pairs a card payment or balance transfer's two rows.

Acceptance scenarios drive `app.services.sync.run_sync` with hand-built SimpleFIN
payloads (`tests.test_sync` builders, `tests.test_reconcile._sync`); the eligibility
rules call `app.services.matching.match_transfers` on seeded rows. `NOW` is
2026-10-16, so the default sync window starts 2026-10-09.
"""

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, Transaction, User
from app.models.enums import AccountKind, Bucket, RunTrigger, Source
from app.services import matching, rules
from tests.factories import (
    make_account,
    make_category,
    make_receipt,
    make_rule,
    make_subscription,
    make_transaction,
)
from tests.test_reconcile import OLD, _epoch, _pending, _posted, _rows, _sync
from tests.test_sync import _account, _connection, _txn

DAY = date(2026, 10, 14)


def _accounts(
    db_session: Session, user: User, settings: Settings
) -> tuple[Account, Account, Account]:
    checking = make_account(user, simplefin_account_id="act-checking")
    card = make_account(
        user, name="Card", kind=AccountKind.credit_card, simplefin_account_id="act-card"
    )
    card2 = make_account(
        user, name="Card 2", kind=AccountKind.credit_card, simplefin_account_id="act-card2"
    )
    db_session.add_all([checking, card, card2, _connection(user, settings)])
    db_session.flush()
    return checking, card, card2


def _payload(
    checking: list[dict[str, Any]] | None = None,
    card: list[dict[str, Any]] | None = None,
    card2: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        _account("act-checking", transactions=checking),
        _account("act-card", name="Card", transactions=card),
        _account("act-card2", name="Card 2", transactions=card2),
    ]


def _bank(
    db_session: Session, user: User, account: Account, amount: str, **kwargs: object
) -> Transaction:
    defaults: dict[str, object] = {
        "source": Source.simplefin,
        "external_id": f"ext-{uuid.uuid4()}",
        "amount": Decimal(amount),
        "posted_date": DAY,
    }
    defaults.update(kwargs)
    txn = make_transaction(user, account, **defaults)
    db_session.add(txn)
    db_session.flush()
    return txn


def _match(db_session: Session, user: User, *rows: Transaction) -> int:
    ids = [row.id for row in rows]
    count = matching.match_transfers(
        db_session, user.id, ids, rules.load_rule_index(db_session, user.id)
    )
    db_session.expire_all()
    return count


def _is_paired(*rows: Transaction) -> bool:
    return all(row.bucket == Bucket.transfer and row.transfer_pair_id is not None for row in rows)


def _unpaired(*rows: Transaction) -> bool:
    return all(row.transfer_pair_id is None and row.bucket != Bucket.transfer for row in rows)


# --- AC-1 / AC-2: through run_sync --------------------------------------------------------


def test_a_card_payment_pairs_both_rows(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)

    _sync(
        db_session,
        user,
        settings,
        _payload(checking=[_posted("c-1", DAY, "-500.00")], card=[_posted("k-1", DAY, "500.00")]),
    )

    out = _rows(db_session, checking)[0]
    inn = _rows(db_session, card)[0]
    assert out.bucket == inn.bucket == Bucket.transfer
    assert out.reviewed is True and inn.reviewed is True
    assert out.transfer_pair_id == inn.id
    assert inn.transfer_pair_id == out.id


def test_a_counterpart_from_an_earlier_run_pairs_when_the_other_side_is_new(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    old = _bank(db_session, user, card, "500.00", posted_date=date(2026, 10, 11))

    _sync(db_session, user, settings, _payload(checking=[_posted("c-1", DAY, "-500.00")]))

    assert _is_paired(db_session.get(Transaction, old.id) or old, _rows(db_session, checking)[0])


def test_a_payment_six_days_apart_does_not_pair(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)

    _sync(
        db_session,
        user,
        settings,
        _payload(
            checking=[_posted("c-1", date(2026, 10, 10), "-500.00")],
            card=[_posted("k-1", date(2026, 10, 16), "500.00")],
        ),
    )

    assert _unpaired(*_rows(db_session, checking), *_rows(db_session, card))


def test_a_card_to_card_balance_transfer_pairs(
    db_session: Session, user: User, settings: Settings
) -> None:
    _, card, card2 = _accounts(db_session, user, settings)

    _sync(
        db_session,
        user,
        settings,
        _payload(card=[_posted("k-1", DAY, "300.00")], card2=[_posted("k-2", DAY, "-300.00")]),
    )

    first, second = _rows(db_session, card)[0], _rows(db_session, card2)[0]
    assert _is_paired(first, second)
    assert first.transfer_pair_id == second.id


def test_an_interest_row_with_no_counterpart_stays_an_expense(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)

    _sync(
        db_session,
        user,
        settings,
        _payload(
            checking=[_posted("c-1", DAY, "-500.00")],
            card=[_posted("k-1", DAY, "500.00"), _posted("k-2", DAY, "-35.00")],
        ),
    )

    fee = next(row for row in _rows(db_session, card) if row.external_id == "k-2")
    assert fee.bucket == Bucket.personal
    assert fee.transfer_pair_id is None
    assert fee.reviewed is False


def test_a_pending_row_is_not_paired(db_session: Session, user: User, settings: Settings) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    pending = _pending(db_session, user, checking, DAY, amount=Decimal("-500.00"))
    counterpart = _bank(db_session, user, card, "500.00")

    _match(db_session, user, pending, counterpart)

    assert _unpaired(pending, counterpart)


def test_split_rows_are_not_paired(db_session: Session, user: User, settings: Settings) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    parent = _bank(db_session, user, checking, "-500.00")
    child = _bank(db_session, user, checking, "-500.00", parent_id=parent.id)
    counterpart = _bank(db_session, user, card, "500.00")

    _match(db_session, user, parent, child, counterpart)

    assert _unpaired(parent, child, counterpart)


def test_a_manual_only_row_is_not_paired(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    manual = _bank(db_session, user, checking, "-500.00", source=Source.manual, external_id=None)
    counterpart = _bank(db_session, user, card, "500.00")

    _match(db_session, user, manual, counterpart)

    assert _unpaired(manual, counterpart)


def test_a_manual_row_is_not_paired_with_an_imported_csv_row(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    manual = _bank(db_session, user, checking, "-500.00", source=Source.manual, external_id=None)
    imported = _bank(db_session, user, card, "500.00", source=Source.csv, external_id=None)

    _match(db_session, user, manual, imported)

    assert _unpaired(manual, imported)


def test_rows_on_the_same_account_do_not_pair(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, _, _ = _accounts(db_session, user, settings)
    out = _bank(db_session, user, checking, "-500.00")
    inn = _bank(db_session, user, checking, "500.00")

    _match(db_session, user, out, inn)

    assert _unpaired(out, inn)


# --- AC-3: ambiguity, by-hand rows, rules, locks -------------------------------------------


@pytest.mark.parametrize("duplicate", ["outflow", "inflow"])
def test_two_candidates_pair_nothing_and_are_logged(
    db_session: Session, user: User, settings: Settings, duplicate: str
) -> None:
    checking, card, card2 = _accounts(db_session, user, settings)
    outflows = [_bank(db_session, user, checking, "-500.00")]
    inflows = [_bank(db_session, user, card, "500.00")]
    if duplicate == "outflow":
        outflows.append(_bank(db_session, user, card2, "-500.00"))
    else:
        inflows.append(_bank(db_session, user, card2, "500.00"))
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        _match(db_session, user, *outflows, *inflows)
    finally:
        logger.remove(sink)

    assert _unpaired(*outflows, *inflows)
    assert any("ambiguous" in message for message in messages)


def test_a_row_the_user_categorized_is_never_paired(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    mine = _bank(db_session, user, checking, "-500.00", category_id=category.id)
    counterpart = _bank(db_session, user, card, "500.00")

    _match(db_session, user, mine, counterpart)

    assert _unpaired(mine, counterpart)


def test_a_row_the_user_put_in_another_bucket_is_never_paired(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    mine = _bank(db_session, user, checking, "-500.00", bucket=Bucket.business)
    counterpart = _bank(db_session, user, card, "500.00")

    _match(db_session, user, mine, counterpart)

    assert _unpaired(mine, counterpart)


def test_a_row_the_user_already_marked_transfer_may_pair(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    mine = _bank(db_session, user, checking, "-500.00", bucket=Bucket.transfer, reviewed=True)
    counterpart = _bank(db_session, user, card, "500.00")

    assert _match(db_session, user, mine, counterpart) == 1

    assert _is_paired(mine, counterpart)
    assert mine.transfer_pair_id == counterpart.id


def test_a_row_a_rule_categorized_and_reviewed_is_paired(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    category = make_category(user, name="Payments")
    db_session.add(category)
    db_session.flush()
    db_session.add(
        make_rule(user, pattern="PAYMENT", set_merchant=None, set_category_id=category.id)
    )
    db_session.flush()

    _sync(
        db_session,
        user,
        settings,
        _payload(
            checking=[_posted("c-1", DAY, "-500.00") | {"description": "CARD PAYMENT"}],
            card=[_posted("k-1", DAY, "500.00")],
        ),
    )

    out = _rows(db_session, checking)[0]
    assert out.category_id == category.id
    assert out.bucket == Bucket.transfer
    assert out.transfer_pair_id == _rows(db_session, card)[0].id


@pytest.fixture
def committed_pair(engine: Engine) -> Iterator[tuple[uuid.UUID, list[uuid.UUID]]]:
    """A user with a checking and a card row, committed so a second session sees them."""
    user_id = uuid.uuid4()
    row_ids = [uuid.uuid4(), uuid.uuid4()]
    with Session(engine) as setup:
        owner = User(id=user_id, display_name="Race")
        setup.add(owner)
        setup.flush()
        checking, card = make_account(owner), make_account(owner, name="Card")
        setup.add_all([checking, card])
        setup.flush()
        for row_id, account, amount in zip(
            row_ids, (checking, card), ("-500.00", "500.00"), strict=True
        ):
            setup.add(
                make_transaction(
                    owner,
                    account,
                    id=row_id,
                    source=Source.simplefin,
                    external_id=str(row_id),
                    amount=Decimal(amount),
                    posted_date=DAY,
                )
            )
        setup.commit()
    try:
        yield user_id, row_ids
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_a_row_locked_by_another_writer_is_skipped_and_logged(
    engine: Engine, committed_pair: tuple[uuid.UUID, list[uuid.UUID]]
) -> None:
    user_id, row_ids = committed_pair
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        with Session(engine) as locker, Session(engine) as matcher:
            locker.scalars(
                select(Transaction).where(Transaction.id == row_ids[1]).with_for_update()
            ).one()
            count = matching.match_transfers(
                matcher, user_id, row_ids, rules.load_rule_index(matcher, user_id)
            )
            matcher.commit()
            locker.commit()
    finally:
        logger.remove(sink)

    assert count == 0
    with Session(engine) as verify:
        rows = verify.scalars(select(Transaction).where(Transaction.id.in_(row_ids))).all()
        assert all(row.transfer_pair_id is None for row in rows)
    assert any("locked by another writer" in message for message in messages)


# --- AC-4: a paired card payment is not spending -----------------------------------------


def test_a_paired_card_payment_is_not_spending_and_is_a_card_log_payment(
    db_client: TestClient,
    db_session: Session,
    user: User,
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)

    _sync(
        db_session,
        user,
        settings,
        _payload(
            checking=[_posted("c-1", DAY, "-500.00"), _posted("c-2", DAY, "-20.00")],
            card=[_posted("k-1", DAY, "500.00")],
        ),
    )

    dashboard = db_client.get("/api/dashboard", params={"month": "2026-10"}, headers=auth_headers)
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["total_spent"] == "20.00"
    home = db_client.get("/api/home", params={"month": "2026-10"}, headers=auth_headers)
    assert home.status_code == 200, home.text
    personal = next(
        row for row in home.json()["spending_by_bucket"]["buckets"] if row["bucket"] == "personal"
    )
    assert personal["spent"] == "20.00"
    card_log = db_client.get(
        f"/api/accounts/{card.id}/card-log", params={"month": "2026-10"}, headers=auth_headers
    )
    assert card_log.status_code == 200, card_log.text
    assert card_log.json()["payments"] == "500.00"


# --- AC-5: manual + pending + re-issued posted + counterpart -------------------------------


def test_manual_pending_posted_and_counterpart_end_as_one_paired_row(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    manual = make_transaction(
        user, checking, posted_date=DAY, amount=Decimal("-500.00"), source=Source.manual
    )
    db_session.add(manual)
    _pending(db_session, user, checking, DAY, amount=Decimal("-500.00"))
    manual_id = manual.id

    _sync(
        db_session,
        user,
        settings,
        _payload(
            checking=[_posted("posted-9", DAY, "-500.00")], card=[_posted("k-1", DAY, "500.00")]
        ),
    )

    rows = _rows(db_session, checking)
    assert [row.id for row in rows] == [manual_id]
    survivor, counterpart = rows[0], _rows(db_session, card)[0]
    assert survivor.external_id == "posted-9"
    assert _is_paired(survivor, counterpart)
    assert survivor.transfer_pair_id == counterpart.id
    assert counterpart.transfer_pair_id == manual_id
    paired = db_session.scalars(
        select(Transaction).where(Transaction.transfer_pair_id.is_not(None))
    ).all()
    assert len(paired) == 2


# --- MV-1: a row whose pending twin is still listed waits for the reconcile ----------------


def test_a_posted_reissue_waits_for_its_pending_twin_then_pairs_under_the_pending_id(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    pending = _pending(db_session, user, checking, OLD, amount=Decimal("-500.00"))
    pending_id = pending.id
    still_listed = _txn("pending-1", posted=0, amount="-500.00", transacted_at=_epoch(OLD))
    reissue = _posted("posted-9", OLD, "-500.00")
    payment = _posted("k-1", OLD, "500.00")

    _sync(
        db_session,
        user,
        settings,
        _payload(checking=[still_listed, reissue], card=[payment]),
        trigger=RunTrigger.scheduled,
    )

    assert _unpaired(*_rows(db_session, checking), *_rows(db_session, card))

    _sync(
        db_session,
        user,
        settings,
        _payload(checking=[reissue], card=[payment]),
        trigger=RunTrigger.scheduled,
    )

    rows = _rows(db_session, checking)
    assert [row.id for row in rows] == [pending_id]
    assert rows[0].external_id == "posted-9"
    assert rows[0].pending is False
    counterpart = _rows(db_session, card)[0]
    assert _is_paired(rows[0], counterpart)
    assert rows[0].transfer_pair_id == counterpart.id
    assert counterpart.transfer_pair_id == pending_id


def test_a_deferred_pending_twin_row_still_counts_toward_transfer_ambiguity(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, card2 = _accounts(db_session, user, settings)
    _pending(db_session, user, checking, OLD, amount=Decimal("-500.00"))
    still_listed = _txn("pending-1", posted=0, amount="-500.00", transacted_at=_epoch(OLD))

    _sync(
        db_session,
        user,
        settings,
        _payload(
            checking=[still_listed, _posted("posted-9", OLD, "-500.00")],
            card=[_posted("k-1", OLD, "500.00")],
            card2=[_posted("k2-1", OLD, "-500.00")],
        ),
        trigger=RunTrigger.scheduled,
    )

    assert _unpaired(*_rows(db_session, card), *_rows(db_session, card2))


# --- MV-3: anything the user attached blocks pairing, even when a rule set the values ------


def test_a_rule_set_reimbursable_charge_is_not_paired_with_a_matching_deposit(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    db_session.add(
        make_rule(user, pattern="CONFERENCE", set_merchant=None, set_bucket=Bucket.reimbursable)
    )
    db_session.flush()

    _sync(
        db_session,
        user,
        settings,
        _payload(
            checking=[_posted("c-1", DAY, "-500.00") | {"description": "CONFERENCE HOTEL"}],
            card=[_posted("k-1", DAY, "500.00")],
        ),
    )

    out = _rows(db_session, checking)[0]
    assert out.bucket == Bucket.reimbursable
    assert _unpaired(_rows(db_session, card)[0])
    assert out.transfer_pair_id is None


def test_a_row_with_a_receipt_is_not_paired(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    mine = _bank(db_session, user, checking, "-500.00")
    counterpart = _bank(db_session, user, card, "500.00")
    db_session.add(make_receipt(user, mine))
    db_session.flush()

    assert _match(db_session, user, mine, counterpart) == 0

    assert _unpaired(mine, counterpart)


def test_a_row_with_notes_is_not_paired(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    mine = _bank(db_session, user, checking, "-500.00", notes="card payoff, check with Sam")
    counterpart = _bank(db_session, user, card, "500.00")

    assert _match(db_session, user, mine, counterpart) == 0

    assert _unpaired(mine, counterpart)


def test_a_payment_the_subscription_matcher_linked_still_pairs_on_a_later_run(
    db_session: Session, user: User, settings: Settings
) -> None:
    checking, card, _ = _accounts(db_session, user, settings)
    subscription = make_subscription(user, name="Toyota", amount=Decimal("-400.00"))
    db_session.add(subscription)
    db_session.flush()
    linked = _bank(db_session, user, checking, "-400.00", subscription_id=subscription.id)

    _sync(db_session, user, settings, _payload(card=[_posted("k-1", DAY, "400.00")]))

    inn = _rows(db_session, card)[0]
    assert _is_paired(db_session.get(Transaction, linked.id) or linked, inn)
