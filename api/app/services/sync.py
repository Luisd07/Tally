"""The nightly (and manual "Sync now") SimpleFIN pull: fetch a window of days,
upsert accounts, balance snapshots and transactions, log the run, and work out
what to push once anything new landed. The push itself is sent by `notify`,
after the caller commits (see its docstring).

Shapes come from https://www.simplefin.org/protocol.html: an Account Set carries
`errlist` (structured errors, `gen.*`/`con.*`/`act.*` codes) and `accounts`, each
with `balance`/`balance-date` and a `transactions` list; each Transaction carries
`posted` (a Unix timestamp, `0` while the transaction is pending), the optional
`transacted_at`, and `amount` with SimpleFIN's sign (negative leaves the account).
The protocol defines no separate payee/memo field -- only `description` -- so
`description_raw` is the only field sync writes from it.

A sync upserts on `(account_id, external_id)` (spec), and a split's parent (#80)
keeps its own `external_id` while its children get a `NULL` one with `parent_id`
set -- so a re-sync can land on a split parent, never on its children.
`_bank_owned_update` checks `app.services.splits.has_split_children` for that;
see its docstring for the policy.

Every payload field this module reads from SimpleFIN is untrusted: a missing
key, a non-numeric amount or an unexpected shape anywhere in one account's data
is caught by `_ingest_account` and recorded as one sanitized error without
losing the rest of the pull or the `sync_runs` row itself (see its docstring).
"""

import hashlib
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from cryptography.fernet import InvalidToken
from loguru import logger
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth.ratelimit import RateLimited
from app.config import APP_TIMEZONE, Settings
from app.models import Account, BalanceSnapshot, SimplefinConnection, SyncRun, Transaction
from app.models.enums import AccountKind, Bucket, RunStatus, RunTrigger, Source
from app.schemas.sync import SyncRunListQuery, SyncRunOut, SyncRunPage, decode_cursor
from app.scoping import owned
from app.services import apns, matching, reconcile, rules, simplefin, subscription_matching
from app.services.normalize import normalize_text
from app.services.paging import paginate
from app.services.splits import has_split_children

# The Bridge refreshes each bank about once every 24 hours, so a longer lookback
# would just re-read data the (account_id, external_id) unique key already absorbs.
LOOKBACK = timedelta(days=7)

# SimpleFIN recommends at most 45 days per /accounts request (longer ones come back
# with a `gen.api` warning, "may be capped" later), so a longer window (the first
# sync's backfill from Jan 1, #130) is split into chunks that overlap by a few
# days: the overlap absorbs rows whose date lands on a chunk edge, and the
# (account_id, external_id) unique key absorbs the repeat.
MAX_CHUNK = timedelta(days=45)
CHUNK_OVERLAP = timedelta(days=5)

# The furthest back a backfill may reach (`since` on a manual sync), and the most
# SimpleFIN requests one backfill may cost. SimpleFIN disables access tokens that
# exceed ~24 requests a day, so the two must agree with the chunking above: the
# assert after `chunk_window` fails at import if a constant change breaks that.
MAX_BACKFILL_DAYS = 400
MAX_BACKFILL_REQUESTS = 10

# A manual "Sync now" refuses a repeat within this long of the last run's start.
# SimpleFIN allows up to 24 requests/day (one every hour on average); the
# scheduled job makes one pull itself, so 15 minutes is a tap-debounce against
# accidental double-taps rather than the sole guard against that budget -- normal
# "Sync now" use never gets close to it regardless.
RUN_COOLDOWN = timedelta(minutes=15)

# There's no `kind` in the SimpleFIN protocol. Heuristic for a brand-new account
# (#48, #255): a name with the whole word loan, mortgage or finance is a loan, even at
# a negative balance; otherwise a negative balance reads like money owed, same as a
# card statement; failing that, a name that mentions a card issuer. Everything else
# defaults to checking. Never re-evaluated after the first sync, so a user's own edit
# always sticks.
_LOAN_NAME = re.compile(r"\b(loan|mortgage|finance)\b", re.IGNORECASE)
_CREDIT_CARD_HINTS = ("credit", "card", "visa", "mastercard", "amex", "discover")

# The Error object's defined fields (protocol "Data: Error"); anything else a
# server includes is dropped before the errlist is stored or shown, so a field we
# don't expect can't smuggle a URL or credentials into `sync_runs.errors`.
_ERRLIST_FIELDS = ("code", "msg", "conn_id", "account_id")

# Raised while parsing one account's payload; caught by `_ingest_account` so one
# bad account doesn't lose the rest of the pull.
_ACCOUNT_PARSE_ERRORS = (KeyError, TypeError, ValueError, InvalidOperation, AttributeError)


