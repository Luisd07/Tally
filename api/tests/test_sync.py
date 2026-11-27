"""`app.services.sync.run_sync`/`notify` and the `/api/sync/simplefin/run`,
`/api/sync/runs` endpoints (#48). Mocked at the HTTP boundary with
`httpx.MockTransport`, using recorded-shape SimpleFIN `/accounts` responses per
https://www.simplefin.org/protocol.html ("4. Get Data", "Account Set", "Account",
"Transaction", "Data: Error"). Never calls the real SimpleFIN or APNs.
"""

import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy.orm import Session

from app.auth.ratelimit import RateLimited
from app.config import Settings
from app.deps import get_simplefin_transport
from app.models import (
    Account,
    BalanceSnapshot,
    SimplefinConnection,
    SyncRun,
    Transaction,
    User,
)
from app.models.enums import AccountKind, Bucket, RunStatus, RunTrigger, Source
from app.schemas.transactions import TransactionSplit, TransactionSplitPart
from app.services import simplefin
from app.services import sync as sync_service
from app.services.transactions import split_transaction
from tests.factories import (
    make_account,
    make_device,
    make_rule,
    make_simplefin_connection,
    make_sync_run,
    make_transaction,
)
from tests.locks import wait_until_blocked_on_lock

NOW = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)
ACCESS_URL = "https://demo-user:demo-pass@bridge.simplefin.org/simplefin"
RUN = "/api/sync/simplefin/run"
RUNS = "/api/sync/runs"


# --- SimpleFIN response builders, per the protocol's Account Set/Account/Transaction shapes ---


def _txn(
    txn_id: str = "txn-1",
    posted: int = int(NOW.timestamp()),
    amount: str = "-12.34",
    description: str = "HERTZ RENTAL CAR",
    **extra: Any,
) -> dict[str, Any]:
    payload = {"id": txn_id, "posted": posted, "amount": amount, "description": description}
    payload.update(extra)
    return payload


def _account(
    account_id: str = "act-checking",
    name: str = "Everyday Checking",
    balance: str = "100.23",
    balance_date: int = int(NOW.timestamp()),
    transactions: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "id": account_id,
        "name": name,
        "balance": balance,
        "balance-date": balance_date,
        "transactions": transactions or [],
    }
    payload.update(extra)
    return payload


