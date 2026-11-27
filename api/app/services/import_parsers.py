"""Bank statement parsers for #54: CSV (driven by a saved `ImportMapping`) and OFX/QFX.

Pure functions over bytes: they never touch the database. A bad row becomes an
`ImportRowError`; a bad file (unreadable, too many rows, a missing mapped column) is a
422.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.errors import UnprocessableEntity
from app.models import ImportMapping
from app.models.import_mapping import SignConvention
from app.schemas.imports import ImportRowError

MAX_ROWS = 5000
MAX_DESCRIPTION = 500
# transactions.amount is NUMERIC(12,2).
MAX_AMOUNT = Decimal("9999999999.99")


@dataclass
class ParsedRow:
    row: int
    posted_date: date
    amount: Decimal
    description: str


@dataclass
class Parsed:
    rows: list[ParsedRow]
    errors: list[ImportRowError]


def _parse_amount(text: str) -> Decimal:
    """`$1,234.50`, `(12.00)` and `12.00-` all parse; anything else is a `ValueError`."""
    value = text.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not value:
        raise ValueError("missing amount")
    negative = False
    if value.startswith("(") and value.endswith(")"):
        negative, value = True, value[1:-1]
    if value.endswith("-"):
        negative, value = True, value[:-1]
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"invalid amount {text.strip()!r}") from None
    if not amount.is_finite():
        raise ValueError(f"invalid amount {text.strip()!r}")
    if amount.copy_abs() > MAX_AMOUNT:
        raise ValueError(f"amount {text.strip()!r} is out of range")
    cents = amount.quantize(Decimal("0.01"))
    if amount != cents:
        raise ValueError(f"amount {text.strip()!r} has more than 2 decimal places")
    return -cents if negative else cents


def _decode(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    # PostgreSQL text can't hold NUL.
    return text.replace("\x00", "")


def _resolve_columns(header: list[str] | None, mapping: ImportMapping) -> dict[str, int | None]:
    """Column name or index -> position. A name missing from the header is a file-level 422."""
    refs = {
        "date": mapping.date_column,
        "description": mapping.description_column,
        "amount": mapping.amount_column,
        "debit": mapping.debit_column,
        "credit": mapping.credit_column,
    }
    if header is None:
        return {key: int(ref) if ref is not None else None for key, ref in refs.items()}
    lookup = {name.strip().casefold(): i for i, name in enumerate(header)}
    positions: dict[str, int | None] = {}
    for key, ref in refs.items():
        if ref is None:
            positions[key] = None
        elif ref.strip().casefold() in lookup:
            positions[key] = lookup[ref.strip().casefold()]
        else:
            raise UnprocessableEntity.value_error(f"column {ref!r} not found in the CSV header")
    return positions


def _cell(record: list[str], position: int | None) -> str:
    if position is None or position >= len(record):
        return ""
    return record[position].strip()


def _row_amount(
    mapping: ImportMapping, columns: dict[str, int | None], record: list[str]
) -> Decimal:
    if mapping.amount_column is not None:
        amount = _parse_amount(_cell(record, columns["amount"]))
        return -amount if mapping.sign_convention == SignConvention.positive_is_out else amount
    debit, credit = _cell(record, columns["debit"]), _cell(record, columns["credit"])
    if not debit and not credit:
        raise ValueError("no debit or credit amount")
    out = abs(_parse_amount(debit)) if debit else Decimal(0)
    into = abs(_parse_amount(credit)) if credit else Decimal(0)
    return into - out


def _parse_record(
    record: list[str], line: int, mapping: ImportMapping, columns: dict[str, int | None]
) -> ParsedRow:
    try:
        posted = datetime.strptime(_cell(record, columns["date"]), mapping.date_format)
    except ValueError:
        raise ValueError("invalid date") from None
    amount = _row_amount(mapping, columns, record)
    description = _cell(record, columns["description"])
    if not description:
        raise ValueError("missing description")
    return ParsedRow(line, posted.date(), amount, description[:MAX_DESCRIPTION])


def parse_csv(content: bytes, mapping: ImportMapping) -> Parsed:
    reader = csv.reader(io.StringIO(_decode(content), newline=""))
    parsed = Parsed(rows=[], errors=[])
    seen = 0
    try:
        header = None
        if mapping.has_header:
            header = next(reader, None)
            if header is None:
                raise UnprocessableEntity.value_error("the CSV file is empty")
        columns = _resolve_columns(header, mapping)
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue
            seen += 1
            if seen > MAX_ROWS:
                raise UnprocessableEntity.value_error(f"a file may have at most {MAX_ROWS} rows")
            try:
                parsed.rows.append(_parse_record(record, reader.line_num, mapping, columns))
            except ValueError as exc:
                parsed.errors.append(ImportRowError(row=reader.line_num, message=str(exc)))
    except csv.Error as exc:
        raise UnprocessableEntity.value_error(f"the file is not a readable CSV: {exc}") from exc
    return parsed


_OFX_BLOCK = re.compile(r"<STMTTRN>(.*?)(?=</STMTTRN>|<STMTTRN>|</BANKTRANLIST>|\Z)", re.S | re.I)


def _ofx_tag(block: str, tag: str) -> str:
    """A leaf value in either OFX 1.x (SGML, no closing tags) or 2.x (XML)."""
    match = re.search(rf"<{tag}>\s*([^<\r\n]*)", block, re.I)
    return match.group(1).strip() if match else ""


def _ofx_row(number: int, block: str) -> ParsedRow:
    try:
        posted = datetime.strptime(_ofx_tag(block, "DTPOSTED")[:8], "%Y%m%d").date()
    except ValueError:
        raise ValueError("invalid date") from None
    raw_amount = _ofx_tag(block, "TRNAMT")
    amount = _parse_amount(raw_amount if "." in raw_amount else raw_amount.replace(",", "."))
    parts = (_ofx_tag(block, "NAME"), _ofx_tag(block, "MEMO"))
    description = " ".join(dict.fromkeys(part for part in parts if part))
    if not description:
        raise ValueError("missing description")
    return ParsedRow(number, posted, amount, description[:MAX_DESCRIPTION])


def parse_ofx(content: bytes) -> Parsed:
    text = _decode(content)
    if not re.search(r"<(OFX|STMTTRN)>", text, re.I):
        raise UnprocessableEntity.value_error("the file is not an OFX/QFX statement")
    parsed = Parsed(rows=[], errors=[])
    for number, match in enumerate(_OFX_BLOCK.finditer(text), start=1):
        if number > MAX_ROWS:
            raise UnprocessableEntity.value_error(f"a file may have at most {MAX_ROWS} rows")
        try:
            parsed.rows.append(_ofx_row(number, match.group(1)))
        except ValueError as exc:
            parsed.errors.append(ImportRowError(row=number, message=str(exc)))
    return parsed
