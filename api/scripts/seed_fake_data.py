"""Fill the database with fake data for manual testing and local development.

Seeds a single owner user, sign-in fixtures (passkeys, refresh tokens, auth events, a
push-notification device, an App Attest key), a fake SimpleFIN connection, accounts,
budgeted categories, subscriptions, a rule, an event, a few months of everyday
transactions plus a handful of special ones (a split, a linked transfer pair,
reimbursable charges and their expense reports), a receipt, income entries, balance
snapshots, sync runs, a mileage log (IRS rates, trips, a saved trip, an odometer
reading) and the Claude fixtures, so the app has something to look at. Everything is
made up: generic merchants, no real people, employers or account numbers. Extend
`SEEDERS` as more tables land.

Run locally, from api/:

    uv run python -m scripts.seed_fake_data

Never run this on the VM: it refuses outright because the VM always has
ENVIRONMENT=production set. Dev only.

Dates are never hard-coded: every row is placed relative to "today" in `APP_TIMEZONE`,
taken from the `now` passed to `seed`. The current month gets the everyday charges
whose day of the month has already come (so there is spending from the 1st on, and
nothing dated in the future), the three months before it are filled completely so the
dashboard trend has something to draw, and the special rows sit a fixed number of days
back from today, falling into the previous month when today is early in one. The newest
few everyday charges are left unreviewed, so the Inbox has about five rows on any date.

The owner is reused if one already exists (same "oldest user" rule as
`app.auth.enrollment.get_or_create_user`), so seeding never creates a second one. Every
other row uses a deterministic id (uuid5 from a fixed namespace) that does not depend on
the date, and `INSERT ... ON CONFLICT DO NOTHING`, so running this twice inserts nothing
the second time.

That holds on a later day too: a re-run finds every id already there and inserts
nothing, so the data stays anchored to the day of the first run and is never shifted,
duplicated or topped up. To get a fresh "current month" after time has passed, seed an
empty database again (drop and re-create it, then `alembic upgrade head`).

The fake passkey and refresh-token rows are fixtures, not credentials: they can't answer a
WebAuthn ceremony or redeem a refresh grant. A real sign-in still needs `scripts.enroll` to
issue an enrollment code and register an actual passkey.

Skips `enrollment_codes`, `webauthn_challenges` and `app_attest_challenges` (see
`SKIPPED_TABLES`): all are single-use and expire within minutes, so a seeded row would
already be stale by the time anyone looked at it.
"""

import calendar
import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple, Protocol

from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth.enrollment import get_or_create_user
from app.auth.tokens import new_refresh_token
from app.config import APP_TIMEZONE, get_settings
from app.db import make_engine
from app.models import (
    Account,
    AIAction,
    AIBatch,
    AISuggestion,
    AIUsage,
    AppAttestKey,
    AuthEvent,
    BalanceSnapshot,
    Category,
    ChatConversation,
    Device,
    Event,
    ImportMapping,
    IncomeEntry,
    Insight,
    MileageRate,
    MileageTrip,
    OdometerReading,
    Passkey,
    Receipt,
    ReceiptReading,
    RefreshToken,
    Reimbursement,
    ReviewFinding,
    Rule,
    SavedTrip,
    SimplefinConnection,
    Subscription,
    SyncRun,
    TaxReview,
    TaxReviewItem,
    Transaction,
    User,
    WeeklyReview,
)
from app.models.enums import (
    AccountKind,
    AIActionSource,
    AIActionStatus,
    AIBatchStatus,
    AIConfidence,
    AISuggestionStatus,
    ApnsEnvironment,
    AppAttestEnvironment,
    AuthEventKind,
    Bucket,
    Cadence,
    CategoryKind,
    FindingKind,
    IncomeType,
    InsightKind,
    MatchField,
    MatchType,
    ReceiptReadingStatus,
    ReimbursementStatus,
    RunStatus,
    RunTrigger,
    Source,
    SubscriptionKind,
    TaxReviewKind,
    TaxReviewStatus,
)
from app.models.import_mapping import SignConvention
from app.schemas.ai_actions import MarkReviewedStep, dump_steps

# Fixed namespace so every seeded row (besides the owner) keeps the same id across runs.
NAMESPACE = uuid.UUID("1545826c-3db5-4da3-b951-82c5b3af8212")

# A fixed key, not whatever SIMPLEFIN_ENCRYPTION_KEY the running settings carry: seeding
# must work before a real key is configured, and nothing ever decrypts this fake row.
_SEED_FERNET_KEY = Fernet(b"OWUYB1vw1Y6vAxUCxDwfP-57Yc6CXpggibcMYI1l49Y=")

# Single-use, short-TTL tables: a seeded row would already be stale by the time anyone
# looked at it, so there's nothing useful to seed here.
SKIPPED_TABLES = {"enrollment_codes", "webauthn_challenges", "app_attest_challenges"}


class _SeedableModel(Protocol):
    """Structural bound for `SEEDERS`: any mapped model with an id, not a closed union."""

    __tablename__: str
    id: Any


RowBuilder = Callable[[uuid.UUID, datetime], list[dict[str, object]]]


def _uuid(label: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, label)


def _today(now: datetime) -> date:
    """The seed's anchor day: `now` as a calendar date in the app's timezone."""
    return now.astimezone(APP_TIMEZONE).date()


def _days_ago(now: datetime, days: int) -> date:
    return _today(now) - timedelta(days=days)


def _month_start(today: date, months_back: int) -> date:
    """The first day of the month `months_back` months before `today`'s."""
    year, month0 = divmod(today.year * 12 + (today.month - 1) - months_back, 12)
    return date(year, month0 + 1, 1)


def _next_day_of_month(today: date, day: int) -> date:
    """The first date on or after `today` that falls on `day` (at most 28)."""
    this_month = today.replace(day=day)
    return this_month if this_month >= today else _month_start(today, -1).replace(day=day)


