"""Account creation and updates, plus the Cards screen, its manual balances, and
the monthly card log.

See `app.scoping` for the user-scoping helpers.
"""

import datetime as dt
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Row, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.orm import Session

from app.config import APP_TIMEZONE
from app.errors import Conflict, UnprocessableEntity
from app.models import Account, BalanceSnapshot, Transaction
from app.models.enums import AccountKind, Bucket
from app.schemas.accounts import (
    AccountCreate,
    AccountOut,
    AccountUpdate,
    CardInterestOut,
    CardLogOut,
    CardOut,
    InterestMonthOut,
    InterestSummaryOut,
    LoanOut,
    PayoffPlanOut,
)
from app.schemas.months import month_range
from app.scoping import get_owned, owned
from app.services.crud import apply_patch, create_idempotent
from app.services.normalize import normalize_text
from app.services.payoff import Debt, plan
from app.services.splits import has_split_children

# Balance history is a sanity check, not a real constraint: the earliest date a
# synced snapshot or a manual entry can plausibly be about, and today, since a card
# balance can't be known before it's sometime in the future.
_MIN_BALANCE_DATE = dt.date(2000, 1, 1)

#: Snapshot history is bounded so one card's log can't page forever.
_MAX_HISTORY_ROWS = 400


def list_accounts(db: Session, user_id: uuid.UUID, active: bool | None) -> list[Account]:
    stmt = owned(Account, user_id).order_by(Account.name)
    if active is not None:
        stmt = stmt.where(Account.active == active)
    return list(db.scalars(stmt))


def create_account(db: Session, user_id: uuid.UUID, data: AccountCreate) -> tuple[Account, bool]:
    return create_idempotent(db, Account, user_id, data.model_dump())


def update_account(db: Session, user_id: uuid.UUID, id: uuid.UUID, data: AccountUpdate) -> Account:
    account = get_owned(db, Account, id, user_id)
    return apply_patch(db, account, data)


def latest_balances(
    db: Session,
    user_id: uuid.UUID,
    account_ids: list[uuid.UUID],
    on_or_before: dt.date | None = None,
) -> dict[uuid.UUID, Row[uuid.UUID, Decimal, dt.date]]:
    """The latest balance snapshot per account, fetched with one `DISTINCT ON` query.

    One query for all of `account_ids`, not a query per account, so the cost doesn't
    grow with the number of accounts. `on_or_before`, when given, bounds the latest
    snapshot picked to one on or before that date.
    """
    if not account_ids:
        return {}
    stmt = (
        select(BalanceSnapshot.account_id, BalanceSnapshot.balance, BalanceSnapshot.date)
        .ext(distinct_on(BalanceSnapshot.account_id))
        .where(BalanceSnapshot.account_id.in_(account_ids), BalanceSnapshot.user_id == user_id)
        .order_by(BalanceSnapshot.account_id, BalanceSnapshot.date.desc())
    )
    if on_or_before is not None:
        stmt = stmt.where(BalanceSnapshot.date <= on_or_before)
    return {row.account_id: row for row in db.execute(stmt)}


def list_cards(
    db: Session, user_id: uuid.UUID, include_inactive: bool, today: dt.date | None = None
) -> list[CardOut]:
    """The Cards screen: every `credit_card` account with its latest balance and
    interest summary (`get_card_interest`).
    """
    stmt = owned(Account, user_id).where(Account.kind == AccountKind.credit_card)
    if not include_inactive:
        stmt = stmt.where(Account.active.is_(True))
    cards = list(db.scalars(stmt.order_by(Account.name)))
    if not cards:
        return []

    account_ids = [card.id for card in cards]
    latest_by_account = latest_balances(db, user_id, account_ids)
    interest_by_account = _interest_by_card(
        db, user_id, cards, latest_by_account, today or _local_today()
    )

    out = []
    for card in cards:
        latest = latest_by_account.get(card.id)
        out.append(
            CardOut(
                account=AccountOut.model_validate(card),
                balance=latest.balance if latest else None,
                balance_date=latest.date if latest else None,
                source_hint="synced" if card.is_synced else "manual",
                interest=interest_by_account[card.id],
            )
        )
    return out


