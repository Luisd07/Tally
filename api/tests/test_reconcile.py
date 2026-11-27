"""#50: a pending bank row the bank re-issues under a new id once it posts.

Every scenario drives `app.services.sync.run_sync` with a hand-built, SimpleFIN-shaped
payload (`tests.test_sync` builders). The pending row is seeded directly, as an earlier
sync would have left it; `NOW` is 2026-10-16, so the default window starts 2026-10-09.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from loguru import logger
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, Event, Receipt, Transaction, User
from app.models.enums import Bucket, RunTrigger, Source
from app.services import reconcile
from app.services import sync as sync_service
from tests.factories import (
    make_account,
    make_category,
    make_receipt,
    make_reimbursement,
    make_rule,
    make_subscription,
    make_sync_run,
    make_transaction,
)
from tests.test_sync import NOW, _account, _account_set, _connection, _transport, _txn

OLD = date(2026, 10, 10)  # 6 days before NOW: inside the window, past the 5-day mark
YOUNG = date(2026, 10, 13)  # 3 days before NOW


def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp())


def _posted(txn_id: str, day: date, amount: str = "-12.34") -> dict[str, Any]:
    return _txn(txn_id, posted=_epoch(day), amount=amount)


def _setup(db_session: Session, user: User, settings: Settings) -> Account:
    account = make_account(user, simplefin_account_id="act-checking")
    # A prior ok run keeps this a normal sync; a first sync now backfills (#130).
    db_session.add_all([account, _connection(user, settings), make_sync_run(user)])
    db_session.flush()
    return account


def _pending(
    db_session: Session, user: User, account: Account, day: date, **kwargs: object
) -> Transaction:
    defaults: dict[str, object] = {
        "posted_date": day,
        "source": Source.simplefin,
        "external_id": "pending-1",
        "pending": True,
    }
    defaults.update(kwargs)
    txn = make_transaction(user, account, **defaults)
    db_session.add(txn)
    db_session.flush()
    return txn


def _sync(
    db_session: Session,
    user: User,
    settings: Settings,
    accounts: list[dict[str, Any]],
    *,
    trigger: RunTrigger = RunTrigger.manual,
    extra: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    """`extra` adds top-level keys to the response; `kwargs` go to `_account_set`."""
    payload = {**_account_set(accounts, **kwargs), **(extra or {})}
    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=trigger,
        sf_transport=_transport(payload),
    )
    db_session.expire_all()


def _rows(db_session: Session, account: Account) -> list[Transaction]:
    return list(
        db_session.scalars(
            select(Transaction)
            .where(Transaction.account_id == account.id)
            .order_by(Transaction.created_at)
        )
    )


def _exists(db_session: Session, id: uuid.UUID) -> bool:
    return db_session.get(Transaction, id) is not None


def _vanish(db_session: Session, user: User, settings: Settings) -> None:
    """A pull that no longer returns anything for the account."""
    _sync(db_session, user, settings, [_account(transactions=[])])


# --- AC-1 / AC-2: a re-issued posted row -------------------------------------------------


def test_edits_on_a_pending_row_survive_posting(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    pending = _pending(
        db_session,
        user,
        account,
        YOUNG,
        category_id=category.id,
        bucket=Bucket.reimbursable,
        notes="client lunch",
        reviewed=True,
    )
    receipt = make_receipt(user, pending)
    db_session.add(receipt)
    db_session.flush()
    pending_id, receipt_id, category_id = pending.id, receipt.id, category.id

    _sync(db_session, user, settings, [_account(transactions=[_posted("posted-9", YOUNG)])])

    rows = _rows(db_session, account)
    assert [row.id for row in rows] == [pending_id]
    survivor = rows[0]
    assert survivor.external_id == "posted-9"
    assert survivor.pending is False
    assert survivor.category_id == category_id
    assert survivor.bucket == Bucket.reimbursable
    assert survivor.notes == "client lunch"
    assert survivor.reviewed is True
    kept_receipt = db_session.get(Receipt, receipt_id)
    assert kept_receipt is not None
    assert kept_receipt.transaction_id == pending_id


@pytest.mark.parametrize("shift", [1, 2, 3, 4, 5])
def test_posted_date_shifted_up_to_five_days_still_matches(
    db_session: Session, user: User, settings: Settings, shift: int
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD, notes="keep me")
    pending_id = pending.id
    posted_day = date(2026, 10, 10 + shift)

    _sync(db_session, user, settings, [_account(transactions=[_posted("posted-9", posted_day)])])

    rows = _rows(db_session, account)
    assert [row.id for row in rows] == [pending_id]
    assert rows[0].posted_date == posted_day
    assert rows[0].notes == "keep me"


def test_posted_date_six_days_away_is_not_a_match(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD, notes="keep me")

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[_posted("posted-9", date(2026, 10, 16))])],
    )

    assert {row.external_id for row in _rows(db_session, account)} == {"pending-1", "posted-9"}
    assert _exists(db_session, pending.id)


def test_a_different_amount_is_not_a_match_and_is_logged(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, YOUNG)
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        _sync(
            db_session,
            user,
            settings,
            [_account(transactions=[_posted("posted-9", YOUNG, amount="-12.99")])],
        )
    finally:
        logger.remove(sink)

    assert {row.external_id for row in _rows(db_session, account)} == {"pending-1", "posted-9"}
    assert any(f"Keeping vanished pending {pending.id}" in message for message in messages)


def test_two_candidates_is_not_a_match_and_is_logged(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        _sync(
            db_session,
            user,
            settings,
            [
                _account(
                    transactions=[
                        _posted("posted-a", date(2026, 10, 11)),
                        _posted("posted-b", date(2026, 10, 12)),
                    ]
                )
            ],
        )
    finally:
        logger.remove(sink)

    assert {row.external_id for row in _rows(db_session, account)} == {
        "pending-1",
        "posted-a",
        "posted-b",
    }
    assert _exists(db_session, pending.id)  # skipped, so not deleted despite its age
    assert any(f"Skipping reconcile of pending {pending.id}" in message for message in messages)


def test_two_pending_rows_competing_for_one_posted_row_are_both_skipped(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    first = _pending(db_session, user, account, OLD, external_id="pending-1")
    second = _pending(db_session, user, account, OLD, external_id="pending-2")

    _sync(db_session, user, settings, [_account(transactions=[_posted("posted-9", OLD)])])

    assert _exists(db_session, first.id)
    assert _exists(db_session, second.id)
    assert len(_rows(db_session, account)) == 3


def test_an_account_that_failed_this_run_keeps_its_pending_rows(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)
    broken = _account(transactions=[_posted("posted-9", OLD), {"id": "no-amount", "posted": 1}])

    _sync(db_session, user, settings, [broken])

    survivor = db_session.get(Transaction, pending.id)
    assert survivor is not None
    assert survivor.pending is True


def test_an_account_named_in_the_errlist_keeps_its_pending_rows(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[])],
        errlist=[{"code": "act.auth", "msg": "re-auth", "account_id": "act-checking"}],
    )

    assert _exists(db_session, pending.id)


def test_an_account_payload_without_a_transactions_list_keeps_its_pending_rows(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)
    payload = _account()
    del payload["transactions"]

    _sync(db_session, user, settings, [payload])

    assert _exists(db_session, pending.id)


def test_an_errlist_entry_naming_no_account_keeps_every_pending_row(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[])],
        errlist=[{"code": "gen.auth", "msg": "something broke"}],
    )

    assert _exists(db_session, pending.id)


def test_a_legacy_errors_list_keeps_every_pending_row(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[])],
        extra={"errors": ["You must reauthenticate."]},
    )

    assert _exists(db_session, pending.id)


def test_a_pending_row_dated_before_the_window_is_left_alone(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, date(2026, 10, 1))

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending.id)


def test_a_pending_row_still_in_the_pull_is_left_alone(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)
    still_pending = _txn("pending-1", posted=0, amount="-12.34", transacted_at=_epoch(OLD))

    _sync(db_session, user, settings, [_account(transactions=[still_pending])])

    assert _exists(db_session, pending.id)


# --- AC-3: a vanished hold with no match ---------------------------------------------------


def test_an_untouched_cancelled_hold_is_deleted(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)
    pending_id = pending.id

    _vanish(db_session, user, settings)

    assert not _exists(db_session, pending_id)


def test_a_cancelled_hold_with_an_unreviewed_user_category_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    """PATCH does not set `reviewed`, so a category chosen alone is still the user's."""
    account = _setup(db_session, user, settings)
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    pending = _pending(db_session, user, account, OLD, category_id=category.id, reviewed=False)

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending.id)