@dataclass
class IngestResult:
    """What one `_ingest` call produced.

    `seen_external_ids` is keyed by `Account.id`, so the pending reconcile (#50)
    can tell which of an account's rows were actually present in this pull's
    `window`. An account only gets a key once it has ingested completely with a
    `transactions` list, and never when the errlist names it or its connection
    (`unreliable_ids`): a failed or unreliable account must not look like one
    whose pending rows vanished. When the response as a whole is degraded (see
    `_response_is_degraded`) it is empty for every account. `errors` holds one
    sanitized entry per account `_ingest_account` had to skip.

    `newly_posted_ids` is the subset of `new_ids`/`updated_ids` that became
    matchable against a manual capture (#52) this run: a brand-new row that
    landed already posted, or an existing row that flipped from pending to
    posted. Deliberately not all of `updated_ids` -- most of it is just the
    `LOOKBACK` window re-reading rows this sync already matched or never will,
    and handing those to the matcher would let an old bank row match a brand
    new manual capture it has nothing to do with.

    A chunked pull (`chunk_window`) ingests every chunk into the one result:
    `seen_external_ids` is the union over chunks, and `unreliable_ids` and
    `degraded` are sticky, so an account that failed or was named in the errlist
    of any chunk is unreliable for the whole run (`_finish_ingest`). An account
    also keeps its seen set only if it was cleanly ingested in every chunk
    (`chunks` vs `clean_chunks`): one absent from a chunk says nothing about
    that chunk's dates, so it looks the same as one absent from a single fetch.
    """

    new_ids: list[uuid.UUID] = field(default_factory=list)
    updated_ids: list[uuid.UUID] = field(default_factory=list)
    newly_posted_ids: list[uuid.UUID] = field(default_factory=list)
    money_out_new: int = 0
    seen_external_ids: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    unreliable_ids: set[str] = field(default_factory=set)
    account_keys: dict[uuid.UUID, tuple[str, Any]] = field(default_factory=dict)
    degraded: bool = False
    chunks: int = 0
    clean_chunks: dict[uuid.UUID, int] = field(default_factory=dict)
    window: tuple[int, int] = (0, 0)
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SyncSummary:
    """What the post-process hooks (#51 et al.) hand back for the push text.

    `reimbursable_count` comes from `rules.apply_rules` (#51): how many of the
    run's new rows a rule moved into the `reimbursable` bucket, which is what
    the spec's "4 new charges, 1 looks reimbursable" wording in `_build_push`
    reports.
    """

    reimbursable_count: int = 0


@dataclass(frozen=True)
class PushIntent:
    """What `run_sync` decided to notify about. Carried back to the caller rather
    than sent directly; see `notify`.
    """

    title: str
    body: str


@dataclass(frozen=True)
class ParsedTxn:
    """The bank-owned fields `_upsert_transaction` writes, parsed once out of one
    Transaction payload (protocol "Transaction").
    """

    external_id: str
    amount: Decimal
    description: str
    pending: bool
    transacted_at: datetime | None
    posted_date: Any


class _SyncFailed(Exception):
    """Carries an already-sanitized message for `_fail`. Never built from the
    Access URL, its credentials, or raw payload values.
    """


class _NoConnection(Exception):
    """No `simplefin_connections` row exists for this user."""


def lock_user(db: Session, user_id: uuid.UUID) -> None:
    """Block until this transaction holds the advisory lock for `user_id`.

    Mirrors `app.auth.tokens._lock_family`: `pg_advisory_xact_lock` over
    `hashtextextended`, so a manual "Sync now" and the scheduled nightly job for the
    same user can never run their upserts at the same time. Released automatically
    at commit or rollback -- whoever calls `run_sync` must eventually do one or the
    other.
    """
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(user_id), 0))))


def ensure_cooldown_elapsed(
    db: Session, user_id: uuid.UUID, now: datetime, trigger: RunTrigger
) -> None:
    """Raise `RateLimited` for a manual "Sync now" within `RUN_COOLDOWN` of the
    caller's last run. A no-op for the scheduled trigger, which isn't rate
    limited by this (APScheduler only ever fires it once a day).

    Called after `lock_user`, inside the same per-user-locked transaction
    `run_sync` already holds: two near-simultaneous taps can no longer both pass
    this check and then run back-to-back -- the second blocks on the lock until
    the first commits, and by the time it gets the lock, the first's row is
    already visible to this very query.
    """
    if trigger != RunTrigger.manual:
        return
    last_started_at = db.scalars(
        select(SyncRun.started_at)
        .where(SyncRun.user_id == user_id)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    ).first()
    if last_started_at is None:
        return
    next_allowed = last_started_at + RUN_COOLDOWN
    if now < next_allowed:
        raise RateLimited(math.ceil((next_allowed - now).total_seconds()))