_INTEREST_MONTHS = 12
_MISMATCH_RATIO = Decimal("0.25")
_MISMATCH_FLOOR = Decimal("5.00")
_CENT = Decimal("0.01")
#: A balance snapshot older than this, before the month it's used for, is too stale to
#: say what the card owed.
_STALE_BALANCE = dt.timedelta(days=45)


def _local_today() -> dt.date:
    return dt.datetime.now(APP_TIMEZONE).date()


def _month_start(year: int, month: int) -> dt.date:
    """The first day of a month, `month` may fall outside 1..12 and rolls the year."""
    year += (month - 1) // 12
    return dt.date(year, (month - 1) % 12 + 1, 1)


def _monthly_interest(owed: Decimal, apr: Decimal) -> Decimal:
    return (max(owed, _ZERO) * apr / 100 / 12).quantize(_CENT)


def _promo_active(card: Account, on: dt.date) -> bool:
    return card.promo_apr_end is not None and card.promo_apr_end > on


def _interest_by_card(
    db: Session,
    user_id: uuid.UUID,
    cards: list[Account],
    latest_by_account: dict[uuid.UUID, Row[uuid.UUID, Decimal, dt.date]],
    today: dt.date,
) -> dict[uuid.UUID, CardInterestOut]:
    """Interest and fees per card for the 12 months ending with `today`'s month.

    One query for every posted, non-split-parent row in the window, classified
    with `is_interest_or_fee` like the card log and split into interest
    (`is_interest`) and fees; amounts are reported as positive costs. Reversals
    (positive rows) net against charges of the same kind in the same month, never
    below zero. The estimate and the mismatch check are described on `CardInterestOut`;
    the mismatch estimate uses the balance on or before the end of the month before
    the last full month, what the card owed going into it.
    """
    window_start = _month_start(today.year, today.month - (_INTEREST_MONTHS - 1))
    this_month = _month_start(today.year, today.month)
    last_full = _month_start(today.year, today.month - 1)
    months = [
        _month_start(window_start.year, window_start.month + i) for i in range(_INTEREST_MONTHS)
    ]

    interest_net: dict[tuple[uuid.UUID, dt.date], Decimal] = {}
    fees_net: dict[tuple[uuid.UUID, dt.date], Decimal] = {}
    stmt = select(
        Transaction.account_id,
        Transaction.posted_date,
        Transaction.amount,
        Transaction.description_raw,
    ).where(
        Transaction.user_id == user_id,
        Transaction.account_id.in_([card.id for card in cards]),
        Transaction.posted_date >= window_start,
        Transaction.posted_date < _month_start(today.year, today.month + 1),
        Transaction.pending.is_(False),
        Transaction.amount != 0,
        ~has_split_children(),
    )
    for account_id, posted, amount, description in db.execute(stmt):
        if not is_interest_or_fee(description):
            continue
        # Positive rows are reversals; they net against charges of the same kind.
        net = interest_net if is_interest(description) else fees_net
        key = (account_id, _month_start(posted.year, posted.month))
        net[key] = net.get(key, _ZERO) - amount

    prior_balances = latest_balances(
        db,
        user_id,
        [card.id for card in cards],
        on_or_before=_month_start(last_full.year, last_full.month) - dt.timedelta(days=1),
    )

    out = {}
    for card in cards:
        interest_by_month = [max(interest_net.get((card.id, m), _ZERO), _ZERO) for m in months]
        fees_by_month = [max(fees_net.get((card.id, m), _ZERO), _ZERO) for m in months]
        latest = latest_by_account.get(card.id)

        estimate = None
        if _promo_active(card, today):
            estimate = _ZERO
        elif card.apr is not None and latest is not None and latest.date >= today - _STALE_BALANCE:
            estimate = _monthly_interest(-latest.balance, card.apr)

        mismatch = False
        prior = prior_balances.get(card.id)
        last_full_end = this_month - dt.timedelta(days=1)
        if _promo_active(card, last_full_end):
            expected = _ZERO
        elif (
            card.apr is not None and prior is not None and prior.date >= last_full - _STALE_BALANCE
        ):
            expected = _monthly_interest(-prior.balance, card.apr)
        else:
            expected = None
        if expected is not None:
            gap = abs(interest_by_month[-2] - expected)
            mismatch = gap > _MISMATCH_FLOOR and gap > expected * _MISMATCH_RATIO

        this_year = [m.year == today.year for m in months]
        ytd_interest = sum(
            (a for a, y in zip(interest_by_month, this_year, strict=True) if y), _ZERO
        )
        ytd_fees = sum((a for a, y in zip(fees_by_month, this_year, strict=True) if y), _ZERO)
        out[card.id] = CardInterestOut(
            months=[
                InterestMonthOut(
                    month=f"{m.year:04d}-{m.month:02d}", amount=i + f, interest=i, fees=f
                )
                for m, i, f in zip(months, interest_by_month, fees_by_month, strict=True)
            ],
            year_to_date=ytd_interest + ytd_fees,
            year_to_date_interest=ytd_interest,
            year_to_date_fees=ytd_fees,
            estimate=estimate,
            mismatch=mismatch,
        )
    return out