def test_a_cancelled_hold_in_a_reimbursement_report_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    report = make_reimbursement(user)
    db_session.add(report)
    db_session.flush()
    pending = _pending(db_session, user, account, OLD, reimbursement_id=report.id)

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending.id)


def test_a_cancelled_hold_a_rule_reviewed_is_deleted(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    db_session.add(make_rule(user, pattern="HERTZ", set_merchant="Hertz"))
    pending = _pending(db_session, user, account, OLD, merchant="Hertz", reviewed=True)
    pending_id = pending.id

    _vanish(db_session, user, settings)

    assert not _exists(db_session, pending_id)


def test_a_cancelled_hold_with_a_user_bucket_a_category_only_rule_does_not_set_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    db_session.add(make_rule(user, pattern="HERTZ", set_category_id=category.id))
    pending = _pending(
        db_session,
        user,
        account,
        OLD,
        merchant="Hertz",
        category_id=category.id,
        bucket=Bucket.business,
        reviewed=True,
    )

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending.id)


def test_a_cancelled_hold_with_a_receipt_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)
    db_session.add(make_receipt(user, pending))
    db_session.flush()

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending.id)


def test_a_cancelled_hold_with_notes_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD, notes="ask Dana")

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending.id)


def test_a_cancelled_hold_the_user_recategorized_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    pending = _pending(db_session, user, account, OLD, category_id=category.id, reviewed=True)

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending.id)


