"""Shared page-assembly step for keyset-paged list endpoints.

Every list service filters and orders its own `Select`, then hands it here to fetch
one row past `limit`, decide whether there's a next page from that extra row, and
encode the cursor — the off-by-one that's easy to get wrong and otherwise gets
copied into every new paged feed.
"""

import uuid
from collections.abc import Callable
from datetime import date, datetime

from sqlalchemy import Select, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.schemas.paging import decode_date_cursor, encode_cursor


def paginate[RowT](
    db: Session,
    stmt: Select[RowT],
    limit: int,
    sort_key: Callable[[RowT], tuple[date | datetime, uuid.UUID]],
) -> tuple[list[RowT], str | None]:
    """Run `stmt` (already filtered, keyset-filtered and ordered by the caller) and
    return up to `limit` rows plus the cursor for the next page, or `None` on the
    last page.

    `sort_key` pulls the `(value, id)` pair `encode_cursor` needs out of the last
    row — the same two columns the caller orders by and filters the cursor on.
    """
    rows = list(db.scalars(stmt.limit(limit + 1)).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        value, id_ = sort_key(rows[-1])
        next_cursor = encode_cursor(value, id_)
    return rows, next_cursor


def filter_date_cursor[RowT](
    stmt: Select[RowT],
    date_column: InstrumentedAttribute[date],
    id_column: InstrumentedAttribute[uuid.UUID],
    cursor: str | None,
) -> Select[RowT]:
    """Keyset filter shared by every `<date_column> DESC, id_column DESC` feed: `stmt`
    must already order by that same pair. A `None` cursor (the first page) is a no-op.
    """
    if cursor is None:
        return stmt
    cursor_date, cursor_id = decode_date_cursor(cursor)
    return stmt.where(tuple_(date_column, id_column) < tuple_(cursor_date, cursor_id))