def get_interest_summary(
    db: Session, user_id: uuid.UUID, today: dt.date | None = None
) -> InterestSummaryOut:
    """Interest and fees across every card, closed ones too: the year to date and each card's
    breakdown, keyed by account id.
    """
    cards = list_cards(db, user_id, include_inactive=True, today=today)
    interest = {card.account.id: card.interest for card in cards if card.interest is not None}
    return InterestSummaryOut(
        year_to_date=sum((i.year_to_date for i in interest.values()), _ZERO), cards=interest
    )


def card_debts(db: Session, user_id: uuid.UUID) -> list[Debt]:
    """Every active card that owes on its latest balance, as a payoff `Debt`."""
    cards = list(
        db.scalars(
            owned(Account, user_id)
            .where(Account.kind == AccountKind.credit_card, Account.active.is_(True))
            .order_by(Account.name, Account.id)
        )
    )
    latest_by_account = latest_balances(db, user_id, [card.id for card in cards])
    debts = []
    for card in cards:
        latest = latest_by_account.get(card.id)
        if latest is None or latest.balance >= 0:
            continue
        debts.append(
            Debt(
                id=card.id,
                name=card.name,
                owed=-latest.balance,
                apr=card.apr,
                promo_apr_end=card.promo_apr_end,
                planned_payment=card.planned_payment,
                due_day=card.due_day,
            )
        )
    return debts


def list_loans(db: Session, user_id: uuid.UUID, now: dt.datetime | None = None) -> list[LoanOut]:
    """Every active `loan` account with what it owes and its payoff on its own."""
    today = (now or dt.datetime.now(dt.UTC)).astimezone(APP_TIMEZONE).date()
    loans = list(
        db.scalars(
            owned(Account, user_id)
            .where(Account.kind == AccountKind.loan, Account.active.is_(True))
            .order_by(Account.name, Account.id)
        )
    )
    latest_by_account = latest_balances(db, user_id, [loan.id for loan in loans])
    out = []
    for loan in loans:
        latest = latest_by_account.get(loan.id)
        owed = abs(latest.balance) if latest is not None and latest.balance != 0 else None
        paid = None
        if owed is not None:
            debt = Debt(
                id=loan.id,
                name=loan.name,
                owed=owed,
                apr=loan.apr,
                promo_apr_end=loan.promo_apr_end,
                planned_payment=loan.planned_payment,
                due_day=loan.due_day,
            )
            paid = plan([debt], today).cards[0]
        out.append(
            LoanOut(
                account=AccountOut.model_validate(loan),
                owed=owed,
                balance_date=latest.date if latest else None,
                months_to_payoff=paid.months_to_payoff if paid else None,
                payoff_month=paid.payoff_month if paid else None,
                total_interest=paid.total_interest if paid else None,
            )
        )
    return out


def get_payoff_plan(
    db: Session, user_id: uuid.UUID, budget: Decimal | None, now: dt.datetime | None = None
) -> PayoffPlanOut:
    """The payoff plan (`app.services.payoff.plan`) for the user's active cards that owe,
    as of today in `APP_TIMEZONE`."""
    today = (now or dt.datetime.now(dt.UTC)).astimezone(APP_TIMEZONE).date()
    return plan(card_debts(db, user_id), today, budget)


def _validate_balance_date(value: dt.date, now: dt.datetime) -> None:
    if value < _MIN_BALANCE_DATE or value > now.date():
        raise UnprocessableEntity.value_error(
            f"date must be between {_MIN_BALANCE_DATE.isoformat()} and today"
        )