def _sanitize_errlist(errlist: Any) -> list[dict[str, Any]]:
    """Keep only the protocol's own Error fields, and only from entries that are
    actually objects -- a non-dict entry (a server deviating from the protocol)
    is dropped rather than raising.
    """
    if not isinstance(errlist, list):
        return []
    return [
        {k: v for k, v in error.items() if k in _ERRLIST_FIELDS}
        for error in errlist
        if isinstance(error, dict)
    ]


def _errlist_ids(errlist: list[dict[str, Any]]) -> set[str]:
    """Every `account_id`/`conn_id` the sanitized `errlist` names."""
    return {
        value
        for error in errlist
        for value in (error.get("account_id"), error.get("conn_id"))
        if isinstance(value, str)
    }


def _response_is_degraded(payload: dict[str, Any], errlist: list[dict[str, Any]]) -> bool:
    """Whether no account can be trusted to have listed all its rows: an errlist
    entry that names neither an account nor a connection (a `gen.*` error
    protects nobody by id), or a non-empty legacy `errors` list, which is how a
    protocol-v1 server reports problems and which the errlist never shows.
    """
    return bool(payload.get("errors")) or any(not _errlist_ids([error]) for error in errlist)


def _sanitize_account_error(exc: Exception, account_id: Any) -> dict[str, Any]:
    """One errlist-shaped entry for an account `_ingest_account` had to skip:
    the exception's type name, never its message or any payload value, plus the
    account id when SimpleFIN gave one as a string.
    """
    error: dict[str, Any] = {
        "code": "gen",
        "msg": f"Unexpected data from SimpleFIN ({type(exc).__name__})",
    }
    if isinstance(account_id, str):
        error["account_id"] = account_id
    return error


def _fail(db: Session, run: SyncRun, now: datetime, message: str) -> SyncRun:
    """Mark `run` failed with one sanitized, plain-text error and flush it.

    `message` must never be built from the Access URL, its credentials, or raw
    payload values; every caller below passes either a fixed string, `simplefin.
    fetch`'s own already-sanitized exception text, or an exception type name.
    """
    logger.warning("Sync failed: {}", message)
    run.finished_at = now
    run.status = RunStatus.failed
    run.errors = [{"code": "gen", "msg": message}]
    db.flush()
    return run


def _finalize_run(
    run: SyncRun, now: datetime, result: IngestResult, errors: list[dict[str, Any]]
) -> None:
    run.finished_at = now
    run.new_count = len(result.new_ids)
    run.updated_count = len(result.updated_ids)
    run.errors = errors
    run.status = RunStatus.partial if errors else RunStatus.ok


def fingerprint(account_id: uuid.UUID, posted_date: Any, amount: Decimal, description: str) -> str:
    """Hash of account, date, amount and normalized description (spec "Data
    model"), for matching rows that carry no bank id -- CSV import and the
    manual-to-bank matcher (#52) dedupe against this.
    """
    normalized = normalize_text(description)
    raw = f"{account_id}|{posted_date.isoformat()}|{amount}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _institution_name(
    account_payload: dict[str, Any], connections: list[dict[str, Any]]
) -> str | None:
    """The connection's human-friendly name for this account.

    Some servers send `conn_name` directly on the account (seen in the protocol's
    own examples); that's used as-is. Otherwise, the account's `conn_id` is looked
    up in the response's `connections` list. `None` if neither has it.
    """
    conn_name = account_payload.get("conn_name")
    if conn_name:
        return conn_name
    conn_id = account_payload.get("conn_id")
    for connection in connections:
        if conn_id is not None and connection.get("conn_id") == conn_id:
            return connection.get("name")
    return None


def _heuristic_kind(name: str, balance: Decimal) -> AccountKind:
    if _LOAN_NAME.search(name):
        return AccountKind.loan
    if balance < 0:
        return AccountKind.credit_card
    lowered = name.lower()
    if any(hint in lowered for hint in _CREDIT_CARD_HINTS):
        return AccountKind.credit_card
    return AccountKind.checking


def _upsert_account(
    db: Session,
    user_id: uuid.UUID,
    account_payload: dict[str, Any],
    connections: list[dict[str, Any]],
    now: datetime,
) -> Account:
    """Upsert by `(user_id, simplefin_account_id)`.

    `name`, `kind` and `institution` are set only on the INSERT branch; the UPDATE
    branch touches only `updated_at`, so a user's own edit to any of them survives
    every later sync.
    """
    simplefin_account_id = account_payload["id"]
    name = account_payload.get("name") or simplefin_account_id
    balance = Decimal(account_payload["balance"])
    stmt = (
        insert(Account)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            simplefin_account_id=simplefin_account_id,
            name=name,
            kind=_heuristic_kind(name, balance),
            institution=_institution_name(account_payload, connections),
        )
        .on_conflict_do_update(
            index_elements=["user_id", "simplefin_account_id"], set_={"updated_at": now}
        )
        .returning(Account)
    )
    return db.scalars(stmt, execution_options={"populate_existing": True}).one()