def _fake_bytes(label: str, length: int) -> bytes:
    """Deterministic filler bytes, long enough for a fake credential id or public key."""
    digest = hashlib.sha256(label.encode()).digest()
    return (digest * (length // len(digest) + 1))[:length]


def _fake_hex(label: str, length: int) -> str:
    """Deterministic filler hex string, long enough for a fake APNs token."""
    digest = hashlib.sha256(label.encode()).hexdigest()
    return (digest * (length // len(digest) + 1))[:length]


def _insert_new(db: Session, model: type[_SeedableModel], rows: list[dict[str, object]]) -> int:
    """Insert rows that don't already exist (by id or any unique column); return how many landed.

    `RETURNING id` rather than `result.rowcount`: Postgres reports an accurate count for
    `ON CONFLICT DO NOTHING`, but SQLAlchemy doesn't trust it and hard-codes rowcount to -1.
    """
    inserted = 0
    for row in rows:
        stmt = insert(model).values(**row).on_conflict_do_nothing().returning(model.id)
        inserted += len(db.execute(stmt).all())
    return inserted


def _passkey_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("passkey:1"),
            "user_id": owner_id,
            "credential_id": _fake_bytes("passkey:1:credential", 32),
            "public_key": _fake_bytes("passkey:1:public-key", 77),
            "sign_count": 0,
            "device_name": "Seed iPhone",
        },
        {
            "id": _uuid("passkey:2"),
            "user_id": owner_id,
            "credential_id": _fake_bytes("passkey:2:credential", 32),
            "public_key": _fake_bytes("passkey:2:public-key", 77),
            "sign_count": 4,
            "device_name": "Seed iPad",
        },
    ]


def _refresh_token_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # Real random tokens, hashed the same way a sign-in would; the tokens themselves are
    # discarded immediately and never printed, so they're useless even if someone saw them.
    _, active_hash = new_refresh_token()
    _, revoked_hash = new_refresh_token()
    return [
        {
            "id": _uuid("refresh_token:active"),
            "user_id": owner_id,
            "token_hash": active_hash,
            "family_id": _uuid("refresh_token:active:family"),
            "expires_at": now + timedelta(days=60),
            "revoked_at": None,
        },
        {
            "id": _uuid("refresh_token:revoked"),
            "user_id": owner_id,
            "token_hash": revoked_hash,
            "family_id": _uuid("refresh_token:revoked:family"),
            "expires_at": now + timedelta(days=60),
            "revoked_at": now,
        },
    ]


def _auth_event_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("auth_event:enroll"),
            "user_id": owner_id,
            "kind": AuthEventKind.enroll,
            "ip": "203.0.113.10",
            "user_agent": "Tally/1.0 (seed)",
        },
        {
            "id": _uuid("auth_event:sign_in:1"),
            "user_id": owner_id,
            "kind": AuthEventKind.sign_in,
            "ip": "203.0.113.10",
            "user_agent": "Tally/1.0 (seed)",
        },
        {
            "id": _uuid("auth_event:sign_in:2"),
            "user_id": owner_id,
            "kind": AuthEventKind.sign_in,
            "ip": "203.0.113.11",
            "user_agent": "Tally/1.0 (seed)",
        },
        {
            "id": _uuid("auth_event:sign_in_failed:1"),
            "user_id": None,
            "kind": AuthEventKind.sign_in_failed,
            "ip": "198.51.100.23",
            "user_agent": "curl/8.0",
        },
        {
            "id": _uuid("auth_event:sign_in_failed:2"),
            "user_id": None,
            "kind": AuthEventKind.sign_in_failed,
            "ip": "198.51.100.24",
            "user_agent": "curl/8.0",
        },
    ]


def _device_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("device:1"),
            "user_id": owner_id,
            "apns_token": _fake_hex("device:1:apns", 64),
            "environment": ApnsEnvironment.sandbox,
            "last_seen_at": now,
        },
    ]


def _app_attest_key_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    public_key = b"" + _fake_bytes("app_attest_key:1:public_key", 64)
    return [
        {
            "id": _uuid("app_attest_key:1"),
            "user_id": owner_id,
            "key_id": hashlib.sha256(public_key).digest(),
            "public_key": public_key,
            "sign_count": 0,
            "environment": AppAttestEnvironment.development,
        },
    ]


def _simplefin_connection_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    fake_access_url = "https://seed-user:seed-pass@bridge.simplefin.org/simplefin"
    return [
        {
            "id": _uuid("simplefin_connection:1"),
            "user_id": owner_id,
            "access_url_encrypted": _SEED_FERNET_KEY.encrypt(fake_access_url.encode()),
            "claimed_at": now,
        },
    ]


def _account_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("account:checking"),
            "user_id": owner_id,
            "name": "Everyday Checking",
            "kind": AccountKind.checking,
            "institution": "Example Bank",
            "simplefin_account_id": "seed-checking",
            "last4": "0000",
        },
        {
            "id": _uuid("account:credit_card"),
            "user_id": owner_id,
            "name": "Everyday Credit Card",
            "kind": AccountKind.credit_card,
            "institution": "Example Bank",
            "simplefin_account_id": "seed-credit-card",
            "last4": "0001",
            # A few days out, so Home always has a card bill coming due. Capped at 28 so
            # the day exists in every month.
            "due_day": min((_today(now) + timedelta(days=4)).day, 28),
            "apr": Decimal("24.99"),
            "planned_payment": Decimal("100.00"),
        },
        {
            # Not synced (no `simplefin_account_id`), so its manual rows are never
            # flagged as "unmatched manual" in the Inbox.
            "id": _uuid("account:cash"),
            "user_id": owner_id,
            "name": "Cash",
            "kind": AccountKind.cash,
        },
    ]


# Monthly budgets live on the category (`categories.monthly_budget`); the dashboard sums
# them into the month's total budget. These add up to $3,070.00.
_EXPENSE_CATEGORIES: list[tuple[str, str, str]] = [
    ("groceries", "Groceries", "500.00"),
    ("meals", "Meals", "250.00"),
    ("gas", "Gas", "160.00"),
    ("fun", "Fun", "180.00"),
    ("shopping", "Shopping", "350.00"),
    ("utilities", "Utilities", "280.00"),
    ("subscriptions", "Subscriptions", "60.00"),
    ("car", "Car", "450.00"),
    ("household", "Household", "290.00"),
    ("health", "Health", "250.00"),
    ("travel", "Travel", "300.00"),
]
_CATEGORY_EXTRAS: dict[str, dict[str, object]] = {
    "travel": {"tax_line": "travel"},
    "meals": {"tax_line": "meals", "deductible_pct": 50},
}


