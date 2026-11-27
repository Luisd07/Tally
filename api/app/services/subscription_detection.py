"""Subscription detection (#78), computed on read: suggest a new subscription from
three similar monthly charges, and flag an existing one whose price changed, whose
charge didn't arrive, or that has gone quiet.

Matching's `match_text` is reused for "which subscription already covers this
merchant", so a later dedicated bank-descriptor field changes in one place.
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.models import Subscription, Transaction
from app.models.enums import Cadence
from app.schemas.subscriptions import PriceChange, SubscriptionFlags, SubscriptionSuggestion
from app.scoping import owned
from app.services.normalize import normalize_text
from app.services.subscription_matching import (
    match_text,
    shift_due,
    unlinked_outflow_clauses,
    word_boundary_pattern,
)

_MIN_CHARGES = 3
_MIN_GAP_DAYS = 28
_MAX_GAP_DAYS = 31
# A charge joins an amount cluster when within this fraction of the cluster's median.
_AMOUNT_TOLERANCE = Decimal("0.10")
# Charges older than this can't be part of the current run.
_LOOKBACK_DAYS = 400
_NAME_MAX = 100
# Volatile parts of a bank descriptor: `#` refs, dates (10/05, 2026-10-05), digit runs.
_PER_CHARGE_TOKENS = re.compile(r"#\w+|\d+(?:[/-]\d+)*")

# A subscription's amount differs from its latest charge by more than this fraction.
_PRICE_CHANGE_TOLERANCE = Decimal("0.01")
# A due date this many days past with no charge since counts as missed.
_MISSED_GRACE_DAYS = 7
_DORMANT_CYCLES = 2


def _group_key(txn: Transaction) -> str:
    """The merchant (or raw description) without per-charge tokens, so
    "NETFLIX.COM 12345" and "NETFLIX.COM 67890" group together.
    """
    text = normalize_text(txn.merchant or txn.description_raw)
    return normalize_text(_PER_CHARGE_TOKENS.sub(" ", text))


def _amount_clusters(charges: Sequence[Transaction]) -> list[list[Transaction]]:
    """`charges` split into clusters of similar amounts: walking by ascending amount, a
    charge joins the current cluster when within `_AMOUNT_TOLERANCE` of its median.
    Each cluster is date-ordered.
    """
    clusters: list[list[Transaction]] = []
    for txn in sorted(charges, key=lambda t: abs(t.amount)):
        if clusters:
            mid = median(abs(t.amount) for t in clusters[-1])
            if abs(abs(txn.amount) - mid) <= mid * _AMOUNT_TOLERANCE:
                clusters[-1].append(txn)
                continue
        clusters.append([txn])
    return [sorted(cluster, key=lambda t: (t.posted_date, t.id)) for cluster in clusters]


def _recent_run(charges: Sequence[Transaction]) -> list[Transaction]:
    """The newest run of `charges` (date-ordered) with consecutive gaps of 28-31 days;
    empty when it has fewer than `_MIN_CHARGES`.
    """
    run = [charges[-1]]
    for txn in reversed(charges[:-1]):
        if not _MIN_GAP_DAYS <= (run[0].posted_date - txn.posted_date).days <= _MAX_GAP_DAYS:
            break
        run.insert(0, txn)
    return run if len(run) >= _MIN_CHARGES else []


def _unlinked_outflows(db: Session, user_id: uuid.UUID, since: date) -> list[Transaction]:
    return list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.posted_date >= since,
                *unlinked_outflow_clauses(),
            )
            .order_by(Transaction.posted_date, Transaction.id)
        )
    )


def _covering_patterns(db: Session, user_id: uuid.UUID) -> list[re.Pattern[str]]:
    """Word-boundary patterns of every active subscription's match text."""
    return [
        word_boundary_pattern(text)
        for sub in db.scalars(owned(Subscription, user_id).where(Subscription.active.is_(True)))
        if (text := match_text(sub))
    ]