def test_a_cancelled_hold_younger_than_five_days_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, YOUNG)

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending.id)


# --- a posted row listed alongside its pending row, then the pending row drops -------------


def test_a_posted_row_from_an_earlier_run_is_absorbed_when_the_pending_row_drops(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    pending = _pending(db_session, user, account, OLD)
    pending_id, category_id = pending.id, category.id
    still_pending = _txn("pending-1", posted=0, amount="-12.34", transacted_at=_epoch(OLD))

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[still_pending, _posted("posted-9", OLD)])],
    )
    assert len(_rows(db_session, account)) == 2
    edited = db_session.get(Transaction, pending_id)
    assert edited is not None
    edited.category_id = category_id
    edited.notes = "edited while both were listed"
    db_session.flush()

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[_posted("posted-9", OLD)])],
        trigger=RunTrigger.scheduled,
    )

    rows = _rows(db_session, account)
    assert [row.id for row in rows] == [pending_id]
    assert rows[0].external_id == "posted-9"
    assert rows[0].pending is False
    assert rows[0].category_id == category_id
    assert rows[0].notes == "edited while both were listed"


def test_a_row_posted_before_the_pending_row_appeared_is_not_a_candidate(
    db_session: Session, user: User, settings: Settings
) -> None:
    """A long-posted daily coffee is not the re-issue of today's hold."""
    account = _setup(db_session, user, settings)
    old_posted = make_transaction(
        user, account, posted_date=OLD, source=Source.simplefin, external_id="posted-old"
    )
    db_session.add(old_posted)
    db_session.flush()
    pending = _pending(db_session, user, account, OLD, notes="keep me")
    db_session.execute(
        update(Transaction)
        .where(Transaction.id == old_posted.id)
        .values(created_at=datetime(2026, 10, 1, tzinfo=UTC))
    )
    db_session.expire_all()

    _sync(db_session, user, settings, [_account(transactions=[_posted("posted-old", OLD)])])

    assert {row.external_id for row in _rows(db_session, account)} == {"pending-1", "posted-old"}
    assert _exists(db_session, pending.id)


# --- a row another writer holds -----------------------------------------------------------


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


@pytest.mark.parametrize("with_posted_row", [False, True], ids=["no-match", "match"])
def test_a_pending_row_locked_by_another_writer_is_skipped_and_logged(
    engine: Engine, committed_account: tuple[uuid.UUID, uuid.UUID], with_posted_row: bool
) -> None:
    user_id, account_id = committed_account
    pending_id = uuid.uuid4()
    rows = [
        Transaction(
            id=pending_id,
            user_id=user_id,
            account_id=account_id,
            posted_date=OLD,
            amount=Decimal("-12.34"),
            description_raw="HOLD",
            source=Source.simplefin,
            external_id="pending-1",
            pending=True,
        )
    ]
    if with_posted_row:
        rows.append(
            Transaction(
                user_id=user_id,
                account_id=account_id,
                posted_date=OLD,
                amount=Decimal("-12.34"),
                description_raw="HOLD",
                source=Source.simplefin,
                external_id="posted-9",
            )
        )
    with Session(engine) as setup:
        setup.add_all(rows)
        setup.commit()
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        with Session(engine) as locker, Session(engine) as reconciler:
            locker.scalars(
                select(Transaction).where(Transaction.id == pending_id).with_for_update()
            ).one()
            result = reconcile.reconcile_pending(
                reconciler,
                user_id,
                NOW,
                seen_external_ids={account_id: set()},
                window_start=date(2026, 10, 9),
            )
            reconciler.commit()
            locker.commit()
    finally:
        logger.remove(sink)

    assert result.deleted_count == 0
    assert result.absorbed_ids == []
    with Session(engine) as verify:
        remaining = verify.scalars(
            select(Transaction).where(Transaction.account_id == account_id)
        ).all()
        assert len(remaining) == len(rows)
    assert any(
        str(pending_id) in message and "locked by another writer" in message for message in messages
    )


# --- AC-5: manual row + pending row + re-issued posted row ----------------------------------