def _category_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    expense: list[dict[str, object]] = [
        {
            "id": _uuid(f"category:{key}"),
            "user_id": owner_id,
            "name": name,
            "kind": CategoryKind.expense,
            "monthly_budget": Decimal(budget),
            **_CATEGORY_EXTRAS.get(key, {}),
        }
        for key, name, budget in _EXPENSE_CATEGORIES
    ]
    return [
        *expense,
        {
            "id": _uuid("category:salary"),
            "user_id": owner_id,
            "name": "Salary",
            "kind": CategoryKind.income,
        },
    ]


def _subscription_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    today = _today(now)
    return [
        {
            "id": _uuid("subscription:car_payment"),
            "user_id": owner_id,
            "name": "Car Payment",
            "kind": SubscriptionKind.bill,
            # Outflow, like transactions.amount; the CHECK requires amount < 0.
            "amount": Decimal("-450.00"),
            "cadence": Cadence.monthly,
            "account_id": _uuid("account:checking"),
            "category_id": _uuid("category:car"),
            # Paid on the 5th in `_EVERYDAY`, so the next one is the next 5th.
            "next_due": _next_day_of_month(today, 5),
            "start_date": date(2024, 1, 5),
            "business_pct": None,
            "match_text": None,
        },
        {
            # Due in two days, so Home always has a subscription under "bills due".
            "id": _uuid("subscription:streaming"),
            "user_id": owner_id,
            "name": "Streamline Video",
            "kind": SubscriptionKind.subscription,
            "amount": Decimal("-15.99"),
            "cadence": Cadence.monthly,
            "account_id": _uuid("account:credit_card"),
            "category_id": _uuid("category:subscriptions"),
            "next_due": today + timedelta(days=2),
            "start_date": date(2025, 3, 6),
            "business_pct": None,
            "match_text": None,
        },
    ]


def _event_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("event:race_weekend"),
            "user_id": owner_id,
            "name": "Riverside Race Weekend",
            "series": "Club Series",
            "start_date": _days_ago(now, 12),
            "end_date": _days_ago(now, 11),
            "location": "Riverside Raceway",
        },
    ]


class _Charge(NamedTuple):
    """One everyday charge in the month template."""

    day: int
    category: str
    merchant: str
    amount: str
    account: str = "credit_card"
    bucket: Bucket = Bucket.personal
    subscription: str | None = None


# One month of ordinary spending, about $2,650 against the $3,070 budget (the special rows
# in `_special_transaction_rows` add roughly $330 more around today). Day 1 has a charge,
# so the current month is never empty; days 29-31 are skipped in months without them.
_EVERYDAY: list[_Charge] = [
    _Charge(1, "meals", "Harbor Coffee", "5.75"),
    _Charge(2, "groceries", "Greenleaf Market", "68.42"),
    _Charge(2, "shopping", "Main Street Department Store", "64.30"),
    _Charge(3, "meals", "Thai Basil", "33.15"),
    _Charge(3, "utilities", "City Electric", "94.36", "checking"),
    _Charge(4, "gas", "Sunrise Fuel", "38.20"),
    _Charge(5, "fun", "Climbing Gym Day Pass", "22.00"),
    _Charge(5, "car", "Car Payment", "450.00", "checking", subscription="car_payment"),
    _Charge(6, "groceries", "Fresh Market", "54.17"),
    _Charge(6, "subscriptions", "Streamline Video", "15.99", subscription="streaming"),
    _Charge(7, "meals", "Harbor Coffee", "6.25"),
    _Charge(7, "household", "Home Goods Depot", "46.20"),
    _Charge(8, "shopping", "Online Marketplace", "54.99"),
    _Charge(8, "travel", "Regional Rail", "46.00"),
    _Charge(9, "groceries", "Greenleaf Market", "71.30"),
    _Charge(9, "health", "Neighborhood Pharmacy", "18.75"),
    _Charge(10, "meals", "Pizza Corner", "24.80"),
    _Charge(10, "utilities", "Fiber Internet", "70.00", "checking"),
    _Charge(11, "fun", "Downtown Cinema", "28.50"),
    _Charge(11, "groceries", "Fresh Market", "44.92"),
    _Charge(11, "travel", "City Parking", "18.00"),
    _Charge(12, "gas", "Sunrise Fuel", "41.75"),
    _Charge(12, "meals", "Sushi Counter", "38.70"),
    _Charge(12, "household", "Furniture Outlet", "72.40"),
    _Charge(13, "groceries", "Corner Grocer", "13.85"),
    _Charge(14, "meals", "Noodle House", "18.40"),
    _Charge(14, "subscriptions", "Tune Cloud", "10.99"),
    _Charge(15, "shopping", "Outdoor Outfitters", "89.00"),
    _Charge(15, "health", "Fitness Club", "49.00"),
    _Charge(15, "fun", "Concert Tickets", "58.00"),
    _Charge(16, "groceries", "Fresh Market", "62.09"),
    _Charge(17, "meals", "Harbor Coffee", "5.75"),
    _Charge(17, "household", "Hardware Store", "63.87"),
    _Charge(18, "fun", "Bookshop", "26.48"),
    _Charge(18, "utilities", "Mobile Plan", "45.00", "checking"),
    # The one business charge a month: with no receipt, it lands in "Missing receipts".
    _Charge(19, "meals", "Taqueria", "27.35", bucket=Bucket.business),
    _Charge(20, "groceries", "Greenleaf Market", "58.66"),
    _Charge(20, "subscriptions", "Cloud Storage", "2.99"),
    _Charge(21, "gas", "Sunrise Fuel", "36.90"),
    _Charge(21, "utilities", "Water Utility", "38.12", "checking"),
    _Charge(22, "meals", "Thai Basil", "29.60"),
    _Charge(23, "shopping", "Online Marketplace", "37.45"),
    _Charge(23, "health", "Dental Copay", "85.00"),
    _Charge(24, "groceries", "Fresh Market", "47.21"),
    _Charge(25, "meals", "Harbor Coffee", "6.25"),
    _Charge(25, "subscriptions", "News Digest", "12.00"),
    _Charge(26, "fun", "Mini Golf", "31.00"),
    _Charge(26, "meals", "Brunch Cafe", "27.80"),
    _Charge(26, "travel", "Metro Transit", "24.50"),
    _Charge(27, "groceries", "Corner Grocer", "31.49"),
    _Charge(27, "shopping", "Shoe Store", "74.95"),
    _Charge(27, "health", "Walk-in Clinic Copay", "40.00"),
    _Charge(28, "meals", "Burger Shack", "16.90"),
    _Charge(28, "household", "Garden Center", "34.50"),
    _Charge(28, "gas", "Sunrise Fuel", "34.60"),
    _Charge(30, "meals", "Harbor Coffee", "5.75"),
]

