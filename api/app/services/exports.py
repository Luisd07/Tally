"""CSV and tax-year zip exports.

`export_select` builds the joined, labeled statement every CSV export narrows with
its own filters (`app.services.transactions._apply_filters` for #73's
`TransactionListQuery`, a tax year for #72); `stream_transaction_csv` is the writer
that turns its rows into CSV text. `_COLUMNS` is the single source of truth for both:
the header, the selected/labeled column, and how to format it all come from the same
tuple, so the two can't drift out of sync the way a hand-written `select(...)` and a
hand-written positional unpack could.
"""

import csv
import io
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Protocol

from sqlalchemy import ColumnElement, Select, and_, select
from sqlalchemy.orm import InstrumentedAttribute

from app.models import Account, Category, Event, Transaction
from app.services.splits import has_split_children

# Excel and Google Sheets read a cell starting with any of these as a formula; a
# transaction's free-text fields (description, merchant, notes, tax_line) and the
# names joined in (account, category, event) all come from a bank feed or the
# user's own typing, so none of them are safe to trust verbatim in a file meant to
# be opened in a spreadsheet.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_csv_cell(value: str | None) -> str:
    """`value` for a text cell, prefixed with `'` when it would otherwise be read as a
    spreadsheet formula. `None` becomes `""`. Never applied to a numeric column
    (`amount`): a negative amount is data, not an injection attempt.
    """
    if value is None:
        return ""
    if value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _bool_cell(value: bool) -> str:
    return "true" if value else "false"


def _optional(formatter: Callable[[Any], str]) -> Callable[[Any], str]:
    """`formatter`, short-circuited to `""` for `None` -- every nullable non-text
    column (`transacted_at`, `parent_id`) needs this; `escape_csv_cell` already
    handles `None` itself, so the text columns use it directly instead.
    """

    def wrapped(value: Any) -> str:
        return "" if value is None else formatter(value)

    return wrapped


# The transactions CSV export's columns, in header order: `(header, column, format)`.
# `export_select` selects `column.label(header)` for each entry, and
# `_transaction_csv_row` reads each formatted row back by that same `header`, so the
# two can only ever agree on order -- a select built in a different column order
# still lines up correctly (see `test_stream_transaction_csv_aligns_columns_by_label`).
#
# A split parent (#80) is included like any other row, with `is_split` set to
# `true`; its children, which also appear, already sum to its amount. A total over
# the file (in a spreadsheet formula or anywhere else) should exclude rows where
# `is_split` is `true`, the same way the dashboard and tax totals do, so the parent's
# amount isn't counted twice.
_COLUMNS: tuple[
    tuple[str, InstrumentedAttribute[Any] | ColumnElement[Any], Callable[[Any], str]], ...
] = (
    ("id", Transaction.id, str),
    ("posted_date", Transaction.posted_date, lambda v: v.isoformat()),
    ("transacted_at", Transaction.transacted_at, _optional(lambda v: v.isoformat())),
    ("account", Account.name, escape_csv_cell),
    ("amount", Transaction.amount, str),
    ("description", Transaction.description_raw, escape_csv_cell),
    ("merchant", Transaction.merchant, escape_csv_cell),
    ("category", Category.name, escape_csv_cell),
    ("tax_line", Category.tax_line, escape_csv_cell),
    ("bucket", Transaction.bucket, lambda v: v.value),
    ("source", Transaction.source, lambda v: v.value),
    ("pending", Transaction.pending, _bool_cell),
    ("reviewed", Transaction.reviewed, _bool_cell),
    ("notes", Transaction.notes, escape_csv_cell),
    ("event", Event.name, escape_csv_cell),
    ("parent_id", Transaction.parent_id, _optional(str)),
    ("is_split", has_split_children(), _bool_cell),
)

TRANSACTION_CSV_COLUMNS: tuple[str, ...] = tuple(header for header, _, _ in _COLUMNS)


def export_select(user_id: uuid.UUID) -> Select[Any]:
    """The transactions CSV export's base statement: every transaction `user_id`
    owns, joined to `Account`, `Category` and `Event` for the names `_COLUMNS` needs,
    with the CSV's columns selected and labeled from that same spec.

    Each join's `user_id` scope lives in its own `ON` clause (`and_`), not a `WHERE`,
    so a transaction with a foreign `category_id`/`event_id` (the DB doesn't enforce
    same-user FKs -- see `app/services/transactions.py`) still keeps its row through
    the outer join, just with a blank name, rather than a `WHERE` silently dropping
    the row or leaking someone else's name onto it.

    Unfiltered beyond that and in no particular order: callers narrow it with their
    own filters and `order_by` (`app.services.transactions.stream_export_rows` for
    #73's `TransactionListQuery`, a tax year for #72).
    """
    return (
        select(*(column.label(header) for header, column, _ in _COLUMNS))
        .select_from(Transaction)
        .join(Account, and_(Account.id == Transaction.account_id, Account.user_id == user_id))
        .outerjoin(
            Category,
            and_(Category.id == Transaction.category_id, Category.user_id == user_id),
        )
        .outerjoin(Event, and_(Event.id == Transaction.event_id, Event.user_id == user_id))
        .where(Transaction.user_id == user_id)
    )


class _MappingRow(Protocol):
    """What `stream_transaction_csv` needs from a row: a `sqlalchemy.engine.Row`'s
    `_mapping`, keyed by the label each column in `_COLUMNS` was selected with.
    Structural rather than `sqlalchemy.engine.Row[...]` itself, since `Row`'s type
    parameters are invariant over the exact column types and arity, which a caller
    narrowing `export_select` to its own column order can't be pinned to. The key
    type is `Any`, not `str`: `Row._mapping` is keyed by `str | int | ColumnElement`
    (you can also look a value up by its original column), and `Mapping`'s key type
    is invariant, so pinning this to `Mapping[str, Any]` would reject a real `Row`.
    """

    @property
    def _mapping(self) -> Mapping[Any, Any]: ...


def _transaction_csv_row(row: _MappingRow) -> list[str]:
    mapping = row._mapping
    return [formatter(mapping[header]) for header, _, formatter in _COLUMNS]


def stream_transaction_csv(rows: Iterable[_MappingRow]) -> Iterator[str]:
    """`rows` (from a statement built on `export_select`, e.g. `app.services.
    transactions.stream_export_rows`) as CSV text, one chunk per header/row so a
    `StreamingResponse` sends each as it's produced instead of building the whole
    file in memory first. Starts with a UTF-8 BOM: Excel on the desktop otherwise
    reads a BOM-less file as the system codepage and mangles non-ASCII names.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def _drain() -> str:
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return text

    writer.writerow(TRANSACTION_CSV_COLUMNS)
    yield "\ufeff" + _drain()
    for row in rows:
        writer.writerow(_transaction_csv_row(row))
        yield _drain()