def test_manual_pending_and_posted_rows_end_as_one_row_with_the_manual_id(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    manual = make_transaction(
        user, account, posted_date=YOUNG, source=Source.manual, notes="phone note"
    )
    pending = _pending(db_session, user, account, YOUNG, category_id=category.id)
    db_session.add(manual)
    db_session.flush()
    manual_receipt = make_receipt(user, manual, sha256="1" * 64)
    pending_receipt = make_receipt(user, pending, sha256="2" * 64)
    db_session.add_all([manual_receipt, pending_receipt])
    db_session.flush()
    manual_id, category_id = manual.id, category.id
    receipt_ids = [manual_receipt.id, pending_receipt.id]

    _sync(db_session, user, settings, [_account(transactions=[_posted("posted-9", YOUNG)])])

    rows = _rows(db_session, account)
    assert [row.id for row in rows] == [manual_id]
    survivor = rows[0]
    assert survivor.source == Source.simplefin
    assert survivor.external_id == "posted-9"
    assert survivor.pending is False
    assert survivor.notes == "phone note"
    assert survivor.category_id == category_id
    receipts = db_session.scalars(select(Receipt).where(Receipt.id.in_(receipt_ids))).all()
    assert len(receipts) == 2
    assert {receipt.transaction_id for receipt in receipts} == {manual_id}


# --- round 2: a user-touched posted row is never the one absorbed (MV2-1) ----------------


def test_a_user_edited_posted_row_during_overlap_is_not_absorbed(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    rule_category = make_category(user, name="Rule")
    user_category = make_category(user, name="Mine")
    db_session.add_all([rule_category, user_category])
    db_session.flush()
    pending = _pending(db_session, user, account, YOUNG, category_id=rule_category.id)
    pending_id, user_category_id = pending.id, user_category.id
    still_pending = _txn("pending-1", posted=0, amount="-12.34", transacted_at=_epoch(YOUNG))
    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[still_pending, _posted("posted-9", YOUNG)])],
    )
    posted = next(row for row in _rows(db_session, account) if row.external_id == "posted-9")
    posted_id = posted.id
    posted.category_id = user_category_id
    posted.bucket = Bucket.reimbursable
    posted.reviewed = True
    db_session.flush()

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[_posted("posted-9", YOUNG)])],
        trigger=RunTrigger.scheduled,
    )

    assert _exists(db_session, pending_id)
    kept = db_session.get(Transaction, posted_id)
    assert kept is not None
    assert kept.category_id == user_category_id
    assert kept.bucket == Bucket.reimbursable


def test_a_late_manual_capture_keeps_its_id_when_the_pending_row_drops(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    rule_category = make_category(user, name="Rule")
    user_category = make_category(user, name="Mine")
    db_session.add_all([rule_category, user_category])
    db_session.flush()
    pending = _pending(db_session, user, account, YOUNG, category_id=rule_category.id)
    manual = make_transaction(
        user,
        account,
        posted_date=YOUNG,
        source=Source.manual,
        category_id=user_category.id,
        bucket=Bucket.reimbursable,
        notes="phone",
    )
    db_session.add(manual)
    db_session.flush()
    pending_id, manual_id = pending.id, manual.id
    still_pending = _txn("pending-1", posted=0, amount="-12.34", transacted_at=_epoch(YOUNG))
    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[still_pending, _posted("posted-9", YOUNG)])],
    )

    _sync(
        db_session,
        user,
        settings,
        [_account(transactions=[_posted("posted-9", YOUNG)])],
        trigger=RunTrigger.scheduled,
    )

    survivor = db_session.get(Transaction, manual_id)
    assert survivor is not None
    assert survivor.bucket == Bucket.reimbursable
    assert survivor.notes == "phone"
    assert _exists(db_session, pending_id)


def test_a_cancelled_hold_tagged_to_an_event_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    event = Event(
        user_id=user.id, name="Trip", start_date=date(2026, 10, 1), end_date=date(2026, 10, 20)
    )
    db_session.add(event)
    db_session.flush()
    pending = _pending(db_session, user, account, OLD, event_id=event.id)
    pending_id = pending.id

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending_id)


def test_a_reviewed_cancelled_hold_with_a_user_merchant_a_category_only_rule_does_not_set_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    category = make_category(user)
    db_session.add(category)
    db_session.flush()
    db_session.add(make_rule(user, pattern="HERTZ", set_merchant=None, set_category_id=category.id))
    db_session.flush()
    pending = _pending(
        db_session,
        user,
        account,
        OLD,
        category_id=category.id,
        merchant="My Renamed Merchant",
        reviewed=True,
    )
    pending_id = pending.id

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending_id)


def test_a_cancelled_hold_linked_to_a_subscription_is_kept(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    subscription = make_subscription(user)
    db_session.add(subscription)
    db_session.flush()
    pending = _pending(db_session, user, account, OLD, subscription_id=subscription.id)
    pending_id = pending.id

    _vanish(db_session, user, settings)

    assert _exists(db_session, pending_id)