def _upsert_balance_snapshot(
    db: Session,
    user_id: uuid.UUID,
    account: Account,
    account_payload: dict[str, Any],
    now: datetime,
) -> None:
    """Upsert on `(account_id, date from balance-date)`, keeping SimpleFIN's sign
    (card debt negative, same as `transactions.amount`).
    """
    balance = Decimal(account_payload["balance"])
    snapshot_date = datetime.fromtimestamp(account_payload["balance-date"], tz=UTC).date()
    stmt = (
        insert(BalanceSnapshot)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            account_id=account.id,
            date=snapshot_date,
            balance=balance,
        )
        .on_conflict_do_update(
            index_elements=["account_id", "date"], set_={"balance": balance, "updated_at": now}
        )
    )
    db.execute(stmt)


def _has_split_children(db: Session, user_id: uuid.UUID, transaction_id: uuid.UUID) -> bool:
    """True when some other transaction points at `transaction_id` via `parent_id`.

    A thin wrapper around the shared `app.services.splits.has_split_children`
    predicate for a single row: the `where` clause's own reference to `Transaction`
    gives the EXISTS subquery's correlation something to bind to.
    """
    return bool(
        db.scalar(
            select(has_split_children()).where(
                Transaction.id == transaction_id, Transaction.user_id == user_id
            )
        )
    )


def _parse_transaction(txn_payload: dict[str, Any], now: datetime) -> ParsedTxn:
    """Pull the fields sync writes out of one Transaction payload.

    Raises `KeyError`/`TypeError`/`decimal.InvalidOperation` on a missing or
    malformed `id`/`amount`; `_ingest_account` catches those for the whole
    account.
    """
    external_id = txn_payload["id"]
    if not isinstance(external_id, str):
        raise TypeError("transaction id is not a string")
    amount = Decimal(txn_payload["amount"])
    description = txn_payload.get("description") or ""
    transacted_epoch = txn_payload.get("transacted_at")
    transacted_at = datetime.fromtimestamp(transacted_epoch, tz=UTC) if transacted_epoch else None
    posted_epoch = txn_payload.get("posted") or 0
    # The protocol lets `posted` be 0 *instead of* setting `pending`, so `posted
    # == 0` on its own means pending even with no explicit `pending` key.
    pending = bool(txn_payload.get("pending", False)) or not txn_payload.get("posted")
    if posted_epoch:
        posted_date = datetime.fromtimestamp(posted_epoch, tz=UTC).date()
    elif transacted_at is not None:
        posted_date = transacted_at.date()
    else:
        posted_date = now.date()
    return ParsedTxn(external_id, amount, description, pending, transacted_at, posted_date)


def _bank_owned_update(
    db: Session, user_id: uuid.UUID, parsed: ParsedTxn, existing: Transaction | None, now: datetime
) -> dict[str, Any]:
    """The ON CONFLICT `set_` for an existing row: every bank-owned field,
    always including `amount` -- the bank is the source of truth for it, split
    parent or not (`app.services.splits`). `reviewed` is reset to `False` only on
    the one sync where a split parent's amount actually changes, so a later,
    identical sync doesn't re-flag a row the user already reviewed; the children
    are left exactly as they are, for the user to re-split against the new amount.
    """
    update_set: dict[str, Any] = {
        "amount": parsed.amount,
        "posted_date": parsed.posted_date,
        "pending": parsed.pending,
        "description_raw": parsed.description,
        "transacted_at": parsed.transacted_at,
        "updated_at": now,
    }
    if (
        existing is not None
        and existing.amount != parsed.amount
        and _has_split_children(db, user_id, existing.id)
    ):
        update_set["reviewed"] = False
        logger.warning(
            "Sync changed amount on split parent {}; flagged unreviewed for re-split.",
            existing.id,
        )
    return update_set


def _upsert_transaction(
    db: Session, user_id: uuid.UUID, account: Account, parsed: ParsedTxn, now: datetime
) -> tuple[Transaction, bool, bool]:
    """Upsert on `(account_id, external_id)`. Never touches `category_id`,
    `bucket`, `notes`, `event_id` or `merchant` -- every one of them the user's to
    set -- except `reviewed`, which `_bank_owned_update` can reset; see its
    docstring. New rows default to `bucket=personal` and `reviewed=False`.

    Returns `(row, created, was_pending)`; `created` is told apart the same way
    `app.services.devices.register_device` does: equal `created_at`/`updated_at`
    only happens on the branch that just inserted the row. `was_pending` is
    whether the pre-upsert row (`None` for a brand-new one) was pending, for the
    caller to tell a pending->posted flip from an ordinary update.
    """
    existing = db.scalars(
        select(Transaction).where(
            Transaction.account_id == account.id, Transaction.external_id == parsed.external_id
        )
    ).one_or_none()
    was_pending = existing is not None and existing.pending

    update_set = _bank_owned_update(db, user_id, parsed, existing, now)
    fp = fingerprint(account.id, parsed.posted_date, parsed.amount, parsed.description)
    update_set["fingerprint"] = fp
    stmt = (
        insert(Transaction)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            account_id=account.id,
            posted_date=parsed.posted_date,
            transacted_at=parsed.transacted_at,
            amount=parsed.amount,
            description_raw=parsed.description,
            source=Source.simplefin,
            external_id=parsed.external_id,
            pending=parsed.pending,
            bucket=Bucket.personal,
            reviewed=False,
            fingerprint=fp,
        )
        .on_conflict_do_update(index_elements=["account_id", "external_id"], set_=update_set)
        .returning(Transaction)
    )
    row = db.scalars(stmt, execution_options={"populate_existing": True}).one()
    return row, row.created_at == row.updated_at, was_pending


