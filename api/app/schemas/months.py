"""The `YYYY-MM` month filter shared by every list endpoint that offers one
(`transactions`, `income`, `dashboard`, and `home` in #45): one regex, one range
rule, one error message, so a future month-filtered list can't drift from the others.
"""

import re
from datetime import date
from typing import Annotated

from pydantic import AfterValidator

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def month_range(month: str) -> tuple[date, date]:
    """The `[start, end)` date range a `YYYY-MM` string covers.

    Raises `ValueError` when the year is out of `date`'s representable range (the
    December rollover at `0000` or `9999`), so a validator can turn that into a 422
    the same way it does a badly formatted string.
    """
    year, mon = int(month[:4]), int(month[5:7])
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    return start, end


def shift_month(month: str, offset: int) -> str:
    """The `YYYY-MM` string `offset` months before (negative) or after `month`.

    Raises `ValueError` when the shifted year falls outside `date`'s representable
    range (year < 1 or > 9999), the same boundary `month_range` enforces for `month`
    itself, so a caller that feeds the result back into `month_range` never hits an
    unhandled `date()` error.
    """
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) + offset
    year, mon = divmod(total, 12)
    mon += 1
    if not 1 <= year <= 9999:
        raise ValueError("month shift out of range")
    return f"{year:04d}-{mon:02d}"


def _validate_month(value: str) -> str:
    if not _MONTH_RE.fullmatch(value):
        raise ValueError("month must be formatted YYYY-MM")
    try:
        month_range(value)
    except ValueError as exc:
        raise ValueError("month is out of range") from exc
    return value


Month = Annotated[str, AfterValidator(_validate_month)]
