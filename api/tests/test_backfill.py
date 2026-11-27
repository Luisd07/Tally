"""The first SimpleFIN sync backfills the year in <=45-day chunks, and the manual run
endpoint takes an optional `since` (#130). SimpleFIN is mocked only at the HTTP
boundary (`httpx.MockTransport`).
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import APP_TIMEZONE, Settings
from app.deps import get_simplefin_transport
from app.models import Transaction, User
from app.models.enums import RunStatus, RunTrigger
from app.services import sync as sync_service
from tests.factories import make_sync_run
from tests.test_reconcile import OLD, _pending, _setup
from tests.test_sync import NOW, RUN, _account, _account_set, _connection, _txn

DAY = 86400


def _recording(
    respond: Callable[[int], Any],
) -> tuple[httpx.MockTransport, list[tuple[int, int]]]:
    """A transport that records each request's (start-date, end-date) and answers
    with `respond(request_index)`: a payload, or an `httpx.Response`.
    """
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        calls.append((int(params["start-date"]), int(params["end-date"])))
        answer = respond(len(calls) - 1)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    return httpx.MockTransport(handler), calls


def _jan_1_epoch() -> int:
    return int(datetime.combine(date(2026, 1, 1), time.min, tzinfo=APP_TIMEZONE).timestamp())


def _first_sync_chunks() -> int:
    """How many SimpleFIN requests the first sync's Jan 1 to `NOW` backfill makes."""
    return len(sync_service.chunk_window((_jan_1_epoch(), int(NOW.timestamp()))))


def _run(
    db_session: Session,
    user: User,
    settings: Settings,
    transport: httpx.MockTransport,
    **kwargs: Any,
):
    return sync_service.run_sync(
        db_session,
        user.id,
        NOW,
        settings,
        trigger=RunTrigger.manual,
        sf_transport=transport,
        **kwargs,
    )


