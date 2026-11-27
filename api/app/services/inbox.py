"""The review inbox: the transactions that need the user's attention, each with a
suggested category and bucket.

That is every `reviewed=False` row (pending included), plus any unmatched manual row
even if reviewed (`unmatched_manual`). A split parent is `reviewed=True` unless sync
reset it because the bank changed its amount, so an unreviewed one is flagged
`needs_resplit`. A row a rule matched is already `reviewed=True` (#51) and stays out.
"""

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.models import AISuggestion, Transaction
from app.models.enums import AISuggestionStatus, Bucket
from app.schemas.inbox import InboxItem, InboxPage, InboxQuery
from app.scoping import owned
from app.services.paging import filter_date_cursor, paginate
from app.services.splits import has_split_children
from app.services.transactions import to_out_many, unmatched_manual_clause


def _suggestions(
    db: Session, user_id: uuid.UUID, merchants: Collection[str]
) -> dict[str, Transaction]:
    """The user's most recent reviewed transaction for each of `merchants`, in one query."""
    if not merchants:
        return {}
    stmt = (
        owned(Transaction, user_id)
        .ext(distinct_on(Transaction.merchant))
        .where(
            Transaction.reviewed.is_(True),
            ~has_split_children(),
            Transaction.merchant.in_(merchants),
        )
        .order_by(Transaction.merchant, Transaction.posted_date.desc(), Transaction.id.desc())
    )
    return {row.merchant: row for row in db.scalars(stmt) if row.merchant is not None}


def _ai_suggestions(
    db: Session, user_id: uuid.UUID, rows: Collection[Transaction]
) -> dict[uuid.UUID, AISuggestion]:
    """Claude's answered suggestions for `rows` (#169), by transaction id, in one query."""
    if not rows:
        return {}
    stmt = select(AISuggestion).where(
        AISuggestion.user_id == user_id,
        AISuggestion.status == AISuggestionStatus.ready,
        AISuggestion.transaction_id.in_([row.id for row in rows]),
    )
    return {s.transaction_id: s for s in db.scalars(stmt)}


_Source = Literal["history", "ai"]

# Confirming a suggestion that this many reviewed rows already share makes three (#169).
RULE_WORTHY_AFTER = 2


def _confirmed_counts(
    db: Session, user_id: uuid.UUID, pairs: Collection[tuple[str, uuid.UUID]]
) -> dict[tuple[str, uuid.UUID], int]:
    """How many reviewed rows have each (merchant, category) pair, in one query."""
    if not pairs:
        return {}
    stmt = (
        select(Transaction.merchant, Transaction.category_id, func.count())
        .where(
            Transaction.user_id == user_id,
            Transaction.reviewed.is_(True),
            Transaction.merchant.in_({merchant for merchant, _ in pairs}),
            Transaction.category_id.in_({category for _, category in pairs}),
        )
        .group_by(Transaction.merchant, Transaction.category_id)
    )
    return {
        (merchant, category): count
        for merchant, category, count in db.execute(stmt)
        if merchant is not None and category is not None
    }


def needs_resplit_clause() -> ColumnElement[bool]:
    """An unreviewed split parent: sync reset it because the bank changed its amount."""
    return Transaction.reviewed.is_(False) & has_split_children()


def _flags(
    db: Session, rows: Collection[Transaction], now: datetime
) -> dict[uuid.UUID, tuple[bool, bool]]:
    """`(needs_resplit, unmatched_manual)` for each of `rows`, in one query."""
    if not rows:
        return {}
    stmt = select(
        Transaction.id,
        needs_resplit_clause(),
        unmatched_manual_clause(now),
    ).where(Transaction.id.in_([row.id for row in rows]))
    return {id: (resplit, unmatched) for id, resplit, unmatched in db.execute(stmt)}


type Pick = tuple[uuid.UUID | None, Bucket | None, str | None, _Source]


def suggestions(
    db: Session, user_id: uuid.UUID, rows: Collection[Transaction]
) -> dict[uuid.UUID, Pick]:
    """(category, bucket, merchant, source) for each of `rows` that has a suggestion: from
    history first, then Claude."""
    history = _suggestions(db, user_id, {row.merchant for row in rows if row.merchant is not None})
    from_ai = _ai_suggestions(db, user_id, rows)
    picks: dict[uuid.UUID, Pick] = {}
    for row in rows:
        hit = history.get(row.merchant) if row.merchant is not None else None
        if hit is not None:
            picks[row.id] = (hit.category_id, hit.bucket, row.merchant, "history")
        elif (guess := from_ai.get(row.id)) is not None:
            picks[row.id] = (guess.category_id, guess.bucket, guess.merchant, "ai")
    return picks


def inbox_base(user_id: uuid.UUID, now: datetime) -> Select[Transaction]:
    """The user's Inbox rows, unordered."""
    return owned(Transaction, user_id).where(
        or_(Transaction.reviewed.is_(False), unmatched_manual_clause(now))
    )


def list_inbox(db: Session, user_id: uuid.UUID, query: InboxQuery, now: datetime) -> InboxPage:
    base = inbox_base(user_id, now)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    stmt = filter_date_cursor(base, Transaction.posted_date, Transaction.id, query.cursor)
    stmt = stmt.order_by(Transaction.posted_date.desc(), Transaction.id.desc())
    rows, next_cursor = paginate(db, stmt, query.limit, lambda row: (row.posted_date, row.id))

    picks = suggestions(db, user_id, rows)
    flags = _flags(db, rows, now)
    pairs = {(m, c) for c, _, m, _ in picks.values() if m is not None and c is not None}
    counts = _confirmed_counts(db, user_id, pairs)

    items: list[InboxItem] = []
    for row, out in zip(rows, to_out_many(db, rows), strict=True):
        pick = picks.get(row.id)
        category, bucket, merchant, source = pick if pick else (None, None, None, None)
        needs_resplit, unmatched_manual = flags[row.id]
        items.append(
            InboxItem(
                **out.model_dump(),
                suggested_category_id=category,
                suggested_bucket=bucket,
                suggestion_source=source,
                suggested_merchant=merchant if source == "ai" else None,
                rule_worthy=bool(
                    merchant
                    and category
                    and counts.get((merchant, category), 0) >= RULE_WORTHY_AFTER
                ),
                needs_resplit=needs_resplit,
                unmatched_manual=unmatched_manual,
            )
        )
    return InboxPage(items=items, next_cursor=next_cursor, total=total)