# Bills cost the same every month; everything else is scaled a little per month so the
# dashboard trend isn't a flat line. Index = months before the current one.
_FIXED_CATEGORIES = {"utilities", "subscriptions", "car"}
_MONTH_FACTORS = (Decimal("1.00"), Decimal("0.94"), Decimal("1.06"), Decimal("0.89"))
_CENT = Decimal("0.01")
# With the Claude-suggested row in `_special_transaction_rows`, this makes five in the Inbox.
_UNREVIEWED_EVERYDAY = 4
_PAYCHECK = Decimal("2150.00")
_PAYDAYS = (1, 15)


def _everyday_rows(owner_id: uuid.UUID, today: date) -> list[dict[str, object]]:
    """`_EVERYDAY` laid over the current month (up to today) and the three before it.

    The newest `_UNREVIEWED_EVERYDAY` charges are left unreviewed for the Inbox, in the
    shapes a fresh sync leaves behind: some with a known merchant (so the Inbox suggests
    what an earlier, reviewed charge there was filed as) and one with nothing but the raw
    bank description.
    """
    dated: list[tuple[date, int, int, _Charge]] = []
    for months_back in range(len(_MONTH_FACTORS)):
        first = _month_start(today, months_back)
        last_day = calendar.monthrange(first.year, first.month)[1]
        for index, charge in enumerate(_EVERYDAY):
            if charge.day > last_day:
                continue
            posted = first.replace(day=charge.day)
            if posted <= today:
                dated.append((posted, months_back, index, charge))
    dated.sort(key=lambda item: (item[0], -item[2]), reverse=True)

    rows: list[dict[str, object]] = []
    for rank, (posted, months_back, index, charge) in enumerate(dated):
        amount = Decimal(charge.amount)
        if charge.category not in _FIXED_CATEGORIES:
            amount = (amount * _MONTH_FACTORS[months_back]).quantize(_CENT)
        reviewed = rank >= _UNREVIEWED_EVERYDAY
        rows.append(
            {
                "id": _uuid(f"transaction:everyday:{months_back}:{index}"),
                "user_id": owner_id,
                "account_id": _uuid(f"account:{charge.account}"),
                "posted_date": posted,
                "amount": -amount,
                "description_raw": charge.merchant.upper(),
                # Ranks 1 and 2 arrive uncategorized; rank 1 has no merchant either.
                "merchant": None if rank == 1 else charge.merchant,
                "category_id": None if rank in (1, 2) else _uuid(f"category:{charge.category}"),
                "bucket": charge.bucket,
                "source": Source.simplefin,
                "external_id": f"seed-everyday-{months_back}-{index}",
                "reviewed": reviewed,
                "subscription_id": (
                    _uuid(f"subscription:{charge.subscription}") if charge.subscription else None
                ),
            }
        )
    return rows


def _paydays(today: date) -> list[tuple[str, date]]:
    """(label, date) for each payday in the seeded months, the current month's included
    even when it hasn't come yet."""
    return [
        (f"{months_back}:{day}", _month_start(today, months_back).replace(day=day))
        for months_back in range(len(_MONTH_FACTORS))
        for day in _PAYDAYS
    ]


def _paycheck_rows(owner_id: uuid.UUID, today: date) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid(f"transaction:paycheck:{label}"),
            "user_id": owner_id,
            "account_id": _uuid("account:checking"),
            "posted_date": payday,
            "amount": _PAYCHECK,
            "description_raw": "PAYROLL DIRECT DEPOSIT",
            "merchant": "Payroll",
            "category_id": _uuid("category:salary"),
            "bucket": Bucket.income,
            "source": Source.simplefin,
            "external_id": f"seed-paycheck-{label}",
            "reviewed": True,
        }
        for label, payday in _paydays(today)
        if payday <= today
    ]


def _transaction_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    today = _today(now)
    return [
        *_everyday_rows(owner_id, today),
        *_paycheck_rows(owner_id, today),
        *_special_transaction_rows(owner_id, now),
    ]