def _suggestion_from(run: Sequence[Transaction]) -> SubscriptionSuggestion:
    latest = run[-1]
    return SubscriptionSuggestion(
        name=(latest.merchant or latest.description_raw).strip()[:_NAME_MAX],
        amount=latest.amount,
        cadence=Cadence.monthly,
        next_due=shift_due(latest.posted_date, Cadence.monthly, 1),
        charge_count=len(run),
        transaction_ids=[txn.id for txn in run],
    )


def suggest_subscriptions(
    db: Session, user_id: uuid.UUID, now: datetime
) -> list[SubscriptionSuggestion]:
    """Merchants with a recent monthly run of similar posted outflows, none linked to
    a subscription, that no active subscription covers yet (its match text, on word
    boundaries, in the merchant or latest description). A run whose next charge was
    due over a week ago has stopped and isn't suggested. Soonest `next_due` first.
    """
    by_merchant: dict[str, list[Transaction]] = defaultdict(list)
    for txn in _unlinked_outflows(db, user_id, now.date() - timedelta(days=_LOOKBACK_DAYS)):
        if key := _group_key(txn):
            by_merchant[key].append(txn)

    covering = _covering_patterns(db, user_id)
    stale_before = now.date() - timedelta(days=_MISSED_GRACE_DAYS)
    suggestions = []
    for key, group in by_merchant.items():
        for cluster in _amount_clusters(group):
            run = _recent_run(cluster)
            if not run:
                continue
            suggestion = _suggestion_from(run)
            text = normalize_text(f"{key} {run[-1].description_raw}")
            if suggestion.next_due >= stale_before and not any(p.search(text) for p in covering):
                suggestions.append(suggestion)
    return sorted(suggestions, key=lambda s: (s.next_due, s.name))


def _latest_charges(
    db: Session, user_id: uuid.UUID, subscriptions: Sequence[Subscription]
) -> dict[uuid.UUID, tuple[date, Decimal]]:
    """Each subscription's latest posted linked charge `(date, amount)`, in one query."""
    if not subscriptions:
        return {}
    rows = db.execute(
        select(Transaction.subscription_id, Transaction.posted_date, Transaction.amount)
        .where(
            Transaction.user_id == user_id,
            Transaction.subscription_id.in_([sub.id for sub in subscriptions]),
            Transaction.pending.is_(False),
        )
        .ext(distinct_on(Transaction.subscription_id))
        .order_by(Transaction.subscription_id, Transaction.posted_date.desc(), Transaction.id)
    )
    return {sub_id: (posted, amount) for sub_id, posted, amount in rows if sub_id}


def _flags_for(
    sub: Subscription, charge: tuple[date, Decimal] | None, today: date
) -> SubscriptionFlags:
    if not sub.active:
        return SubscriptionFlags()
    last_date = charge[0] if charge else None
    price_change = None
    if charge and abs(charge[1] - sub.amount) > abs(sub.amount) * _PRICE_CHANGE_TOLERANCE:
        price_change = PriceChange(old=sub.amount, new=charge[1])
    missed = (
        sub.next_due is not None
        and sub.next_due < today - timedelta(days=_MISSED_GRACE_DAYS)
        and (last_date is None or last_date < sub.next_due)
    )
    quiet_since = shift_due(today, sub.cadence, -_DORMANT_CYCLES)
    dormant = (last_date or sub.created_at.date()) < quiet_since
    return SubscriptionFlags(price_change=price_change, missed=missed, dormant=dormant)


def subscription_flags(
    db: Session, user_id: uuid.UUID, subscriptions: Sequence[Subscription], now: datetime
) -> dict[uuid.UUID, SubscriptionFlags]:
    """Flags for each of `subscriptions`, from one query for every one's latest linked
    charge. An inactive subscription is never flagged. `dormant`: no linked charge
    within two cycles of today (a subscription never charged counts from its
    creation). `missed`: `next_due` over a week past with no linked charge since.
    `price_change`: the latest charge differs from the stored amount by over 1%.
    """
    latest = _latest_charges(db, user_id, subscriptions)
    today = now.date()
    return {sub.id: _flags_for(sub, latest.get(sub.id), today) for sub in subscriptions}