def upsert_balance(
    db: Session,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    date: dt.date,
    balance: Decimal,
    now: dt.datetime,
) -> BalanceSnapshot:
    """Set a manual month-end balance for a card the Bridge can't connect.

    Only `credit_card` accounts take a manual balance (`UnprocessableEntity`), and
    only an unsynced one: a `simplefin_account_id` account is refused with
    `Conflict` so a manual entry can never fight the nightly sync, which owns that
    account's snapshots. Upserts on `(account_id, date)`, same key the sync writes.
    """
    account = get_owned(db, Account, account_id, user_id)
    if account.kind != AccountKind.credit_card:
        raise UnprocessableEntity("Only credit card accounts have a manual balance.")
    if account.is_synced:
        raise Conflict("This account is synced; its balances come from the nightly sync.")
    _validate_balance_date(date, now)

    stmt = (
        insert(BalanceSnapshot)
        .values(id=uuid.uuid4(), user_id=user_id, account_id=account_id, date=date, balance=balance)
        .on_conflict_do_update(
            index_elements=["account_id", "date"], set_={"balance": balance, "updated_at": now}
        )
        .returning(BalanceSnapshot)
    )
    return db.scalars(stmt).one()


def get_balance_history(
    db: Session,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    from_: dt.date | None,
    to: dt.date | None,
) -> list[BalanceSnapshot]:
    """Snapshot history for one account, newest first, capped at `_MAX_HISTORY_ROWS`."""
    get_owned(db, Account, account_id, user_id)

    stmt = (
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.user_id == user_id)
        .order_by(BalanceSnapshot.date.desc())
        .limit(_MAX_HISTORY_ROWS)
    )
    if from_ is not None:
        stmt = stmt.where(BalanceSnapshot.date >= from_)
    if to is not None:
        stmt = stmt.where(BalanceSnapshot.date <= to)
    return list(db.scalars(stmt))


_ZERO = Decimal("0.00")

# No category or column marks interest and fees (spec "Credit cards" gives no
# column for them), so this matches a bank's own wording on the posted
# description, the same `description_raw` the sync writes (#48), normalized the
# same way the sync fingerprint and the rules matcher do (#51). Each phrase is
# wrapped in its own `\b...\b` so a substring inside an unrelated word --
# "COFFEE", "FEED", "TOFFEE" -- can't trigger it (#63 review UB-1/MV-1). `FEES?`
# alone already matches "LATE FEE"/"ANNUAL FEE"/etc., so those aren't spelled out
# as separate phrases (#63 review UB-6).
_INTEREST_FEE_PHRASES = (
    r"INTEREST(?:\s+CHARGED?)?",
    r"FINANCE CHARGE",
    r"FEES?",
)
_INTEREST_FEE_PATTERN = re.compile("|".join(rf"\b{phrase}\b" for phrase in _INTEREST_FEE_PHRASES))
_INTEREST_PATTERN = re.compile("|".join(rf"\b{phrase}\b" for phrase in _INTEREST_FEE_PHRASES[:2]))


def is_interest(description: str) -> bool:
    """Whether `description` is an interest or finance charge, as opposed to a fee."""
    return _INTEREST_PATTERN.search(normalize_text(description)) is not None


def is_interest_or_fee(description: str) -> bool:
    """Whether `description` looks like a card's own interest charge or fee, not a
    purchase. Flagged for review: this is a heuristic, not a bank-provided flag, and
    a statement's actual wording may need a phrase added to `_INTEREST_FEE_PHRASES`.
    """
    normalized = normalize_text(description)
    return _INTEREST_FEE_PATTERN.search(normalized) is not None


@dataclass(frozen=True)
class CardLogTotals:
    """One month's posted, non-split-parent transactions on a card, classified
    (spec "Credit cards"). Every amount keeps SimpleFIN's sign.
    """

    new_charges: Decimal
    payments: Decimal
    interest_and_fees: Decimal
    other: Decimal

    @property
    def total(self) -> Decimal:
        return self.new_charges + self.payments + self.interest_and_fees + self.other