def _ingest_transaction(
    db: Session,
    user_id: uuid.UUID,
    account: Account,
    txn_payload: Any,
    now: datetime,
    result: IngestResult,
    seen: set[str],
) -> None:
    if not isinstance(txn_payload, dict):
        raise TypeError("transaction payload is not an object")
    parsed = _parse_transaction(txn_payload, now)
    seen.add(parsed.external_id)
    row, created, was_pending = _upsert_transaction(db, user_id, account, parsed, now)
    newly_posted = not parsed.pending and (created or was_pending)
    if created:
        result.new_ids.append(row.id)
        if row.amount < 0:
            result.money_out_new += 1
    else:
        result.updated_ids.append(row.id)
    if newly_posted:
        result.newly_posted_ids.append(row.id)


def _ingest_account(
    db: Session,
    user_id: uuid.UUID,
    account_payload: Any,
    connections: list[dict[str, Any]],
    now: datetime,
    result: IngestResult,
) -> None:
    """Upsert one account, its balance snapshot and transactions inside a
    savepoint. An account named in `result.unreliable_ids`, or without a
    `transactions` list, is ingested but not recorded in `result.seen_external_ids`.

    A malformed account (a missing/non-numeric `balance`, a transaction with no
    `amount`, a non-object entry) rolls back to the savepoint and is recorded as
    one sanitized error on `result` -- never raised -- so one bad account never
    costs the rest of the pull, or the `sync_runs` row itself.
    """
    account_id = account_payload.get("id") if isinstance(account_payload, dict) else None
    try:
        with db.begin_nested():
            if not isinstance(account_payload, dict):
                raise TypeError("account payload is not an object")
            account = _upsert_account(db, user_id, account_payload, connections, now)
            _upsert_balance_snapshot(db, user_id, account, account_payload, now)
            seen: set[str] = set()
            transactions = account_payload.get("transactions")
            for txn_payload in transactions if isinstance(transactions, list) else []:
                _ingest_transaction(db, user_id, account, txn_payload, now, result, seen)
            reliable = {account_payload["id"], account_payload.get("conn_id")}.isdisjoint(
                result.unreliable_ids
            )
            result.account_keys[account.id] = (
                account_payload["id"],
                account_payload.get("conn_id"),
            )
            if isinstance(transactions, list) and reliable:
                result.seen_external_ids.setdefault(account.id, set()).update(seen)
                result.clean_chunks[account.id] = result.clean_chunks.get(account.id, 0) + 1
            else:
                result.unreliable_ids.add(account_payload["id"])
    except _ACCOUNT_PARSE_ERRORS as exc:
        if isinstance(account_id, str):
            result.unreliable_ids.add(account_id)
        result.errors.append(_sanitize_account_error(exc, account_id))


def _ingest(
    db: Session, user_id: uuid.UUID, payload: dict[str, Any], now: datetime, result: IngestResult
) -> None:
    """Upsert every account in `payload["accounts"]` into `result`. Each account
    gets its own savepoint via `_ingest_account`, so a malformed one is skipped and
    recorded rather than losing the others.
    """
    result.chunks += 1
    errlist = _sanitize_errlist(payload.get("errlist"))
    result.unreliable_ids |= _errlist_ids(errlist)
    connections_raw = payload.get("connections")
    connections = connections_raw if isinstance(connections_raw, list) else []
    accounts_raw = payload.get("accounts")
    accounts = accounts_raw if isinstance(accounts_raw, list) else []
    for account_payload in accounts:
        _ingest_account(db, user_id, account_payload, connections, now, result)
    if _response_is_degraded(payload, errlist):
        logger.warning(
            "Skipping the pending reconcile this run: the SimpleFIN response is degraded"
        )
        result.degraded = True
        result.seen_external_ids.clear()