def _account_set(
    accounts: list[dict[str, Any]],
    *,
    errlist: list[Any] | None = None,
    connections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {"errlist": errlist or [], "connections": connections or [], "accounts": accounts}


def _transport(payload: Any) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


def _connection(
    user: User, settings: Settings, access_url: str = ACCESS_URL
) -> SimplefinConnection:
    return make_simplefin_connection(
        user, access_url_encrypted=simplefin.encrypt_access_url(settings, access_url)
    )


def _make_split_parent_with_children(
    db_session: Session, user: User, settings: Settings
) -> tuple[Account, Transaction, Transaction, Transaction]:
    account = make_account(user, simplefin_account_id="act-split")
    db_session.add(account)
    db_session.flush()
    parent = make_transaction(
        user,
        account,
        amount=Decimal("-100.00"),
        description_raw="COSTCO WHOLESALE",
        source=Source.simplefin,
        external_id="txn-split-parent",
        reviewed=True,
    )
    db_session.add(parent)
    db_session.flush()
    child_1 = make_transaction(
        user,
        account,
        amount=Decimal("-60.00"),
        description_raw="COSTCO (groceries)",
        parent_id=parent.id,
    )
    child_2 = make_transaction(
        user,
        account,
        amount=Decimal("-40.00"),
        description_raw="COSTCO (travel)",
        parent_id=parent.id,
    )
    db_session.add_all([child_1, child_2])
    db_session.add(_connection(user, settings))
    db_session.flush()
    return account, parent, child_1, child_2


@pytest.fixture
def push_settings(settings: Settings, tmp_path: Path) -> Settings:
    """`settings` plus a real P-256 key, so `apns.send_alert` can mint a provider
    JWT instead of failing before it ever reaches the (mocked) APNs transport.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "AuthKey_TEST48.p8"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return settings.model_copy(
        update={
            "apns_key_id": "TESTKEY48",
            "apns_team_id": "TESTTEAM48",
            "apns_bundle_id": "com.example.tally",
            "apns_private_key_path": str(key_path),
        }
    )


# --- run_sync: first sync, idempotent re-sync -----------------------------------------


def test_first_sync_inserts_accounts_snapshots_and_transactions(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    payload = _account_set([_account(transactions=[_txn()])])

    run, push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.status == RunStatus.ok
    assert run.new_count == 1
    assert run.updated_count == 0
    assert push is not None

    account = db_session.scalars(
        select(Account).where(
            Account.user_id == user.id, Account.simplefin_account_id == "act-checking"
        )
    ).one()
    assert account.name == "Everyday Checking"
    assert account.kind.value == "checking"

    snapshot = db_session.scalars(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id)
    ).one()
    assert snapshot.balance == Decimal("100.23")

    txn = db_session.scalars(
        select(Transaction).where(
            Transaction.account_id == account.id, Transaction.external_id == "txn-1"
        )
    ).one()
    assert txn.amount == Decimal("-12.34")
    assert txn.source == Source.simplefin
    assert txn.bucket.value == "personal"
    assert txn.reviewed is False
    assert txn.fingerprint is not None


def test_second_identical_sync_has_new_count_zero_and_no_duplicates(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    payload = _account_set([_account(transactions=[_txn()])])

    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )
    second, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW + timedelta(hours=1),
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert second.new_count == 0
    assert second.updated_count == 1

    accounts = db_session.scalars(select(Account).where(Account.user_id == user.id)).all()
    assert len(accounts) == 1
    txns = db_session.scalars(select(Transaction).where(Transaction.user_id == user.id)).all()
    assert len(txns) == 1


def test_run_sync_accepts_a_custom_window(
    db_session: Session, user: User, settings: Settings
) -> None:
    """UB-1: the window is a caller input (default: the last 7 days), so a future
    backfill (#49) can loop `fetch`/`_ingest` over its own 45-day windows without
    touching `run_sync`'s signature again.
    """
    db_session.add(_connection(user, settings))
    db_session.flush()
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        return httpx.Response(200, json=_account_set([]))

    custom_window = (1700000000, 1700086400)
    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        window=custom_window,
        sf_transport=httpx.MockTransport(handler),
    )

    assert seen[0]["start-date"] == str(custom_window[0])
    assert seen[0]["end-date"] == str(custom_window[1])


def test_user_edits_survive_a_resync_that_changes_the_amount(
    db_session: Session, user: User, settings: Settings
) -> None:
    """Category, notes and reviewed are the user's; a re-sync only ever touches the
    bank-owned fields, even when the bank's own amount for the row changes.
    """
    from app.models import Category
    from app.models.enums import CategoryKind

    account = make_account(user, simplefin_account_id="act-checking")
    category_id = uuid.uuid4()
    db_session.add(account)
    db_session.flush()

    category = Category(id=category_id, user_id=user.id, name="Travel", kind=CategoryKind.expense)
    db_session.add(category)
    txn = make_transaction(
        user,
        account,
        amount=Decimal("-12.34"),
        description_raw="HERTZ RENTAL CAR",
        source=Source.simplefin,
        external_id="txn-1",
        category_id=category_id,
        notes="Daytona trip",
        reviewed=True,
    )
    db_session.add(txn)
    db_session.add(_connection(user, settings))
    db_session.flush()

    payload = _account_set([_account(transactions=[_txn(amount="-20.00")])])
    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    updated = db_session.get(Transaction, txn.id)
    assert updated is not None
    assert updated.amount == Decimal("-20.00")  # the bank-owned field did change
    assert updated.category_id == category_id
    assert updated.notes == "Daytona trip"
    assert updated.reviewed is True


def test_pending_transaction_uses_transacted_at_when_posted_is_zero(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    transacted_epoch = int(NOW.timestamp()) - 1800
    payload = _account_set(
        [_account(transactions=[_txn(posted=0, pending=True, transacted_at=transacted_epoch)])]
    )

    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    txn = db_session.scalars(select(Transaction).where(Transaction.external_id == "txn-1")).one()
    assert txn.pending is True
    assert txn.posted_date == datetime.fromtimestamp(transacted_epoch, tz=UTC).date()
    assert txn.transacted_at == datetime.fromtimestamp(transacted_epoch, tz=UTC)


def test_posted_zero_with_no_pending_key_is_treated_as_pending(
    db_session: Session, user: User, settings: Settings
) -> None:
    """MV-2: the protocol lets `posted` be 0 *instead of* setting `pending`, so a
    row with `posted=0` and no `pending` key at all must still be stored pending.
    """
    db_session.add(_connection(user, settings))
    db_session.flush()
    transacted_epoch = int(NOW.timestamp()) - 1800
    payload = _account_set(
        [_account(transactions=[_txn(posted=0, transacted_at=transacted_epoch)])]
    )

    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    txn = db_session.scalars(select(Transaction).where(Transaction.external_id == "txn-1")).one()
    assert txn.pending is True


# --- errors, status --------------------------------------------------------------------


def test_errlist_is_saved_and_status_is_partial(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    payload = _account_set(
        [_account(transactions=[_txn()])],
        errlist=[{"code": "con.auth", "msg": "My Bank login needs renewing.", "conn_id": "CON-1"}],
    )

    run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.status == RunStatus.partial
    assert run.new_count == 1  # data was still saved
    assert run.errors == [
        {"code": "con.auth", "msg": "My Bank login needs renewing.", "conn_id": "CON-1"}
    ]


def test_fetch_failure_gives_status_failed_with_a_sanitized_error(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    run, push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=httpx.MockTransport(handler),
    )

    assert run.status == RunStatus.failed
    assert push is None
    assert len(run.errors) == 1
    message = run.errors[0]["msg"]
    assert "demo-user" not in message
    assert "demo-pass" not in message
    assert ACCESS_URL not in message
    assert "https://" not in message


def test_wrong_encryption_key_gives_status_failed_with_a_sanitized_error(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    wrong_settings = settings.model_copy(
        update={"simplefin_encryption_key": SecretStr(Fernet.generate_key().decode())}
    )

    run, _push = sync_service.run_sync(
        db_session, user.id, NOW, wrong_settings, trigger=RunTrigger.manual
    )

    assert run.status == RunStatus.failed
    message = run.errors[0]["msg"]
    assert "demo-user" not in message
    assert "demo-pass" not in message
    assert ACCESS_URL not in message


def test_no_connection_gives_a_failed_run(
    db_session: Session, user: User, settings: Settings
) -> None:
    run, push = sync_service.run_sync(db_session, user.id, NOW, settings, trigger=RunTrigger.manual)

    assert run.status == RunStatus.failed
    assert run.new_count == 0
    assert len(run.errors) == 1
    assert push is None


def test_non_dict_payload_body_gives_a_failed_run(
    db_session: Session, user: User, settings: Settings
) -> None:
    """UB-4/MV-1: a server returning a JSON array (or any non-object body) must
    not crash the run -- it fails cleanly, and the row is still persisted.
    """
    db_session.add(_connection(user, settings))
    db_session.flush()

    run, push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport([]),
    )

    assert run.status == RunStatus.failed
    assert push is None
    assert len(run.errors) == 1


def test_malformed_account_missing_balance_is_skipped_and_recorded(
    db_session: Session, user: User, settings: Settings
) -> None:
    """UB-4/MV-1: one account missing a required field (`balance`) doesn't lose
    the rest of the pull -- it's skipped and recorded, while a good account in
    the same response still lands.
    """
    db_session.add(_connection(user, settings))
    db_session.flush()
    good = _account(account_id="act-good", transactions=[_txn(txn_id="txn-good")])
    bad = {"id": "act-bad", "name": "Bad Account", "transactions": []}  # no "balance"
    payload = _account_set([good, bad])

    run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.status == RunStatus.partial
    assert run.new_count == 1  # the good account still landed
    assert len(run.errors) == 1
    assert run.errors[0]["account_id"] == "act-bad"
    assert "KeyError" in run.errors[0]["msg"]
    good_account = db_session.scalars(
        select(Account).where(Account.simplefin_account_id == "act-good")
    ).one_or_none()
    assert good_account is not None
    bad_account = db_session.scalars(
        select(Account).where(Account.simplefin_account_id == "act-bad")
    ).one_or_none()
    assert bad_account is None  # rolled back to the savepoint


def test_transaction_with_non_numeric_amount_skips_that_account_only(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    good = _account(account_id="act-good", transactions=[_txn(txn_id="txn-good")])
    bad = _account(
        account_id="act-bad", transactions=[_txn(txn_id="txn-bad", amount="not-a-number")]
    )
    payload = _account_set([good, bad])

    run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.status == RunStatus.partial
    assert run.new_count == 1
    assert len(run.errors) == 1
    assert run.errors[0]["account_id"] == "act-bad"
    assert "not-a-number" not in str(run.errors)


def test_errlist_entry_that_is_not_an_object_is_dropped(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    payload = _account_set(
        [_account(transactions=[_txn()])],
        errlist=[{"code": "con.auth", "msg": "ok"}, "a string, not an object"],
    )

    run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.errors == [{"code": "con.auth", "msg": "ok"}]


def test_post_process_failure_gives_partial_and_keeps_the_ingest(
    db_session: Session, user: User, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UB-4: a bug in a future hook (#51/#52/#56/#61/#50) must not cost the night's
    ingest -- it's recorded as one sanitized error and the run finishes `partial`.
    """
    db_session.add(_connection(user, settings))
    db_session.flush()
    payload = _account_set([_account(transactions=[_txn()])])

    def raising_post_process(
        db: Session, run: SyncRun, result: sync_service.IngestResult
    ) -> sync_service.SyncSummary:
        raise RuntimeError("boom")

    monkeypatch.setattr(sync_service, "_post_process", raising_post_process)

    run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.status == RunStatus.partial
    assert run.new_count == 1  # the ingest itself is kept
    assert any("RuntimeError" in e["msg"] for e in run.errors)
    assert "boom" not in str(run.errors)


# --- push --------------------------------------------------------------------------------


def test_push_sent_once_when_new_count_is_positive(
    db_session: Session, user: User, push_settings: Settings
) -> None:
    db_session.add(_connection(user, push_settings))
    db_session.add(make_device(user))
    db_session.flush()
    payload = _account_set([_account(transactions=[_txn()])])
    run, push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        push_settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )
    assert run.new_count == 1
    assert push is not None
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    sync_service.notify(
        db_session,
        user.id,
        NOW,
        push_settings,
        push,
        apns_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert len(requests) == 1
    assert b"new charge" in requests[0].content


def test_push_not_sent_when_new_count_is_zero(
    db_session: Session, user: User, push_settings: Settings
) -> None:
    db_session.add(_connection(user, push_settings))
    db_session.add(make_device(user))
    db_session.flush()
    payload = _account_set([_account(transactions=[])])  # nothing new

    run, push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        push_settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.new_count == 0
    assert push is None

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    sync_service.notify(
        db_session,
        user.id,
        NOW,
        push_settings,
        push,
        apns_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert len(requests) == 0


def test_push_body_uses_new_count_when_nothing_is_money_out(
    db_session: Session, user: User, push_settings: Settings
) -> None:
    """MV-3: a run whose only new row is a deposit must not push "0 new charges"."""
    db_session.add(_connection(user, push_settings))
    db_session.add(make_device(user))
    db_session.flush()
    payload = _account_set(
        [_account(transactions=[_txn(amount="1500.00", description="Payroll deposit")])]
    )

    run, push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        push_settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.new_count == 1
    assert push is not None
    assert "0 new charge" not in push.body
    assert push.body == "1 new transaction"


def test_notify_never_raises_when_the_push_fails(
    db_session: Session, user: User, push_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MV-5: a push failure must never cost the caller the sync it already committed."""
    db_session.add(make_device(user))
    db_session.flush()

    def raising_send_alert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("apns is down")

    monkeypatch.setattr(sync_service.apns, "send_alert", raising_send_alert)

    sync_service.notify(
        db_session, user.id, NOW, push_settings, sync_service.PushIntent("Tally", "1 new charge")
    )  # must not raise


# --- rules integration (#51): applied to new rows only, folded into the push ----------


def test_sync_applies_rules_and_reports_reimbursable_count_in_the_push(
    db_session: Session, user: User, push_settings: Settings
) -> None:
    db_session.add(_connection(user, push_settings))
    db_session.add(make_device(user))
    db_session.add(
        make_rule(user, pattern="HERTZ", set_bucket=Bucket.reimbursable, set_merchant=None)
    )
    db_session.flush()
    payload = _account_set([_account(transactions=[_txn()])])  # description defaults to HERTZ

    run, push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        push_settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.new_count == 1
    txn = db_session.scalars(select(Transaction).where(Transaction.external_id == "txn-1")).one()
    assert txn.bucket == Bucket.reimbursable
    assert txn.reviewed is True  # the spec's converse of "unmatched stays unreviewed" (MV-4)
    assert push is not None
    assert push.body == "1 new charge, 1 looks reimbursable"


def test_count_reimbursable_counts_only_the_given_transaction_ids(
    db_session: Session, user: User
) -> None:
    """#51 MV-3: the count is scoped to the ids passed in (a sync's own `new_ids`),
    not every reimbursable row the user has -- otherwise an older, unrelated
    reimbursable transaction would inflate a later sync's push.
    """
    account = make_account(user, simplefin_account_id="act-checking")
    db_session.add(account)
    db_session.flush()
    in_scope = make_transaction(user, account, bucket=Bucket.reimbursable, external_id="in-scope")
    out_of_scope = make_transaction(
        user, account, bucket=Bucket.reimbursable, external_id="out-of-scope"
    )
    db_session.add_all([in_scope, out_of_scope])
    db_session.flush()

    count = sync_service._count_reimbursable(db_session, [in_scope.id])

    assert count == 1


def test_rules_do_not_touch_an_existing_updated_row(
    db_session: Session, user: User, settings: Settings
) -> None:
    """Rules run on `IngestResult.new_ids` only (`_post_process`): a resync that just
    updates an existing, already-reviewed row's bank-owned fields must never
    retroactively apply a rule to it, even if a rule now matches its description.
    """
    account = make_account(user, simplefin_account_id="act-checking")
    db_session.add(account)
    db_session.flush()
    txn = make_transaction(
        user,
        account,
        amount=Decimal("-12.34"),
        description_raw="HERTZ RENTAL CAR",
        source=Source.simplefin,
        external_id="txn-1",
        reviewed=True,
    )
    db_session.add(txn)
    db_session.add(
        make_rule(user, pattern="HERTZ", set_bucket=Bucket.reimbursable, set_merchant=None)
    )
    db_session.add(_connection(user, settings))
    db_session.flush()
    payload = _account_set([_account(transactions=[_txn(amount="-20.00")])])

    run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.new_count == 0
    assert run.updated_count == 1
    updated = db_session.get(Transaction, txn.id)
    assert updated is not None
    assert updated.bucket == Bucket.personal
    assert updated.reviewed is True


# --- split-parent protection (#80): the bank's amount wins, flagged once --------------


def test_resync_updates_split_parents_amount_and_flags_once_for_review(
    db_session: Session, user: User, settings: Settings
) -> None:
    """MV-4: the bank is the source of truth for `amount`, even on a split parent
    (#80); a mismatch updates it and resets `reviewed` so the user knows to
    re-split, leaving the (now out-of-sum) children untouched.
    """
    _account_row, parent, child_1, child_2 = _make_split_parent_with_children(
        db_session, user, settings
    )
    payload = _account_set(
        [
            _account(
                account_id="act-split",
                transactions=[_txn(txn_id="txn-split-parent", amount="-120.00")],
            )
        ]
    )

    run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    assert run.new_count == 0
    assert run.updated_count == 1
    updated_parent = db_session.get(Transaction, parent.id)
    assert updated_parent is not None
    assert updated_parent.amount == Decimal("-120.00")  # the bank's new amount
    assert updated_parent.reviewed is False
    updated_child_1 = db_session.get(Transaction, child_1.id)
    updated_child_2 = db_session.get(Transaction, child_2.id)
    assert updated_child_1 is not None
    assert updated_child_2 is not None
    assert updated_child_1.amount == Decimal("-60.00")
    assert updated_child_2.amount == Decimal("-40.00")


def test_identical_resync_after_review_does_not_reflag_split_parent(
    db_session: Session, user: User, settings: Settings
) -> None:
    """MV-4: once the amount has already moved to the bank's value and the user
    re-reviews it, a later sync reporting that same amount must not re-flag it.
    """
    _account_row, parent, _child_1, _child_2 = _make_split_parent_with_children(
        db_session, user, settings
    )
    payload = _account_set(
        [
            _account(
                account_id="act-split",
                transactions=[_txn(txn_id="txn-split-parent", amount="-120.00")],
            )
        ]
    )

    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )
    reviewed_parent = db_session.get(Transaction, parent.id)
    assert reviewed_parent is not None
    reviewed_parent.reviewed = True  # the user reviews it
    db_session.flush()

    second_run, _push = sync_service.run_sync(
        db_session,
        user.id,
        NOW + timedelta(hours=1),
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),  # identical bank amount this time
    )

    assert second_run.updated_count == 1
    final_parent = db_session.get(Transaction, parent.id)
    assert final_parent is not None
    assert final_parent.reviewed is True  # not re-flagged


def test_resplit_against_the_new_amount_succeeds(
    db_session: Session, user: User, settings: Settings
) -> None:
    """MV-4: a re-split summing to the bank's new amount goes through, since
    `amount` (not the old split sum) is what a re-split validates against.
    """
    _account_row, parent, _child_1, _child_2 = _make_split_parent_with_children(
        db_session, user, settings
    )
    payload = _account_set(
        [
            _account(
                account_id="act-split",
                transactions=[_txn(txn_id="txn-split-parent", amount="-120.00")],
            )
        ]
    )
    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )

    row, children = split_transaction(
        db_session,
        user.id,
        parent.id,
        TransactionSplit(
            parts=[
                TransactionSplitPart(amount=Decimal("-70.00")),
                TransactionSplitPart(amount=Decimal("-50.00")),
            ]
        ),
    )

    assert row.amount == Decimal("-120.00")
    assert row.reviewed is True
    assert sorted(child.amount for child in children) == [Decimal("-70.00"), Decimal("-50.00")]


# --- ensure_cooldown_elapsed (UB-5/MV-6) ------------------------------------------------


def test_ensure_cooldown_elapsed_raises_within_the_window(db_session: Session, user: User) -> None:
    db_session.add(make_sync_run(user, started_at=NOW))
    db_session.flush()

    with pytest.raises(RateLimited):
        sync_service.ensure_cooldown_elapsed(
            db_session, user.id, NOW + timedelta(minutes=5), RunTrigger.manual
        )


def test_ensure_cooldown_elapsed_allows_after_the_window(db_session: Session, user: User) -> None:
    db_session.add(make_sync_run(user, started_at=NOW))
    db_session.flush()

    sync_service.ensure_cooldown_elapsed(
        db_session, user.id, NOW + timedelta(minutes=16), RunTrigger.manual
    )  # must not raise


def test_ensure_cooldown_elapsed_ignores_the_scheduled_trigger(
    db_session: Session, user: User
) -> None:
    db_session.add(make_sync_run(user, started_at=NOW))
    db_session.flush()

    sync_service.ensure_cooldown_elapsed(
        db_session, user.id, NOW + timedelta(minutes=1), RunTrigger.scheduled
    )  # must not raise


# --- UB-6: branches that previously had no dedicated test ------------------------------


def test_heuristic_kind_is_credit_card_for_a_negative_balance() -> None:
    assert (
        sync_service._heuristic_kind("Everyday Checking", Decimal("-5.00"))
        == AccountKind.credit_card
    )


def test_heuristic_kind_is_credit_card_for_a_card_name_hint() -> None:
    assert sync_service._heuristic_kind("Chase VISA", Decimal("100.00")) == AccountKind.credit_card


def test_institution_name_is_looked_up_via_conn_id() -> None:
    account_payload = {"id": "act-1", "conn_id": "CON-1"}
    connections = [{"conn_id": "CON-1", "name": "My Bank - Jeff"}]

    assert sync_service._institution_name(account_payload, connections) == "My Bank - Jeff"


def test_sanitize_errlist_drops_undeclared_fields() -> None:
    errlist = [{"code": "con.auth", "msg": "ok", "url": "https://leaked.example/token"}]

    assert sync_service._sanitize_errlist(errlist) == [{"code": "con.auth", "msg": "ok"}]


def test_fingerprint_is_stable_across_whitespace_and_case() -> None:
    account_id = uuid.uuid4()
    posted_date = date(2026, 10, 1)

    first = sync_service.fingerprint(
        account_id, posted_date, Decimal("-12.34"), "HERTZ   rental car"
    )
    second = sync_service.fingerprint(
        account_id, posted_date, Decimal("-12.34"), "hertz rental car"
    )

    assert first == second


# --- router: POST /api/sync/simplefin/run (cooldown) -----------------------------------


def test_run_now_returns_the_run_summary(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    settings: Settings,
) -> None:
    """Synchronous: a 7-day fetch is small, so the response carries the finished
    run itself rather than a 202 the app would have to poll.
    """
    db_session.add(_connection(user, settings))
    db_session.flush()
    payload = _account_set([_account(transactions=[_txn()])])
    app.dependency_overrides[get_simplefin_transport] = lambda: _transport(payload)

    r = db_client.post(RUN, headers=auth_headers)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["trigger"] == "manual"
    assert body["new_count"] == 1
    assert body["updated_count"] == 0


def test_cooldown_returns_429_with_retry_after(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    """The router computes its own `datetime.now(UTC)` (same as `claim_simplefin`),
    so the seeded run's `started_at` has to be real "now", not the fixed `NOW` the
    rest of this file uses for `run_sync`'s injected clock.
    """
    db_session.add(make_sync_run(user, started_at=datetime.now(UTC)))
    db_session.flush()

    r = db_client.get(RUNS, headers=auth_headers)
    assert r.status_code == 200  # sanity: the seeded run is visible before the cooldown check

    r = db_client.post(RUN, headers=auth_headers)

    assert r.status_code == 429
    retry_after = int(r.headers["Retry-After"])
    assert 0 < retry_after <= 15 * 60


# --- router: GET /api/sync/runs (paging, scoping) --------------------------------------


def test_runs_list_is_most_recent_first_and_pages(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    oldest = make_sync_run(user, started_at=NOW - timedelta(days=2))
    middle = make_sync_run(user, started_at=NOW - timedelta(days=1))
    newest = make_sync_run(user, started_at=NOW)
    db_session.add_all([oldest, middle, newest])
    db_session.flush()

    first_page = db_client.get(f"{RUNS}?limit=2", headers=auth_headers)
    assert first_page.status_code == 200
    body = first_page.json()
    assert [item["id"] for item in body["items"]] == [str(newest.id), str(middle.id)]
    assert body["next_cursor"] is not None

    second_page = db_client.get(
        f"{RUNS}?limit=2&cursor={body['next_cursor']}", headers=auth_headers
    )
    second_body = second_page.json()
    assert [item["id"] for item in second_body["items"]] == [str(oldest.id)]
    assert second_body["next_cursor"] is None


def test_runs_list_is_scoped_to_the_caller(
    db_client: TestClient,
    db_session: Session,
    user: User,
    other_user: User,
    auth_headers: dict[str, str],
) -> None:
    mine = make_sync_run(user)
    theirs = make_sync_run(other_user)
    db_session.add_all([mine, theirs])
    db_session.flush()

    r = db_client.get(RUNS, headers=auth_headers)

    assert r.status_code == 200
    ids = {item["id"] for item in r.json()["items"]}
    assert ids == {str(mine.id)}


# --- concurrency: two run_sync calls for the same user never double-insert -------------


def test_concurrent_run_sync_calls_do_not_double_insert(engine: Engine, settings: Settings) -> None:
    """Needs committed rows, so it runs outside the rolled-back test transaction,
    the same two-connection pattern as `test_refresh_rotation.py`'s advisory-lock
    tests. The first call holds the per-user lock for its whole upsert; the second
    blocks at the top of `run_sync` until the first commits, then lands on the
    first's already-committed transaction via the ordinary ON CONFLICT path.
    """
    user_id = uuid.uuid4()
    encrypted = simplefin.encrypt_access_url(settings, ACCESS_URL)
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Test User"))
        setup.flush()
        setup.add(
            SimplefinConnection(user_id=user_id, access_url_encrypted=encrypted, claimed_at=NOW)
        )
        setup.commit()

    payload = _account_set([_account(transactions=[_txn()])])
    transport = _transport(payload)

    try:
        with Session(engine) as first, Session(engine) as second:
            sync_service.run_sync(
                first, user_id, NOW, settings, trigger=RunTrigger.manual, sf_transport=transport
            )

            second_pid = second.scalar(text("SELECT pg_backend_pid()"))
            assert second_pid is not None
            results: list[SyncRun] = []

            def race() -> None:
                run, _push = sync_service.run_sync(
                    second,
                    user_id,
                    NOW,
                    settings,
                    trigger=RunTrigger.scheduled,
                    sf_transport=transport,
                )
                results.append(run)

            racer = threading.Thread(target=race)
            racer.start()
            wait_until_blocked_on_lock(engine, second_pid)
            first.commit()
            racer.join(timeout=10)
            second.commit()

            assert len(results) == 1
            assert results[0].new_count == 0
            assert results[0].updated_count == 1

        with Session(engine) as check:
            count = check.scalar(
                select(func.count()).select_from(Transaction).where(Transaction.user_id == user_id)
            )
            assert count == 1
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(Transaction).where(Transaction.user_id == user_id))
            cleanup.execute(delete(BalanceSnapshot).where(BalanceSnapshot.user_id == user_id))
            cleanup.execute(delete(Account).where(Account.user_id == user_id))
            cleanup.execute(delete(SyncRun).where(SyncRun.user_id == user_id))
            cleanup.execute(
                delete(SimplefinConnection).where(SimplefinConnection.user_id == user_id)
            )
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


# --- loans (#255) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "balance", "kind"),
    [
        ("ACME AUTO FINANCE", "-18000.00", AccountKind.loan),
        ("Chase Auto Loan", "-9000.00", AccountKind.loan),
        ("Chase Auto Loan", "9000.00", AccountKind.loan),
        ("Home MORTGAGE", "-250000.00", AccountKind.loan),
        ("Wells Fargo Autograph Card", "-200.00", AccountKind.credit_card),
        ("Autopay Checking", "150.00", AccountKind.checking),
        ("Automatic Savings", "50.00", AccountKind.checking),
        ("Toyota Financial Rewards Card", "-10.00", AccountKind.credit_card),
    ],
)
def test_heuristic_kind_matches_loan_words_whole(
    name: str, balance: str, kind: AccountKind
) -> None:
    assert sync_service._heuristic_kind(name, Decimal(balance)) == kind


def test_sync_classifies_a_new_loan_but_never_changes_an_existing_kind(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    payload = _account_set([_account("act-loan", "ACME AUTO FINANCE", "-18000.00")])

    sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )
    account = db_session.scalars(select(Account).where(Account.user_id == user.id)).one()
    assert account.kind == AccountKind.loan

    account.kind = AccountKind.credit_card
    db_session.flush()
    sync_service.run_sync(
        db_session,
        user.id,
        NOW + timedelta(days=1),
        settings,
        trigger=RunTrigger.manual,
        sf_transport=_transport(payload),
    )
    db_session.refresh(account)
    assert account.kind == AccountKind.credit_card