def _classify(rows: Iterable[tuple[Decimal, Bucket, str]]) -> CardLogTotals:
    """Sort `rows` (amount, bucket, description) into `CardLogTotals`. Pure and
    DB-free (#63 review UB-2), so classification rules (and #61's transfer
    matcher) can be unit-tested without a session:
    - `payments`: positive amounts with `bucket=transfer`. The transfer matcher
      (#61) is meant to set that bucket; until it exists, a payment only lands
      here if the user sets the bucket themselves.
    - `interest_and_fees`: negative amounts whose description matches
      `is_interest_or_fee`.
    - `new_charges`: the other negative amounts.
    - `other`: the remaining positive, non-transfer amounts (refunds and credits).
    """
    new_charges = payments = interest_and_fees = other = _ZERO
    for amount, bucket, description in rows:
        if amount > 0:
            if bucket == Bucket.transfer:
                payments += amount
            else:
                other += amount
        elif is_interest_or_fee(description):
            interest_and_fees += amount
        else:
            new_charges += amount
    return CardLogTotals(new_charges, payments, interest_and_fees, other)


def _month_end_balances(
    db: Session, user_id: uuid.UUID, account_id: uuid.UUID, start: dt.date, end: dt.date
) -> tuple[Row[uuid.UUID, Decimal, dt.date] | None, Row[uuid.UUID, Decimal, dt.date] | None]:
    """The latest balance snapshot on or before the last day of the previous month
    and of `start`'s month (`[start, end)`), `None` each when no snapshot qualifies.
    """
    previous_month_end = start - dt.timedelta(days=1)
    this_month_end = end - dt.timedelta(days=1)
    starting = latest_balances(db, user_id, [account_id], on_or_before=previous_month_end)
    ending = latest_balances(db, user_id, [account_id], on_or_before=this_month_end)
    return starting.get(account_id), ending.get(account_id)


def get_card_log(db: Session, user_id: uuid.UUID, account_id: uuid.UUID, month: str) -> CardLogOut:
    """The monthly card log (spec "Credit cards"): starting balance, new charges,
    payments, interest and fees, ending balance, replacing the sheet's Cards tab log.

    `starting_balance`/`ending_balance` are the latest snapshot on or before the
    last day of the previous/this month (`_month_end_balances`); either is `None`
    when no snapshot qualifies. `computed_ending = starting_balance + sum(every
    amount in the month)` (`_classify`), set whenever `starting_balance` is known.

    `difference = ending_balance - computed_ending`, but only when the ending
    snapshot is actually dated inside the requested month and the starting one on
    or before the previous month-end (#63 review UB-5/MV-2): `latest_balances`
    reuses an older snapshot when nothing newer qualifies, and a reused ending
    snapshot's gap from `computed_ending` is a stale baseline, not a real
    reconciliation gap. `difference` stays `None` in that case; the snapshot dates
    are still reported so the client can tell a stale baseline from a fresh one.
    """
    account = get_owned(db, Account, account_id, user_id)
    if account.kind != AccountKind.credit_card:
        raise UnprocessableEntity("Only credit card accounts have a monthly card log.")

    start, end = month_range(month)
    starting, ending = _month_end_balances(db, user_id, account_id, start, end)
    starting_balance = starting.balance if starting is not None else None
    ending_balance = ending.balance if ending is not None else None

    stmt = select(Transaction.amount, Transaction.bucket, Transaction.description_raw).where(
        Transaction.user_id == user_id,
        Transaction.account_id == account_id,
        Transaction.posted_date >= start,
        Transaction.posted_date < end,
        Transaction.pending.is_(False),
        ~has_split_children(),
    )
    totals = _classify(db.execute(stmt))

    computed_ending = None
    difference = None
    if starting is not None:
        computed_ending = starting.balance + totals.total
        previous_month_end = start - dt.timedelta(days=1)
        starting_predates_month = starting.date <= previous_month_end
        if ending is not None and starting_predates_month and start <= ending.date < end:
            difference = ending.balance - computed_ending

    return CardLogOut(
        month=month,
        starting_balance=starting_balance,
        starting_balance_date=starting.date if starting is not None else None,
        new_charges=totals.new_charges,
        payments=totals.payments,
        interest_and_fees=totals.interest_and_fees,
        other=totals.other,
        ending_balance=ending_balance,
        ending_balance_date=ending.date if ending is not None else None,
        computed_ending=computed_ending,
        difference=difference,
    )