def _finish_ingest(result: IngestResult) -> None:
    """Settle a result that took one or more `_ingest` calls: an account named
    unreliable by any chunk, or missing or unclean in any chunk, loses its seen
    set (a chunk that ingested it before a later chunk's errlist named it must not
    leave it looking reliable, nor one a chunk left out), a
    degraded chunk empties every seen set, and a row an overlap re-read in a later
    chunk counts once -- as new if any chunk created it.
    """
    if result.degraded:
        result.seen_external_ids.clear()
    for account_id, keys in result.account_keys.items():
        if (
            not result.unreliable_ids.isdisjoint(keys)
            or result.clean_chunks.get(account_id, 0) < result.chunks
        ):
            result.seen_external_ids.pop(account_id, None)
    result.new_ids = list(dict.fromkeys(result.new_ids))
    created = set(result.new_ids)
    result.updated_ids = [i for i in dict.fromkeys(result.updated_ids) if i not in created]
    result.newly_posted_ids = list(dict.fromkeys(result.newly_posted_ids))
    result.errors = [e for i, e in enumerate(result.errors) if e not in result.errors[:i]]


def chunk_window(window: tuple[int, int]) -> list[tuple[int, int]]:
    """Split `window` into consecutive requests of at most `MAX_CHUNK`, each
    starting `CHUNK_OVERLAP` before the previous one ended. A window that fits
    in one request comes back as itself.
    """
    start, end = window
    chunks: list[tuple[int, int]] = []
    while True:
        chunk_end = min(start + int(MAX_CHUNK.total_seconds()), end)
        chunks.append((start, chunk_end))
        if chunk_end >= end:
            return chunks
        start = chunk_end - int(CHUNK_OVERLAP.total_seconds())


# A `since` of MAX_BACKFILL_DAYS ago starts at local midnight, so allow two days of slack.
assert (
    len(chunk_window((0, int(timedelta(days=MAX_BACKFILL_DAYS + 2).total_seconds()))))
    <= MAX_BACKFILL_REQUESTS
), "MAX_BACKFILL_DAYS needs more SimpleFIN requests than MAX_BACKFILL_REQUESTS"


def _load_access_url(db: Session, settings: Settings, user_id: uuid.UUID) -> str:
    """Raises `_NoConnection` if the user has never claimed a setup token, or
    `cryptography.fernet.InvalidToken` if the stored blob doesn't decrypt with
    the current key.
    """
    connection = db.scalars(
        select(SimplefinConnection).where(SimplefinConnection.user_id == user_id)
    ).one_or_none()
    if connection is None:
        raise _NoConnection()
    return simplefin.decrypt_access_url(settings, connection.access_url_encrypted)


def _fetch_chunks(
    db: Session,
    user_id: uuid.UUID,
    settings: Settings,
    window: tuple[int, int],
    sf_transport: httpx.BaseTransport | None,
) -> list[dict[str, Any]]:
    """Load the Access URL and fetch every chunk of `window`, in order. Raises
    `_SyncFailed` with an already-sanitized message for every failure mode.
    """
    try:
        access_url = _load_access_url(db, settings, user_id)
    except _NoConnection:
        raise _SyncFailed("No SimpleFIN connection is set up for this user.") from None
    except InvalidToken:
        raise _SyncFailed("Could not decrypt the stored SimpleFIN Access URL") from None

    payloads: list[dict[str, Any]] = []
    for chunk in chunk_window(window):
        try:
            payload = simplefin.fetch(access_url, *chunk, transport=sf_transport)
        except simplefin.SimplefinRequestFailed as exc:
            raise _SyncFailed(str(exc)) from exc
        if not isinstance(payload, dict):
            raise _SyncFailed("SimpleFIN returned an unexpected response shape") from None
        payloads.append(payload)
    return payloads


def _fetch_and_ingest(
    db: Session,
    user_id: uuid.UUID,
    settings: Settings,
    now: datetime,
    window: tuple[int, int],
    sf_transport: httpx.BaseTransport | None,
) -> tuple[list[dict[str, Any]], IngestResult]:
    """Fetch `window` in chunks (`_fetch_chunks`), then ingest every response
    into one result. Returns the run's errlist (each chunk's, repeats dropped).
    Raises `_SyncFailed` with an already-sanitized message for every failure
    mode, including an unexpected error anywhere in `_ingest` -- so `run_sync`
    can always persist a `failed` run rather than losing the row.

    Every chunk is fetched before any is ingested, so a failed chunk fails the run
    with nothing written: ingesting the earlier chunks alone would leave rows
    that a retry sees as updates, never as new, and the rules and transfer
    matcher would skip them.
    """
    payloads = _fetch_chunks(db, user_id, settings, window, sf_transport)
    result = IngestResult(window=window)
    errlist: list[dict[str, Any]] = []
    try:
        for payload in payloads:
            _ingest(db, user_id, payload, now, result)
            for error in _sanitize_errlist(payload.get("errlist")):
                if error not in errlist:
                    errlist.append(error)
        _finish_ingest(result)
    except Exception as exc:
        raise _SyncFailed(
            f"Unexpected error while processing the sync ({type(exc).__name__})"
        ) from exc
    return errlist, result


