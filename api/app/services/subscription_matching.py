"""Sync-time subscription matching (#56): link a new charge to the active
subscription it pays and advance that subscription's `next_due`.

Kept apart from `app.services.subscriptions` (the CRUD and totals the API serves)
because it changes for different reasons; `app.services.sync` calls it as a
post-process step.
"""

import calendar
import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from loguru import logger
from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from app.errors import Conflict, NotFound, UnprocessableEntity
from app.models import Subscription, Transaction
from app.models.enums import Bucket, Cadence
from app.services import rules
from app.services.matching import has_pending_twin
from app.services.normalize import normalize_text
from app.services.splits import has_split_children
from app.services.transactions import split_into

# A charge matches a subscription when its amount is within this fraction of the
# subscription's: price bumps and FX drift, not a different product.
_AMOUNT_TOLERANCE = Decimal("0.10")

_CENT = Decimal("0.01")
_FULL_SHARE = 100

_MONTHS_PER_CADENCE = {Cadence.monthly: 1, Cadence.quarterly: 3, Cadence.yearly: 12}
_DAYS_PER_CADENCE = {
    Cadence.weekly: 7,
    Cadence.monthly: 30,
    Cadence.quarterly: 91,
    Cadence.yearly: 365,
}
# How far from a due date a charge may land and still pay that cycle (an early
# charge, a weekend delay). Half a weekly cycle, otherwise a week.
_WINDOW_DAYS = {
    Cadence.weekly: 3,
    Cadence.monthly: 7,
    Cadence.quarterly: 7,
    Cadence.yearly: 7,
}


def shift_due(due: date, cadence: Cadence, periods: int) -> date:
    """`due` moved by `periods` cadences (negative goes back); a month-based step
    clamps a day past the target month's end (Jan 31 monthly -> Feb 28/29) to its
    last day.
    """
    if cadence == Cadence.weekly:
        return due + timedelta(weeks=periods)
    month_index = due.year * 12 + due.month - 1 + _MONTHS_PER_CADENCE[cadence] * periods
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(due.day, calendar.monthrange(year, month)[1]))


def advance_next_due(due: date, cadence: Cadence) -> date:
    """`due` one cadence later, month ends clamped."""
    return shift_due(due, cadence, 1)


def match_text(sub: Subscription) -> str:
    """What a charge's merchant or description must contain to fit `sub`: its
    normalized `match_text` (#139) when set, else its normalized name.
    """
    return normalize_text(sub.match_text or "") or normalize_text(sub.name)


def word_boundary_pattern(text: str) -> re.Pattern[str]:
    """`text` as a whole word or phrase: not preceded or followed by a word character."""
    return re.compile(rf"(?<!\w){re.escape(text)}(?!\w)")


def unlinked_outflow_clauses() -> list[ColumnElement[bool]]:
    """`where` clauses for a standalone outflow no subscription has claimed: negative,
    posted, not a split parent or child, not linked, not a transfer.
    """
    return [
        Transaction.amount < 0,
        Transaction.pending.is_(False),
        Transaction.parent_id.is_(None),
        ~has_split_children(),
        Transaction.subscription_id.is_(None),
        Transaction.transfer_pair_id.is_(None),
        Transaction.bucket != Bucket.transfer,
    ]


def _near_a_due_date(sub: Subscription, day: date) -> bool:
    """Whether `day` is within the cadence's window of one of `sub`'s due dates,
    counted forward and back from `next_due`. Always true without a `next_due`.
    """
    if sub.next_due is None:
        return True
    window = _WINDOW_DAYS[sub.cadence]
    nearest = round((day - sub.next_due).days / _DAYS_PER_CADENCE[sub.cadence])
    return any(
        abs((shift_due(sub.next_due, sub.cadence, cycle) - day).days) <= window
        for cycle in (nearest - 1, nearest, nearest + 1)
    )


def _eligible_charges(
    db: Session, user_id: uuid.UUID, row_ids: Sequence[uuid.UUID]
) -> list[Transaction]:
    """Posted outflows among `row_ids`, oldest first: not a split parent or child,
    a transfer, or a row already linked. A charge whose pending original may still
    be listed is deferred: linking it now would make the pending reconcile (#50)
    see it as user-touched, and its absorb carries the link over later anyway.
    """
    charges = db.scalars(
        select(Transaction)
        .where(
            Transaction.id.in_(row_ids),
            Transaction.user_id == user_id,
            *unlinked_outflow_clauses(),
        )
        .order_by(Transaction.posted_date, Transaction.id)
    ).all()
    eligible = []
    for txn in charges:
        if has_pending_twin(db, txn):
            logger.info(
                "Deferring subscription match for {}: a pending twin is still listed", txn.id
            )
        else:
            eligible.append(txn)
    return eligible


def _candidate_pairs(
    charges: Sequence[Transaction], subscriptions: Sequence[Subscription]
) -> tuple[dict[uuid.UUID, list[Subscription]], dict[uuid.UUID, list[Transaction]]]:
    """Every `(charge, subscription)` fit, indexed by charge and by subscription: the
    subscription's match text on word boundaries in the normalized merchant and
    description, the amount within `_AMOUNT_TOLERANCE`, and the date near one of
    its due dates. A charge fitting two subscriptions is logged here.
    """
    patterns = {
        sub.id: word_boundary_pattern(text) for sub in subscriptions if (text := match_text(sub))
    }
    by_charge: dict[uuid.UUID, list[Subscription]] = defaultdict(list)
    by_subscription: dict[uuid.UUID, list[Transaction]] = defaultdict(list)
    for txn in charges:
        text = normalize_text(f"{txn.merchant or ''} {txn.description_raw}")
        for sub in subscriptions:
            pattern = patterns.get(sub.id)
            gap = abs(txn.amount - sub.amount)
            if (
                pattern
                and pattern.search(text)
                and gap <= abs(sub.amount) * _AMOUNT_TOLERANCE
                and _near_a_due_date(sub, txn.posted_date)
            ):
                by_charge[txn.id].append(sub)
                by_subscription[sub.id].append(txn)
        if len(by_charge[txn.id]) > 1:
            logger.warning(
                "Skipping subscription match for {}: fits {} subscriptions",
                txn.id,
                len(by_charge[txn.id]),
            )
    return by_charge, by_subscription