def _special_transaction_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    """The rows that each show one feature, a fixed number of days back from today.

    All but the first are reviewed, and none is a week-old manual row on a synced
    account, so none of them adds to the Inbox by accident.
    """
    checking_id = _uuid("account:checking")
    credit_card_id = _uuid("account:credit_card")
    groceries_id = _uuid("category:groceries")
    travel_id = _uuid("category:travel")
    split_parent_id = _uuid("transaction:split:parent")
    event_id = _uuid("event:race_weekend")

    return [
        {
            # Fresh from the bank: unreviewed, no merchant or category. Claude's
            # suggestion (`_ai_suggestion_rows`) gives it the sparkle tag in the Inbox.
            "id": _uuid("transaction:synced"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _today(now),
            "amount": Decimal("-12.40"),
            "description_raw": "SQ *CORNER BAKERY",
            "source": Source.simplefin,
            "external_id": "seed-simplefin-1",
        },
        {
            "id": _uuid("transaction:manual"),
            "user_id": owner_id,
            "account_id": _uuid("account:cash"),
            "posted_date": _days_ago(now, 2),
            "amount": Decimal("-9.00"),
            "description_raw": "Farmers market stand",
            "merchant": "Farmers Market",
            "category_id": groceries_id,
            "source": Source.manual,
            "external_id": None,
            "reviewed": True,
        },
        {
            # The one business charge with a receipt (`_receipt_rows`).
            "id": _uuid("transaction:business"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 13),
            "amount": Decimal("-220.00"),
            "description_raw": "SKYWAY AIRLINES",
            "merchant": "Skyway Airlines",
            "category_id": travel_id,
            "bucket": Bucket.business,
            "source": Source.csv,
            "external_id": "seed-csv-1",
            "event_id": event_id,
            "reviewed": True,
        },
        {
            "id": _uuid("transaction:income"),
            "user_id": owner_id,
            "account_id": checking_id,
            "posted_date": _days_ago(now, 20),
            "amount": Decimal("1500.00"),
            "description_raw": "Freelance invoice payment",
            "category_id": _uuid("category:salary"),
            "bucket": Bucket.income,
            "source": Source.sheet_import,
            "external_id": "seed-sheet-1",
            "reviewed": True,
        },
        {
            # A split parent is reviewed (an unreviewed one means "needs re-splitting")
            # and never counted: its two children are.
            "id": split_parent_id,
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 8),
            "amount": Decimal("-100.00"),
            "description_raw": "WAREHOUSE CLUB",
            "merchant": "Warehouse Club",
            "category_id": groceries_id,
            "source": Source.simplefin,
            "external_id": "seed-simplefin-split",
            "reviewed": True,
        },
        {
            "id": _uuid("transaction:split:child:1"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 8),
            "amount": Decimal("-60.00"),
            "description_raw": "WAREHOUSE CLUB (household share)",
            "merchant": "Warehouse Club",
            "category_id": _uuid("category:household"),
            "source": Source.manual,
            "external_id": None,
            "parent_id": split_parent_id,
            "reviewed": True,
        },
        {
            "id": _uuid("transaction:split:child:2"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 8),
            "amount": Decimal("-40.00"),
            "description_raw": "WAREHOUSE CLUB (groceries share)",
            "merchant": "Warehouse Club",
            "category_id": groceries_id,
            "source": Source.manual,
            "external_id": None,
            "parent_id": split_parent_id,
            "reviewed": True,
        },
        {
            # transfer_pair_id is set afterwards, by `_link_transfer_pair`: each side needs the
            # other's id, and neither row exists yet for a single INSERT to reference.
            "id": _uuid("transaction:transfer:out"),
            "user_id": owner_id,
            "account_id": checking_id,
            "posted_date": _days_ago(now, 5),
            "amount": Decimal("-300.00"),
            "description_raw": "Payment to credit card",
            "bucket": Bucket.transfer,
            "source": Source.simplefin,
            "external_id": "seed-simplefin-transfer-out",
            "reviewed": True,
        },
        {
            "id": _uuid("transaction:transfer:into"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 5),
            "amount": Decimal("300.00"),
            "description_raw": "Payment from checking",
            "bucket": Bucket.transfer,
            "source": Source.simplefin,
            "external_id": "seed-simplefin-transfer-into",
            "reviewed": True,
        },
        # Reimbursable rows are money owed back, not spending. Owed-to-you (#64, #45) is
        # the four below that are in no report or in the unpaid one: $874.96.
        {
            # No report yet; `_link_reimbursement_transactions` leaves these two alone.
            "id": _uuid("transaction:reimbursable:unassigned"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 10),
            "amount": Decimal("-130.00"),
            "description_raw": "TRACKSIDE PARTS",
            "merchant": "Trackside Parts",
            "category_id": travel_id,
            "bucket": Bucket.reimbursable,
            "source": Source.simplefin,
            "external_id": "seed-reimbursable-parts",
            "reviewed": True,
        },
        {
            "id": _uuid("transaction:reimbursable:unassigned:2"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 11),
            "amount": Decimal("-64.00"),
            "description_raw": "SUNRISE FUEL",
            "merchant": "Sunrise Fuel",
            "category_id": travel_id,
            "bucket": Bucket.reimbursable,
            "source": Source.simplefin,
            "external_id": "seed-reimbursable-fuel",
            "event_id": event_id,
            "reviewed": True,
        },
        {
            # reimbursement_id is set afterwards, by `_link_reimbursement_transactions`:
            # it needs the report's id, and the report needs an income entry that in turn
            # needs a transaction id, so reimbursements can't be seeded before transactions.
            "id": _uuid("transaction:reimbursable:submitted"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 11),
            "amount": Decimal("-268.16"),
            "description_raw": "ROADWAY RENTAL CAR",
            "merchant": "Roadway Rental",
            "category_id": travel_id,
            "bucket": Bucket.reimbursable,
            "source": Source.simplefin,
            "external_id": "seed-reimbursable-rental",
            "event_id": event_id,
            "reviewed": True,
        },
        {
            "id": _uuid("transaction:reimbursable:submitted:2"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 12),
            "amount": Decimal("-412.80"),
            "description_raw": "RIVERSIDE INN",
            "merchant": "Riverside Inn",
            "category_id": travel_id,
            "bucket": Bucket.reimbursable,
            "source": Source.simplefin,
            "external_id": "seed-reimbursable-hotel",
            "event_id": event_id,
            "reviewed": True,
        },
        {
            # Already paid back, so not owed any more.
            "id": _uuid("transaction:reimbursable:paid"),
            "user_id": owner_id,
            "account_id": credit_card_id,
            "posted_date": _days_ago(now, 45),
            "amount": Decimal("-60.00"),
            "description_raw": "SUNRISE FUEL",
            "merchant": "Sunrise Fuel",
            "category_id": travel_id,
            "bucket": Bucket.reimbursable,
            "source": Source.simplefin,
            "external_id": "seed-reimbursable-paid",
            "reviewed": True,
        },
    ]