def _count_reimbursable(db: Session, transaction_ids: list[uuid.UUID]) -> int:
    """How many of `transaction_ids` are, right now, in the `reimbursable` bucket.

    Counted once every post-process hook has run (#51 MV-3), not inside
    `rules.apply_rules` itself: a later hook in the same pipeline (the manual
    matcher, #52) can still move a row's bucket after a rule sets it, so
    counting mid-pipeline could report a row as reimbursable that the matcher
    then turned back to personal.
    """
    if not transaction_ids:
        return 0
    return (
        db.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.id.in_(transaction_ids), Transaction.bucket == Bucket.reimbursable)
        )
        or 0
    )


def _post_process(db: Session, run: SyncRun, result: IngestResult) -> SyncSummary:
    """The pipeline that runs on newly-synced transactions, in this order:

    1. auto-categorization rules (#51), on `result.new_ids` -- brand-new bank
       rows, never split children or parents. First, so a rule's category/bucket
       guess is on a row before any merge below folds user values over it (the
       fill-if-null fields of `app.services.matching.absorb`), never the other
       way around.
    2. pending reconcile (#50): a vanished pending row absorbs its re-issued
       posted row (the posted row's id is deleted, the pending row's id lives)
       or, if cancelled, is deleted. Before the manual matcher on purpose: if
       the matcher ran first, it would fold the posted row into a manual
       capture, so the pending row would find no posted candidate and be
       deleted or stranded, losing the edits the user made on it.
    3. the manual-entry matcher (#52), on `newly_posted_ids` plus the pending
       rows reconcile just turned posted, so manual row M, pending row P and
       posted row B end as one row under M's id (B folded into P, P into M).
    4. the transfer matcher (#61), on every row this run created or posted,
       resolved to survivors: a manual row that absorbed a bank row keeps its
       own id, so `new_ids` alone would miss it. After the merges, so it only
       sees rows that still exist.
    5. subscription matching (#56), on the same survivor ids: a charge whose
       merchant and amount fit one active subscription is linked to it and
       advances its `next_due`. After the transfer matcher, which never leaves
       a `transfer` row for this step to link.

    `reimbursable_count` is computed last (#51 MV-3) -- a bank row a later hook
    absorbed no longer exists under its `new_ids` id, so it drops out of the
    count on its own.
    """
    rules.apply_rules(db, run.user_id, result.new_ids)
    window_start = datetime.fromtimestamp(result.window[0], tz=UTC).date()
    reconciled = reconcile.reconcile_pending(
        db,
        run.user_id,
        run.started_at,
        seen_external_ids=result.seen_external_ids,
        window_start=window_start,
    )
    manual = matching.match_manual_entries(
        db,
        run.user_id,
        [*result.newly_posted_ids, *reconciled.absorbed_ids],
        window_start=window_start,
    )
    touched_ids = [
        *result.new_ids,
        *result.newly_posted_ids,
        *reconciled.absorbed_ids,
        *manual.survivor_ids,
    ]
    matching.match_transfers(db, run.user_id, touched_ids, rules.load_rule_index(db, run.user_id))
    subscription_matching.match_subscriptions(db, run.user_id, touched_ids)
    return SyncSummary(reimbursable_count=_count_reimbursable(db, result.new_ids))


def _run_post_process(
    db: Session, run: SyncRun, result: IngestResult
) -> tuple[SyncSummary, list[dict[str, Any]]]:
    """Run `_post_process` in a savepoint: a hook bug rolls back to here, not past
    the ingest or the run row, and is recorded as one sanitized error. The run
    still finishes `partial`, keeping the ingest it already did.
    """
    try:
        with db.begin_nested():
            summary = _post_process(db, run, result)
    except Exception as exc:
        return SyncSummary(), [
            {"code": "gen", "msg": f"Post-process failed ({type(exc).__name__})"}
        ]
    return summary, []


def _build_push(result: IngestResult, summary: SyncSummary) -> PushIntent:
    """Word the body from `money_out_new` when there is any -- "N new charges" --
    falling back to the plain `new_count` ("N new transactions") for a run whose
    only new rows are deposits, rather than the always-wrong "0 new charges".
    Appends the spec's "N look(s) reimbursable" once `summary.reimbursable_count`
    is ever nonzero (#51).
    """
    if result.money_out_new:
        count, noun = result.money_out_new, "charge"
    else:
        count, noun = len(result.new_ids), "transaction"
    body = f"{count} new {noun}{'' if count == 1 else 's'}"
    if summary.reimbursable_count:
        verb = "looks" if summary.reimbursable_count == 1 else "look"
        body += f", {summary.reimbursable_count} {verb} reimbursable"
    return PushIntent("Tally", body)