def _standalone_charges(sub: Subscription, charges: Sequence[Transaction]) -> list[Transaction]:
    """The date-ordered `charges` that are alone in their billing cycle. Charges
    chained closer than half a cadence apart share a cycle: which of them pays it
    can't be told, so none is returned (logged).
    """
    cycles: list[list[Transaction]] = []
    cycle_days = _DAYS_PER_CADENCE[sub.cadence]
    for txn in charges:
        previous = cycles[-1][-1] if cycles else None
        if previous and (txn.posted_date - previous.posted_date).days * 2 < cycle_days:
            cycles[-1].append(txn)
        else:
            cycles.append([txn])
    for cycle in cycles:
        if len(cycle) > 1:
            logger.warning(
                "Skipping subscription match for {}: subscription {} fits {} charges in one cycle",
                [txn.id for txn in cycle],
                sub.id,
                len(cycle),
            )
    return [cycle[0] for cycle in cycles if len(cycle) == 1]


def _link(sub: Subscription, txn: Transaction) -> None:
    """Link `txn` to `sub` and move `next_due` forward, never back, to the first due
    date after the charge (and its early window): a stale `next_due` catches up
    across missed cycles, and an older backfilled charge leaves it alone.
    """
    txn.subscription_id = sub.id
    due = sub.next_due or txn.posted_date
    while due <= txn.posted_date + timedelta(days=_WINDOW_DAYS[sub.cadence]):
        due = advance_next_due(due, sub.cadence)
    if sub.next_due is None or due > sub.next_due:
        sub.next_due = due


def _split_amounts(amount: Decimal, pct: int) -> tuple[Decimal, Decimal] | None:
    """The (business, personal) parts of `amount` at `pct` percent business, the
    business part rounded half-even to the cent and the personal part the remainder
    so they always sum to `amount`. None when either part is zero.
    """
    business = (amount * pct / _FULL_SHARE).quantize(_CENT, rounding=ROUND_HALF_EVEN)
    personal = amount - business
    if not business or not personal:
        return None
    return business, personal


def _apply_business_share(
    db: Session,
    user_id: uuid.UUID,
    sub: Subscription,
    txn: Transaction,
    index: rules.RuleIndex,
) -> None:
    """Apply `sub.business_pct` (#129) to the freshly linked `txn`: a charge the
    user hasn't classified is split into a business and a personal child through
    the split service (100 just marks it business). A charge whose classification
    was set by hand, or whose split a guard refuses (a filed expense report), is
    left as linked and logged. Like the 100% path, a split of an unreviewed charge
    leaves its children unreviewed so they reach the Inbox for a category; the
    parent stays reviewed, as an unreviewed split parent reads as needs_resplit.
    """
    pct = sub.business_pct
    if not pct or rules.values_set_by_hand(txn, index):
        return
    if pct == _FULL_SHARE:
        txn.bucket = Bucket.business
        return
    parts = _split_amounts(txn.amount, pct)
    if parts is None:
        logger.info("Not splitting {}: a {}% share rounds to a zero part", txn.id, pct)
        return
    was_reviewed = txn.reviewed
    business, personal = parts
    try:
        with db.begin_nested():
            _, children = split_into(
                db, user_id, txn.id, [(business, Bucket.business), (personal, Bucket.personal)]
            )
            if not was_reviewed:
                for child in children:
                    child.reviewed = False
    except (Conflict, NotFound, UnprocessableEntity) as exc:
        logger.warning("Linked {} to {} but left it unsplit: {}", txn.id, sub.id, exc)


def match_subscriptions(db: Session, user_id: uuid.UUID, row_ids: Sequence[uuid.UUID]) -> int:
    """Link each of `row_ids` (the rows this run created or posted, as they exist
    now) to the one active subscription its merchant, amount and date fit, and
    advance that subscription's `next_due` past the charge. Returns the number
    linked.

    Ambiguity is never guessed at and judged per billing cycle: a charge fitting
    two subscriptions, or two charges of one subscription in the same cycle, are
    logged and left unlinked, while a backfill of one charge per cycle links all. A
    linked charge then takes the subscription's business share (`business_pct`).
    """
    charges = _eligible_charges(db, user_id, row_ids) if row_ids else []
    if not charges:
        return 0
    subscriptions = db.scalars(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.active.is_(True))
        .with_for_update()
    ).all()
    by_charge, by_subscription = _candidate_pairs(charges, subscriptions)
    index = rules.load_rule_index(db, user_id)
    linked = 0
    for sub in subscriptions:
        for txn in _standalone_charges(sub, by_subscription.get(sub.id, [])):
            if len(by_charge[txn.id]) == 1:
                _link(sub, txn)
                _apply_business_share(db, user_id, sub, txn, index)
                linked += 1
    db.flush()
    return linked
