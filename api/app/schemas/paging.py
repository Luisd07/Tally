"""Shared keyset-cursor codec for `(<sort column> DESC, id DESC)` list endpoints.

The cursor is opaque to the client: base64 of `<value>|<id>`, the last row of the
previous page. Each caller supplies its own ordering column's type via `parse`, since
a date and a datetime round-trip through `isoformat()`/`fromisoformat()` differently.
"""

import base64
import binascii
import uuid
from collections.abc import Callable
from datetime import date, datetime


def encode_cursor(value: date | datetime, id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{value.isoformat()}|{id}".encode()).decode()


def decode_cursor[T](cursor: str, parse: Callable[[str], T]) -> tuple[T, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        value_part, id_part = raw.split("|")
        return parse(value_part), uuid.UUID(id_part)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("cursor is invalid") from exc


def decode_date_cursor(cursor: str) -> tuple[date, uuid.UUID]:
    """The `(<date column>, id)` cursor shared by every `<date column> DESC, id DESC`
    feed keyed on a `date` column (transactions, the Tax screen's missing-receipt
    list) rather than a `datetime` one (auth events).
    """
    return decode_cursor(cursor, date.fromisoformat)