def _start_run(db: Session, user_id: uuid.UUID, now: datetime, trigger: RunTrigger) -> SyncRun:
    run = SyncRun(
        user_id=user_id,
        started_at=now,
        status=RunStatus.running,
        trigger=trigger,
        new_count=0,
        updated_count=0,
        errors=[],
    )
    db.add(run)
    db.flush()
    return run


def _default_window(
    db: Session, user_id: uuid.UUID, now: datetime, since: date | None
) -> tuple[int, int]:
    """`since` or, for a user with no `ok` or `partial` run (both ingested data;
    a `failed` run ingested nothing), Jan 1 of this year, from local midnight in
    `APP_TIMEZONE`; otherwise the last `LOOKBACK`.
    """
    end = int(now.timestamp())
    if since is None:
        has_prior_run = db.scalar(
            select(
                select(SyncRun.id)
                .where(
                    SyncRun.user_id == user_id,
                    SyncRun.status.in_((RunStatus.ok, RunStatus.partial)),
                )
                .exists()
            )
        )
        if has_prior_run:
            return int((now - LOOKBACK).timestamp()), end
        since = date(now.astimezone(APP_TIMEZONE).year, 1, 1)
    return int(datetime.combine(since, time.min, tzinfo=APP_TIMEZONE).timestamp()), end


def run_sync(
    db: Session,
    user_id: uuid.UUID,
    now: datetime,
    settings: Settings,
    *,
    trigger: RunTrigger,
    window: tuple[int, int] | None = None,
    since: date | None = None,
    sf_transport: httpx.BaseTransport | None = None,
) -> tuple[SyncRun, PushIntent | None]:
    """Pull `window` from SimpleFIN for `user_id` and upsert accounts, balance
    snapshots and transactions, logging the result as a `SyncRun`. Takes the
    per-user advisory lock for the whole call and checks the manual cooldown
    under it (`ensure_cooldown_elapsed`).

    The default window, when neither `window` nor `since` is given, is the last
    `LOOKBACK` -- except for a user with no `ok` or `partial` run yet, whose first sync
    backfills from Jan 1 of this year (#130). `since` backfills from that local
    day (`APP_TIMEZONE`) instead. Either way the fetch is split into chunks
    (`chunk_window`) and the post-process hooks run once over the whole range.

    Returns `(run, push)`: `run` is flushed but not committed, and `push`, if not
    `None`, is what the caller should pass to `notify` -- after its own commit,
    never before.

    Raises `ValueError` if both `window` and `since` are given.
    """
    if window is not None and since is not None:
        raise ValueError("Pass either window or since, not both.")
    lock_user(db, user_id)
    ensure_cooldown_elapsed(db, user_id, now, trigger)
    run = _start_run(db, user_id, now, trigger)
    window = window or _default_window(db, user_id, now, since)

    try:
        errlist, result = _fetch_and_ingest(db, user_id, settings, now, window, sf_transport)
    except _SyncFailed as exc:
        return _fail(db, run, now, str(exc)), None

    summary, hook_errors = _run_post_process(db, run, result)
    errors = errlist + result.errors + hook_errors
    _finalize_run(run, now, result, errors)

    push = _build_push(result, summary) if result.new_ids else None
    db.flush()
    return run, push


def notify(
    db: Session,
    user_id: uuid.UUID,
    now: datetime,
    settings: Settings,
    push: PushIntent | None,
    *,
    apns_client: httpx.Client | None = None,
) -> None:
    """Send `push` (from `run_sync`), after the caller's own commit -- never
    before, so a notification is never sent about rows that then roll back.

    Never raises: `apns.send_alert` already swallows its own failures, but this
    also guards building the client and this call's own `commit` (for a device
    `send_alert` pruned as dead), so a push failure is logged and never costs the
    caller the sync it already committed.
    """
    if push is None:
        return
    client = apns_client or apns.make_client()
    try:
        apns.send_alert(db, user_id, push.title, push.body, now, client, settings)
        db.commit()
    except Exception:
        logger.warning("Push notification failed for user {}", user_id)
    finally:
        if apns_client is None:
            client.close()


def list_sync_runs(db: Session, user_id: uuid.UUID, query: SyncRunListQuery) -> SyncRunPage:
    """Most recent first, for `GET /api/sync/runs`."""
    stmt = owned(SyncRun, user_id)
    if query.cursor is not None:
        cursor_started_at, cursor_id = decode_cursor(query.cursor)
        last = tuple_(cursor_started_at, cursor_id)
        stmt = stmt.where(tuple_(SyncRun.started_at, SyncRun.id) < last)

    stmt = stmt.order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
    rows, next_cursor = paginate(db, stmt, query.limit, lambda row: (row.started_at, row.id))

    return SyncRunPage(
        items=[SyncRunOut.model_validate(row) for row in rows], next_cursor=next_cursor
    )