def test_first_sync_backfills_from_jan_1_in_chunks_that_tile_to_now(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    chunk_days = sync_service.MAX_CHUNK.days
    overlap_days = sync_service.CHUNK_OVERLAP.days
    chunks = _first_sync_chunks()
    assert chunks >= 4
    # Two days before the first chunk ends: inside the overlap, so the second chunk sees it too.
    overlap = _txn("txn-overlap", posted=_jan_1_epoch() + (chunk_days - 2) * DAY)
    payloads = [_account_set([]) for _ in range(chunks)]
    payloads[0] = _account_set(
        [_account(transactions=[_txn("txn-a", posted=_jan_1_epoch() + DAY), overlap])]
    )
    payloads[1] = _account_set([_account(transactions=[overlap])])
    payloads[2] = _account_set(
        [_account(transactions=[_txn("txn-c", posted=_jan_1_epoch() + 2 * chunk_days * DAY)])]
    )
    payloads[-1] = _account_set(
        [_account(transactions=[_txn("txn-d", posted=int(NOW.timestamp()))])]
    )
    transport, calls = _recording(lambda i: payloads[i])

    run, _push = _run(db_session, user, settings, transport)

    assert run.status == RunStatus.ok
    assert len(calls) == chunks
    assert calls[0][0] == _jan_1_epoch()
    assert calls[-1][1] == int(NOW.timestamp())
    for start, end in calls:
        assert 0 < end - start <= chunk_days * DAY
    for (_, previous_end), (start, _) in zip(calls, calls[1:], strict=False):
        assert start == previous_end - overlap_days * DAY
    external_ids = db_session.scalars(
        select(Transaction.external_id).where(Transaction.user_id == user.id)
    ).all()
    assert len(external_ids) == 4
    assert set(external_ids) == {"txn-a", "txn-c", "txn-d", "txn-overlap"}
    assert run.new_count == 4
    assert run.updated_count == 0


def test_second_sync_after_an_ok_run_uses_the_seven_day_lookback(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    transport, calls = _recording(lambda i: _account_set([]))

    _run(db_session, user, settings, transport)
    assert len(calls) == _first_sync_chunks()
    calls.clear()
    sync_service.run_sync(
        db_session,
        user.id,
        NOW + timedelta(hours=1),
        settings,
        trigger=RunTrigger.manual,
        sf_transport=transport,
    )

    assert len(calls) == 1
    start, end = calls[0]
    assert end - start == 7 * DAY


def test_a_failed_first_run_does_not_count_as_a_prior_ok_run(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add_all([_connection(user, settings), make_sync_run(user, status=RunStatus.failed)])
    db_session.flush()
    transport, calls = _recording(lambda i: _account_set([]))

    _run(db_session, user, settings, transport)

    assert len(calls) == _first_sync_chunks()


def test_a_partial_first_run_counts_as_a_prior_sync(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    err = [{"code": "con.auth", "msg": "reauth", "conn_id": "con-x"}]
    transport, calls = _recording(lambda i: _account_set([], errlist=err))
    first, _ = _run(db_session, user, settings, transport)
    assert first.status == RunStatus.partial
    assert len(calls) == _first_sync_chunks()
    calls.clear()

    sync_service.run_sync(
        db_session,
        user.id,
        NOW + timedelta(days=1),
        settings,
        trigger=RunTrigger.scheduled,
        sf_transport=transport,
    )

    assert len(calls) == 1


def test_window_and_since_together_raise(
    db_session: Session, user: User, settings: Settings
) -> None:
    transport, calls = _recording(lambda i: _account_set([]))

    with pytest.raises(ValueError):
        _run(db_session, user, settings, transport, window=(0, 1), since=date(2026, 1, 1))

    assert calls == []


def test_max_backfill_fits_the_request_cap() -> None:
    end = int(NOW.timestamp())
    start = end - (sync_service.MAX_BACKFILL_DAYS + 1) * DAY

    assert len(sync_service.chunk_window((start, end))) <= sync_service.MAX_BACKFILL_REQUESTS


def test_account_absent_from_a_chunk_keeps_its_pending_hold(
    db_session: Session, user: User, settings: Settings
) -> None:
    account = _setup(db_session, user, settings)
    pending = _pending(db_session, user, account, OLD)
    pending_id = pending.id

    def respond(i: int) -> Any:
        accounts = [_account("act-other", transactions=[])]
        if i < _first_sync_chunks() - 1:  # act-checking is left out of the last chunk, no errlist
            accounts.append(_account("act-checking", transactions=[_txn(f"old-{i}")]))
        return _account_set(accounts)

    transport, calls = _recording(respond)

    run, _ = _run(db_session, user, settings, transport, since=date(2026, 1, 1))

    assert len(calls) == _first_sync_chunks()
    assert run.status == RunStatus.ok
    assert db_session.get(Transaction, pending_id) is not None


def test_a_failed_chunk_fails_the_run_and_ingests_nothing(
    db_session: Session, user: User, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    post_process_calls: list[int] = []
    monkeypatch.setattr(sync_service, "_post_process", lambda *args: post_process_calls.append(1))

    def respond(i: int) -> Any:
        if i == 2:
            return httpx.Response(500)
        return _account_set([_account(transactions=[_txn(f"txn-{i}")])])

    transport, calls = _recording(respond)

    run, push = _run(db_session, user, settings, transport)

    assert run.status == RunStatus.failed
    assert push is None
    assert len(calls) == 3
    assert post_process_calls == []
    assert db_session.scalar(select(func.count()).select_from(Transaction)) == 0


def test_post_process_runs_once_over_the_whole_backfill_window(
    db_session: Session, user: User, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    seen_results: list[sync_service.IngestResult] = []

    def spy(db: Session, run: Any, result: sync_service.IngestResult) -> sync_service.SyncSummary:
        seen_results.append(result)
        return sync_service.SyncSummary()

    monkeypatch.setattr(sync_service, "_post_process", spy)
    transport, _calls = _recording(
        lambda i: _account_set([_account(transactions=[_txn(f"txn-{i}")])])
    )

    _run(db_session, user, settings, transport)

    assert len(seen_results) == 1
    assert seen_results[0].window == (_jan_1_epoch(), int(NOW.timestamp()))
    assert len(seen_results[0].new_ids) == _first_sync_chunks()


def test_account_unreliable_in_any_chunk_has_no_seen_set_for_the_run(
    db_session: Session, user: User, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    seen_results: list[sync_service.IngestResult] = []

    def spy(db: Session, run: Any, result: sync_service.IngestResult) -> sync_service.SyncSummary:
        seen_results.append(result)
        return sync_service.SyncSummary()

    monkeypatch.setattr(sync_service, "_post_process", spy)

    def respond(i: int) -> Any:
        accounts = [
            _account("act-good", transactions=[_txn(f"good-{i}")]),
            _account("act-flaky", transactions=[_txn(f"flaky-{i}")]),
        ]
        errlist = [{"code": "act.auth", "msg": "x", "account_id": "act-flaky"}] if i == 1 else []
        return _account_set(accounts, errlist=errlist)

    transport, _calls = _recording(respond)

    _run(db_session, user, settings, transport)

    seen = seen_results[0].seen_external_ids
    assert len(seen) == 1
    assert next(iter(seen.values())) == {f"good-{i}" for i in range(_first_sync_chunks())}


def test_explicit_window_longer_than_45_days_is_chunked(
    db_session: Session, user: User, settings: Settings
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    transport, calls = _recording(lambda i: _account_set([]))

    _run(db_session, user, settings, transport, window=(0, 100 * DAY))

    assert calls == [(0, 45 * DAY), (40 * DAY, 85 * DAY), (80 * DAY, 100 * DAY)]


# --- router: POST /api/sync/simplefin/run?since= ---------------------------------------


def _today() -> date:
    return datetime.now(UTC).astimezone(APP_TIMEZONE).date()


def test_run_now_with_since_backfills_from_that_day_in_chunks(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    settings: Settings,
) -> None:
    db_session.add_all([_connection(user, settings), make_sync_run(user)])
    db_session.flush()
    transport, calls = _recording(lambda i: _account_set([]))
    app.dependency_overrides[get_simplefin_transport] = lambda: transport
    since = _today() - timedelta(days=200)

    r = db_client.post(f"{RUN}?since={since.isoformat()}", headers=auth_headers)

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert len(calls) == len(sync_service.chunk_window((calls[0][0], calls[-1][1])))
    assert len(calls) > 1
    assert calls[0][0] == int(datetime.combine(since, time.min, tzinfo=APP_TIMEZONE).timestamp())


def test_since_400_days_back_stays_within_the_request_cap(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    settings: Settings,
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    transport, calls = _recording(lambda i: _account_set([]))
    app.dependency_overrides[get_simplefin_transport] = lambda: transport
    since = _today() - timedelta(days=400)

    r = db_client.post(f"{RUN}?since={since.isoformat()}", headers=auth_headers)

    assert r.status_code == 200
    assert len(calls) <= sync_service.MAX_BACKFILL_REQUESTS


@pytest.mark.parametrize("offset_days", [-401, 1])
def test_since_out_of_range_is_422_and_fetches_nothing(
    app: FastAPI,
    db_client: TestClient,
    db_session: Session,
    user: User,
    auth_headers: dict[str, str],
    settings: Settings,
    offset_days: int,
) -> None:
    db_session.add(_connection(user, settings))
    db_session.flush()
    transport, calls = _recording(lambda i: _account_set([]))
    app.dependency_overrides[get_simplefin_transport] = lambda: transport
    since = _today() + timedelta(days=offset_days)

    r = db_client.post(f"{RUN}?since={since.isoformat()}", headers=auth_headers)

    assert r.status_code == 422
    assert calls == []