def _sync_run_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("sync_run:ok"),
            "user_id": owner_id,
            "started_at": now - timedelta(hours=2),
            "finished_at": now - timedelta(hours=2) + timedelta(seconds=4),
            "status": RunStatus.ok,
            "trigger": RunTrigger.scheduled,
            "new_count": 2,
            "updated_count": 1,
            "errors": [],
        },
        {
            "id": _uuid("sync_run:partial"),
            "user_id": owner_id,
            "started_at": now - timedelta(days=1),
            "finished_at": now - timedelta(days=1) + timedelta(seconds=3),
            "status": RunStatus.partial,
            "trigger": RunTrigger.manual,
            "new_count": 1,
            "updated_count": 0,
            "errors": [{"code": "con.auth", "msg": "My Bank login needs renewing at the Bridge."}],
        },
    ]


def _rule_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("rule:rental"),
            "user_id": owner_id,
            "priority": 10,
            "match_field": MatchField.description,
            "match_type": MatchType.contains,
            "pattern": "ROADWAY RENTAL",
            "set_merchant": "Roadway Rental",
            "set_category_id": _uuid("category:travel"),
            "set_bucket": Bucket.reimbursable,
        },
    ]


def _import_mapping_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("import_mapping:checking"),
            "user_id": owner_id,
            "account_id": _uuid("account:checking"),
            "date_column": "Date",
            "debit_column": "Debit",
            "credit_column": "Credit",
            "description_column": "Description",
            "date_format": "%m/%d/%Y",
            "sign_convention": SignConvention.negative_is_out,
            "has_header": True,
        },
    ]


def _link_transfer_pair(db: Session) -> None:
    """Point the fake transfer pair at each other.

    Can't be done inside `_transaction_rows`: a single INSERT can't reference a row that
    doesn't exist yet, and both sides need the other's id. The update is idempotent, so a
    repeat run just re-sets the same two values.
    """
    out_id = _uuid("transaction:transfer:out")
    into_id = _uuid("transaction:transfer:into")
    db.execute(update(Transaction).where(Transaction.id == out_id).values(transfer_pair_id=into_id))
    db.execute(update(Transaction).where(Transaction.id == into_id).values(transfer_pair_id=out_id))


def _receipt_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # `receipts.transaction_id` is ON DELETE RESTRICT: a real receipt would need moving to a
    # new transaction before its old one could be deleted. `file_path` points at a fake path;
    # nothing in this script (or the rest of the app, for a row it never uploaded) reads the
    # file, only the row, so a non-existent path is fine here.
    flight_day = _days_ago(now, 13)
    return [
        {
            "id": _uuid("receipt:1"),
            "user_id": owner_id,
            "transaction_id": _uuid("transaction:business"),
            "file_path": f"receipts/{flight_day:%Y/%m}/seed-skyway-airlines.jpg",
            "mime": "image/jpeg",
            "sha256": _fake_hex("receipt:1:sha256", 64),
        },
    ]


def _income_entry_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    today = _today(now)
    paychecks: list[dict[str, object]] = [
        {
            # Landed paydays are matched to their deposit; one still ahead this month is
            # expected but not received, with no transaction to match yet.
            "id": _uuid(f"income_entry:paycheck:{label}"),
            "user_id": owner_id,
            "date": payday,
            "type": IncomeType.other,
            "description": "Payroll deposit",
            "expected": _PAYCHECK,
            "received": _PAYCHECK if payday <= today else None,
            "transaction_id": _uuid(f"transaction:paycheck:{label}") if payday <= today else None,
        }
        for label, payday in _paydays(today)
    ]
    return [
        *paychecks,
        {
            # Matched to the sheet_import income transaction seeded above; both sides landed.
            "id": _uuid("income_entry:freelance"),
            "user_id": owner_id,
            "date": _days_ago(now, 20),
            "type": IncomeType.other,
            "description": "Freelance invoice payment",
            "expected": Decimal("1500.00"),
            "received": Decimal("1500.00"),
            "transaction_id": _uuid("transaction:income"),
        },
        {
            # What paid the settled expense report (`reimbursement:paid`).
            "id": _uuid("income_entry:reimbursement"),
            "user_id": owner_id,
            "date": _days_ago(now, 30),
            "type": IncomeType.reimbursement,
            "description": "Expense report #seed",
            "expected": Decimal("60.00"),
            "received": Decimal("60.00"),
        },
    ]


def _reimbursement_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # Transaction links are set afterwards by `_link_reimbursement_transactions`: a
    # single INSERT can't reference a `transactions` row that points back at it in the
    # other direction, and neither exists yet for the other to reference.
    return [
        {
            # No transactions attached yet; a freshly-opened report starts empty.
            "id": _uuid("reimbursement:draft"),
            "user_id": owner_id,
            "status": ReimbursementStatus.draft,
            "note": "Tools and fuel",
        },
        {
            "id": _uuid("reimbursement:submitted"),
            "user_id": owner_id,
            "status": ReimbursementStatus.submitted,
            "submitted_date": _days_ago(now, 6),
            "note": "Race weekend travel",
        },
        {
            # Linked to the matching reimbursement income entry (spec: "Mark paid links
            # the income entry").
            "id": _uuid("reimbursement:paid"),
            "user_id": owner_id,
            "status": ReimbursementStatus.paid,
            "submitted_date": _days_ago(now, 40),
            "paid_date": _days_ago(now, 30),
            "income_entry_id": _uuid("income_entry:reimbursement"),
            "note": "Test day fuel",
        },
    ]


def _link_reimbursement_transactions(db: Session) -> None:
    """Point the seeded reimbursable transactions at their reports.

    Can't be done inside `_transaction_rows`/`_reimbursement_rows`: `reimbursements.
    income_entry_id` and `transactions.reimbursement_id` form a cycle with
    `income_entries.transaction_id`, so something has to be linked after the fact.
    The two `transaction:reimbursable:unassigned` rows are left alone on purpose: in no
    report yet, they still count toward owed-to-you. Idempotent, like the transfer link.
    """
    links = {
        "transaction:reimbursable:submitted": "reimbursement:submitted",
        "transaction:reimbursable:submitted:2": "reimbursement:submitted",
        "transaction:reimbursable:paid": "reimbursement:paid",
    }
    for transaction_label, reimbursement_label in links.items():
        db.execute(
            update(Transaction)
            .where(Transaction.id == _uuid(transaction_label))
            .values(reimbursement_id=_uuid(reimbursement_label))
        )


