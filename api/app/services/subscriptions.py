"""Subscription and fixed-bill CRUD, plus the monthly/annual totals shown on the
Subscriptions screen.

See `.claude/rules/api.md` for user scoping: every client-supplied id (`account_id`,
`category_id`) is loaded through `get_owned` because the DB doesn't enforce same-user FKs.
Create and patch go through `app.services.crud`, the pattern shared with accounts/categories.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy.orm import Session

from app.models import Account, Category, Subscription
from app.models.enums import Cadence, SubscriptionKind
from app.schemas.subscriptions import (
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionPage,
    SubscriptionTotals,
    SubscriptionUpdate,
)
from app.scoping import get_owned, owned
from app.services.crud import apply_patch, check_date_order, check_owned_refs, create_idempotent
from app.services.subscription_detection import subscription_flags

_CENT = Decimal("0.01")

# Occurrences per year, for normalizing every cadence to a monthly and an annual total.
# A `Cadence` member missing here is a bug: it raises `KeyError` rather than silently
# costing a new cadence as monthly, which `set(_PERIODS_PER_YEAR) == set(Cadence)` pins.
_PERIODS_PER_YEAR: dict[Cadence, int] = {
    Cadence.weekly: 52,
    Cadence.monthly: 12,
    Cadence.quarterly: 4,
    Cadence.yearly: 1,
}


def create_subscription(
    db: Session, user_id: uuid.UUID, body: SubscriptionCreate
) -> tuple[Subscription, bool]:
    """Idempotent create: a repeated `id` returns the existing row, ignoring the new body.

    Raises `NotFound` if `account_id`/`category_id` aren't the caller's, `Conflict` if `id`
    already belongs to another user's row, and `UnprocessableEntity` if `end_date` is
    before `start_date`.
    """
    check_owned_refs(db, user_id, body, {"account_id": Account, "category_id": Category})
    check_date_order(body, body)

    return create_idempotent(db, Subscription, user_id, body.model_dump())


def update_subscription(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, body: SubscriptionUpdate
) -> Subscription:
    """Checks `end_date >= start_date` against the merged row: whichever of the two the
    patch sets, falling back to what's already stored for the one it doesn't.
    """
    row = get_owned(db, Subscription, id, user_id)
    check_owned_refs(db, user_id, body, {"account_id": Account, "category_id": Category})
    check_date_order(row, body)

    return apply_patch(db, row, body)


def _normalize_zero(value: Decimal) -> Decimal:
    """`-0.00 + 0` is `0.00`: adding a positive zero to a negative one flips its sign,
    same as any other Decimal addition of differently-signed zeros.
    """
    return value + 0


def _totals_by_kind(rows: Iterable[Subscription]) -> dict[SubscriptionKind, SubscriptionTotals]:
    """Monthly and annual totals per `kind`, so a car payment (`bill`) never inflates the
    subscriptions total. Each row's annual-equivalent amount is `amount * periods_per_year`
    and its monthly-equivalent is that divided by 12, both exact Decimal arithmetic; only
    the per-kind sum is quantized to cents, with banker's rounding, not each row.
    """
    monthly = {kind: Decimal(0) for kind in SubscriptionKind}
    annual = {kind: Decimal(0) for kind in SubscriptionKind}
    for row in rows:
        periods = _PERIODS_PER_YEAR[row.cadence]
        annual_equivalent = row.amount * periods
        annual[row.kind] += annual_equivalent
        monthly[row.kind] += annual_equivalent / 12
    return {
        kind: SubscriptionTotals(
            monthly=_normalize_zero(monthly[kind].quantize(_CENT, rounding=ROUND_HALF_EVEN)),
            annual=_normalize_zero(annual[kind].quantize(_CENT, rounding=ROUND_HALF_EVEN)),
        )
        for kind in SubscriptionKind
    }


def subscription_out(
    db: Session, user_id: uuid.UUID, row: Subscription, now: datetime
) -> SubscriptionOut:
    """`row` as the API returns it, with its detection `flags` as of `now`."""
    flags = subscription_flags(db, user_id, [row], now)[row.id]
    return SubscriptionOut.model_validate(row).model_copy(update={"flags": flags})


def list_subscriptions(
    db: Session, user_id: uuid.UUID, include_inactive: bool, now: datetime
) -> SubscriptionPage:
    """Active subscriptions by default; `include_inactive=True` also lists closed ones.

    Totals only ever cover active rows, regardless of `include_inactive`: an inactive bill
    no longer costs anything, whether or not it's still shown for reference. Each item
    carries its detection `flags` as of `now`.
    """
    stmt = owned(Subscription, user_id)
    if not include_inactive:
        stmt = stmt.where(Subscription.active.is_(True))
    stmt = stmt.order_by(
        Subscription.next_due.is_(None), Subscription.next_due.asc(), Subscription.id.asc()
    )
    rows = list(db.scalars(stmt))

    active_rows = rows if not include_inactive else [row for row in rows if row.active]
    flags = subscription_flags(db, user_id, rows, now)
    return SubscriptionPage(
        items=[
            SubscriptionOut.model_validate(row).model_copy(update={"flags": flags[row.id]})
            for row in rows
        ],
        totals=_totals_by_kind(active_rows),
    )
