"""Shared row-builders for model tests, so a new required column is fixed in one place."""

from datetime import UTC, date, datetime
from decimal import Decimal

from app.models import (
    Account,
    AuthEvent,
    BalanceSnapshot,
    Category,
    Device,
    Event,
    ImportMapping,
    IncomeEntry,
    MileageRate,
    MileageTrip,
    OdometerReading,
    Receipt,
    Reimbursement,
    Rule,
    SavedTrip,
    SimplefinConnection,
    Subscription,
    SyncRun,
    Transaction,
    User,
)
from app.models.enums import (
    AccountKind,
    ApnsEnvironment,
    AuthEventKind,
    Cadence,
    CategoryKind,
    IncomeType,
    MatchField,
    MatchType,
    RunStatus,
    RunTrigger,
    Source,
)


def make_account(user: User, **kwargs: object) -> Account:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Everyday Checking",
        "kind": AccountKind.checking,
    }
    defaults.update(kwargs)
    return Account(**defaults)


def make_category(user: User, **kwargs: object) -> Category:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Travel - Airfare",
        "kind": CategoryKind.expense,
    }
    defaults.update(kwargs)
    return Category(**defaults)


def make_transaction(user: User, account: Account, **kwargs: object) -> Transaction:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "account_id": account.id,
        "posted_date": date(2026, 10, 1),
        "amount": Decimal("-12.34"),
        "description_raw": "HERTZ RENTAL CAR",
        "source": Source.manual,
    }
    defaults.update(kwargs)
    return Transaction(**defaults)


def make_receipt(user: User, transaction: Transaction, **kwargs: object) -> Receipt:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "transaction_id": transaction.id,
        "file_path": "2026/10/one.jpg",
        "mime": "image/jpeg",
        "sha256": "0" * 64,
    }
    defaults.update(kwargs)
    return Receipt(**defaults)


def make_income_entry(user: User, **kwargs: object) -> IncomeEntry:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "date": date(2026, 10, 1),
        "type": IncomeType.event_pay,
        "expected": Decimal("250.00"),
    }
    defaults.update(kwargs)
    return IncomeEntry(**defaults)


def make_reimbursement(user: User, **kwargs: object) -> Reimbursement:
    defaults: dict[str, object] = {"user_id": user.id}
    defaults.update(kwargs)
    return Reimbursement(**defaults)


def make_subscription(user: User, **kwargs: object) -> Subscription:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Spotify",
        "amount": Decimal("-11.99"),
        "cadence": Cadence.monthly,
        "business_pct": None,
        "match_text": None,
    }
    defaults.update(kwargs)
    return Subscription(**defaults)


def make_rule(user: User, **kwargs: object) -> Rule:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "priority": 10,
        "match_field": MatchField.description,
        "match_type": MatchType.contains,
        "pattern": "HERTZ",
        "set_merchant": "Hertz",
    }
    defaults.update(kwargs)
    return Rule(**defaults)


def make_auth_event(**kwargs: object) -> AuthEvent:
    """`user_id` defaults to `None`: unlike every other table, `auth_events` rows
    routinely have no owner (a failed sign-in from an unknown credential), so there's
    no `user` positional argument the way the other factories have one.
    """
    defaults: dict[str, object] = {
        "kind": AuthEventKind.sign_in,
        "ip": "203.0.113.5",
        "user_agent": "Tally/1.0 (test)",
    }
    defaults.update(kwargs)
    return AuthEvent(**defaults)


def make_event(user: User, **kwargs: object) -> Event:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Daytona 200",
        "start_date": date(2026, 3, 7),
        "end_date": date(2026, 3, 8),
    }
    defaults.update(kwargs)
    return Event(**defaults)


def make_simplefin_connection(user: User, **kwargs: object) -> SimplefinConnection:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "access_url_encrypted": b"fake-encrypted-access-url",
        "claimed_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return SimplefinConnection(**defaults)


def make_snapshot(user: User, account: Account, **kwargs: object) -> BalanceSnapshot:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "account_id": account.id,
        "date": date(2026, 10, 1),
        "balance": Decimal("1234.56"),
    }
    defaults.update(kwargs)
    return BalanceSnapshot(**defaults)


def make_device(user: User, **kwargs: object) -> Device:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "apns_token": "ab" * 32,
        "environment": ApnsEnvironment.sandbox,
        "last_seen_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return Device(**defaults)


def make_sync_run(user: User, **kwargs: object) -> SyncRun:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "started_at": datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 10, 1, 5, 1, tzinfo=UTC),
        "status": RunStatus.ok,
        "trigger": RunTrigger.scheduled,
        "new_count": 0,
        "updated_count": 0,
        "errors": [],
    }
    defaults.update(kwargs)
    return SyncRun(**defaults)


def make_import_mapping(user: User, account: Account, **kwargs: object) -> ImportMapping:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "account_id": account.id,
        "date_column": "Date",
        "amount_column": "Amount",
        "description_column": "Description",
        "date_format": "%m/%d/%Y",
    }
    defaults.update(kwargs)
    return ImportMapping(**defaults)


def make_mileage_trip(user: User, **kwargs: object) -> MileageTrip:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "date": date(2026, 10, 1),
        "miles": Decimal("24.6"),
        "purpose": "Client site visit",
    }
    defaults.update(kwargs)
    return MileageTrip(**defaults)


def make_saved_trip(user: User, **kwargs: object) -> SavedTrip:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Office to track",
        "miles": Decimal("31.2"),
        "purpose": "Race day setup",
    }
    defaults.update(kwargs)
    return SavedTrip(**defaults)


def make_odometer_reading(user: User, **kwargs: object) -> OdometerReading:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "year": 2026,
        "start_reading": Decimal("41250.0"),
    }
    defaults.update(kwargs)
    return OdometerReading(**defaults)


def make_mileage_rate(user: User, **kwargs: object) -> MileageRate:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "effective_from": date(2026, 1, 1),
        "rate": Decimal("0.7250"),
    }
    defaults.update(kwargs)
    return MileageRate(**defaults)