def _balance_snapshot_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # Card debt is negative, same sign as transactions.amount (see the model's comment).
    # Both accounts have a balance dated today, so Home doesn't call the bank stale.
    snapshots = [
        ("credit_card:1", "credit_card", 60, "-1612.40"),
        ("credit_card:2", "credit_card", 30, "-1420.18"),
        ("credit_card:3", "credit_card", 7, "-1234.56"),
        ("credit_card:4", "credit_card", 0, "-1180.25"),
        ("checking:1", "checking", 7, "2987.13"),
        ("checking:2", "checking", 0, "3245.80"),
    ]
    return [
        {
            "id": _uuid(f"balance_snapshot:{label}"),
            "user_id": owner_id,
            "account_id": _uuid(f"account:{account}"),
            "date": _days_ago(now, days),
            "balance": Decimal(balance),
        }
        for label, account, days, balance in snapshots
    ]


def _mileage_rate_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # The IRS rates the seed migration gives every existing user (#167).
    rates = [
        (date(2025, 1, 1), Decimal("0.7000")),
        (date(2026, 1, 1), Decimal("0.7250")),
        (date(2026, 7, 1), Decimal("0.7600")),
    ]
    return [
        {
            "id": _uuid(f"mileage_rate:{effective_from.isoformat()}"),
            "user_id": owner_id,
            "effective_from": effective_from,
            "rate": rate,
        }
        for effective_from, rate in rates
    ]


def _mileage_trip_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # Two fixed trips on either side of the 2026-07-01 rate change, so line 9 prices trips
    # at two rates, and two recent ones placed from today.
    trips = [
        ("rate:before", date(2026, 6, 30), "31.2", "Parts pickup", "Parts shop"),
        ("rate:after", date(2026, 7, 1), "31.2", "Parts pickup", "Parts shop"),
        ("practice", _days_ago(now, 60), "94.3", "Practice day", "Riverside Raceway"),
        ("race_weekend", _days_ago(now, 12), "94.3", "Race weekend", "Riverside Raceway"),
    ]
    return [
        {
            "id": _uuid(f"mileage_trip:{label}"),
            "user_id": owner_id,
            "date": trip_date,
            "miles": Decimal(miles),
            "purpose": purpose,
            "start_location": "Home",
            "end_location": end,
        }
        for label, trip_date, miles, purpose, end in trips
    ]


def _saved_trip_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("saved_trip:parts_pickup"),
            "user_id": owner_id,
            "name": "Parts pickup",
            "miles": Decimal("31.2"),
            "purpose": "Parts pickup",
            "start_location": "Home",
            "end_location": "Parts shop",
        },
    ]


def _odometer_reading_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("odometer_reading:this_year"),
            "user_id": owner_id,
            "year": _today(now).year,
            "start_reading": Decimal("41250.0"),
            "end_reading": None,
        },
    ]


def _ai_usage_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # A couple of cents of Claude spend this month, so More shows a usage line (#168).
    return [
        {
            "id": _uuid(f"ai_usage:{feature}"),
            "user_id": owner_id,
            "feature": feature,
            "model": "claude-sonnet-5-5",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": 0,
            "batch": batch,
            "cost_usd": cost,
            "created_at": now,
        }
        for feature, input_tokens, output_tokens, cache_read, batch, cost in [
            ("inbox_suggestions", 12000, 1500, 9000, True, Decimal("0.020400")),
            ("quick_capture", 800, 120, 0, False, Decimal("0.002800")),
        ]
    ]


def _ai_batch_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("ai_batch:nightly"),
            "user_id": owner_id,
            "feature": "inbox_suggestions",
            "anthropic_id": "msgbatch_seed",
            "status": AIBatchStatus.ended,
        },
    ]


def _ai_suggestion_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # The synced row is in the Inbox with no merchant to look up in history, so this is
    # the suggestion it shows, with the sparkle tag (#169).
    return [
        {
            "id": _uuid("ai_suggestion:synced"),
            "user_id": owner_id,
            "transaction_id": _uuid("transaction:synced"),
            "batch_id": _uuid("ai_batch:nightly"),
            "status": AISuggestionStatus.ready,
            "category_id": _uuid("category:meals"),
            "bucket": Bucket.personal,
            "merchant": "Corner Bakery",
            "confidence": AIConfidence.high,
            "model": "claude-sonnet-5-5",
        },
    ]


def _receipt_reading_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # What Claude read off the seeded receipt (#170), with line items that add up.
    return [
        {
            "id": _uuid("receipt_reading:1"),
            "user_id": owner_id,
            "receipt_id": _uuid("receipt:1"),
            "status": ReceiptReadingStatus.ready,
            "attempts": 1,
            "merchant": "Skyway Airlines",
            "receipt_date": _days_ago(now, 13),
            "subtotal": Decimal("200.00"),
            "tax": Decimal("20.00"),
            "tip": None,
            "total": Decimal("220.00"),
            "line_items": [{"description": "Airfare, one way", "amount": "200.00"}],
            "category_id": None,
            "adds_up": True,
            "matches_transaction": True,
            "model": "claude-sonnet-5-5",
        },
    ]


def _weekly_review_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # Last full week, Monday to Sunday.
    today = _today(now)
    week_start = today - timedelta(days=today.weekday() + 7)
    return [
        {
            "id": _uuid("weekly_review:last_week"),
            "user_id": owner_id,
            "week_start": week_start,
            "week_end": week_start + timedelta(days=6),
            "summary": "A steady week, close to your usual pace. One purchase has no receipt.",
            "model": "claude-sonnet-5-5",
        },
    ]


def _review_finding_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("review_finding:missing_receipt"),
            "user_id": owner_id,
            "review_id": _uuid("weekly_review:last_week"),
            "kind": FindingKind.missing_receipt,
            "transaction_id": _uuid("transaction:reimbursable:unassigned"),
            "note": "A reimbursable parts purchase with no receipt attached.",
            "position": 0,
        },
    ]


def _chat_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # A one-question money chat (#174), in the shape the Messages API sends and takes.
    return [
        {
            "id": _uuid("chat:groceries"),
            "user_id": owner_id,
            "title": "How much on groceries this month?",
            "messages": [
                {"role": "user", "content": "How much on groceries this month?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "A little under your $500.00 Groceries budget so far.",
                        }
                    ],
                },
            ],
        },
    ]


def _ai_action_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # A change Claude proposed from the chat (#222), waiting for the user to confirm it.
    step = MarkReviewedStep(kind="mark_reviewed", ids=[_uuid("transaction:synced")])
    return [
        {
            "id": _uuid("ai_action:review_groceries"),
            "user_id": owner_id,
            "summary": "Mark the bakery charge as reviewed.",
            "status": AIActionStatus.proposed,
            "source": AIActionSource.chat,
            "conversation_id": _uuid("chat:groceries"),
            "steps": dump_steps([step]),
        },
    ]


def _insight_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    # A heads-up found by the nightly rules (#225), about today's bakery charge.
    return [
        {
            "id": _uuid("insight:unusual_groceries"),
            "user_id": owner_id,
            "kind": InsightKind.unusual_charge,
            "title": "Unusually large charge from SQ *CORNER BAKERY",
            "message": "$12.40 at SQ *CORNER BAKERY, where you usually spend about $5.00.",
            "subject_key": f"charge:{_uuid('transaction:synced')}",
            "category_id": None,
            "transaction_id": _uuid("transaction:synced"),
            "day": _today(now),
        },
    ]


def _tax_review_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("tax_review:this_year"),
            "user_id": owner_id,
            "year": _today(now).year,
            "status": TaxReviewStatus.ready,
            "summary": "Business spend is mostly travel and meals. One lunch has no receipt yet.",
            "model": "claude-sonnet-5-5",
        },
    ]


def _tax_review_item_rows(owner_id: uuid.UUID, now: datetime) -> list[dict[str, object]]:
    return [
        {
            "id": _uuid("tax_review_item:parts"),
            "user_id": owner_id,
            "review_id": _uuid("tax_review:this_year"),
            "kind": TaxReviewKind.missing_receipt,
            "transaction_id": _uuid("transaction:reimbursable:unassigned"),
            "note": "The parts purchase is a reimbursable expense with no receipt.",
            "position": 0,
        },
    ]


# Ordered by FK dependency: sign-in fixtures and the SimpleFIN connection reference only
# the owner user (seeded separately, above). Accounts and categories reference the
# owner; subscriptions reference accounts and categories; events reference only the
# owner; transactions reference accounts,
# categories and events (and, via `_link_transfer_pair`, each other); receipts, income
# entries and balance snapshots reference transactions/accounts/events. Reimbursements
# reference income entries: `transactions.reimbursement_id` is only set after every
# seeder, by `_link_reimbursement_transactions` (see its docstring for the cycle). The
# mileage tables reference only the owner.
# Adding a table: write one row-builder, add one line here.
SEEDERS: list[tuple[type[_SeedableModel], RowBuilder]] = [
    (Passkey, _passkey_rows),
    (RefreshToken, _refresh_token_rows),
    (AuthEvent, _auth_event_rows),
    (Device, _device_rows),
    (AppAttestKey, _app_attest_key_rows),
    (SimplefinConnection, _simplefin_connection_rows),
    (Account, _account_rows),
    (Category, _category_rows),
    (Subscription, _subscription_rows),
    (Rule, _rule_rows),
    (ImportMapping, _import_mapping_rows),
    (Event, _event_rows),
    (Transaction, _transaction_rows),
    (Receipt, _receipt_rows),
    (IncomeEntry, _income_entry_rows),
    (Reimbursement, _reimbursement_rows),
    (BalanceSnapshot, _balance_snapshot_rows),
    (SyncRun, _sync_run_rows),
    (MileageRate, _mileage_rate_rows),
    (MileageTrip, _mileage_trip_rows),
    (SavedTrip, _saved_trip_rows),
    (OdometerReading, _odometer_reading_rows),
    (AIUsage, _ai_usage_rows),
    (AIBatch, _ai_batch_rows),
    (AISuggestion, _ai_suggestion_rows),
    (ReceiptReading, _receipt_reading_rows),
    (WeeklyReview, _weekly_review_rows),
    (ReviewFinding, _review_finding_rows),
    (ChatConversation, _chat_rows),
    (AIAction, _ai_action_rows),
    (Insight, _insight_rows),
    (TaxReview, _tax_review_rows),
    (TaxReviewItem, _tax_review_item_rows),
]


def seed(db: Session, now: datetime) -> dict[str, int]:
    """Insert fake fixture rows for every seedable table; a repeat run inserts nothing.

    `now` is the seed's clock: every date is placed relative to that instant's day in
    `APP_TIMEZONE`, so a test can pin "today" by passing its own.

    Reuses the existing owner (oldest user, same rule as `get_or_create_user`) if one
    already exists instead of creating a second one. Every other row uses a deterministic
    id that doesn't depend on `now`, so re-running the script after the first time is a
    no-op, on the same day or a later one.
    """
    owner_existed = db.scalars(select(User.id).limit(1)).first() is not None
    owner = get_or_create_user(db, "Tally")
    db.flush()

    counts = {User.__tablename__: 0 if owner_existed else 1}
    for model, build_rows in SEEDERS:
        counts[model.__tablename__] = _insert_new(db, model, build_rows(owner.id, now))
    _link_transfer_pair(db)
    _link_reimbursement_transactions(db)
    return counts


def main() -> None:
    settings = get_settings()
    if settings.is_production:
        raise SystemExit("Refusing to seed fake data with ENVIRONMENT=production.")

    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            counts = seed(db, datetime.now(UTC))
            db.commit()
    finally:
        engine.dispose()

    for table, count in counts.items():
        print(f"{table}: {count} row(s) inserted")


if __name__ == "__main__":
    main()
