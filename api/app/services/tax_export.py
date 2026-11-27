"""The tax-year export (#72): a zip with the year's business transactions as a CSV,
their receipt files, and the mileage log (#167) as a second CSV.

The rows are the ones the tax summary counts (`app.services.tax.tax_rows`, over
`expense_buckets`). For every line except line 9, the rows' `amount x deductible_pct`
adds up to that line's deductible on the summary. Line 9 (`car_truck`) is built
differently: it is the mileage amount from `mileage-{year}.csv` plus the rows tagged
`car_parking_tolls` (which keep that tag here). Rows tagged `car_truck` are actual car
costs, which the standard mileage rate replaces, so they show `deductible_pct` 0
(`app.services.tax.effective_deductible_pct`) and add nothing to the summary.

A split parent carries the receipt for its children (#80), so each child's row lists the
parent's receipts too. Everything the zip needs from Postgres is read up front by
`plan_export`; `stream_zip` then only touches the receipts folder, so it doesn't depend
on the request's DB session outliving the route.
"""

import csv
import io
import re
import uuid
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Category, Receipt, Transaction
from app.services import tax_review
from app.services.exports import escape_csv_cell
from app.services.mileage import find_odometer, priced_trips, summarize_mileage
from app.services.tax import effective_deductible_pct, expense_buckets, tax_rows

TAX_EXPORT_COLUMNS: tuple[str, ...] = (
    "date",
    "merchant",
    "description",
    "amount",
    "category",
    "tax_line",
    "deductible_pct",
    "bucket",
    "receipts",
)

MILEAGE_EXPORT_COLUMNS: tuple[str, ...] = (
    "date",
    "miles",
    "purpose",
    "start",
    "end",
    "rate",
    "amount",
)

_CENT = Decimal("0.01")
_CHUNK_BYTES = 64 * 1024
_SLUG_MAX = 40


@dataclass
class _Row:
    posted_date: date
    merchant: str | None
    description: str
    amount: str
    category: str | None
    tax_line: str | None
    deductible_pct: int | None
    bucket: str
    receipt_names: list[str] = field(default_factory=list)


@dataclass
class _PlannedFile:
    name: str  # inside the zip
    path: Path  # on disk
    label: str  # how missing.txt names it


@dataclass
class ExportPlan:
    year: int
    rows: list[_Row]
    files: list[_PlannedFile]
    missing: list[str]
    mileage_rows: list[list[str]]
    # The year's AI review for the preparer (#175), when one is ready.
    ai_review: str | None = None


class _Sink(io.RawIOBase):
    """A write-only, non-seekable file that `zipfile` can stream into: `drain()` hands
    back whatever has been written since the last call.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._position = 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        view = memoryview(data)
        self._buffer += view
        self._position += view.nbytes
        return view.nbytes

    def tell(self) -> int:
        return self._position

    def drain(self) -> bytes:
        chunk = bytes(self._buffer)
        self._buffer.clear()
        return chunk


def _slug(text: str | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:_SLUG_MAX].strip("-")
    return slug or "unknown"


def _extension(file_path: str) -> str:
    suffix = re.sub(r"[^a-z0-9]", "", Path(file_path).suffix.lower())[:5]
    return suffix or "jpg"


def _receipt_path(base: Path, file_path: str) -> Path | None:
    """The receipt's file under `base` (the resolved `receipts_dir`), or `None` when
    the row points outside it or the file isn't there.
    """
    full = (base / file_path).resolve()
    if not full.is_relative_to(base) or not full.is_file():
        return None
    return full


def _query_rows(
    db: Session, user_id: uuid.UUID, year: int, settings: Settings
) -> list[tuple[Transaction, str | None, str | None, int | None]]:
    stmt = (
        select(Transaction, Category.name, Category.tax_line, Category.deductible_pct)
        .outerjoin(
            Category,
            and_(Category.id == Transaction.category_id, Category.user_id == user_id),
        )
        .where(
            *tax_rows(user_id, expense_buckets(settings), date(year, 1, 1), date(year + 1, 1, 1))
        )
        .order_by(Transaction.posted_date, Transaction.id)
    )
    return [tuple(r) for r in db.execute(stmt).all()]  # pyright: ignore[reportReturnType]


def _receipts_by_transaction(
    db: Session, user_id: uuid.UUID, transactions: list[Transaction]
) -> dict[uuid.UUID, list[Receipt]]:
    """Receipts of the given transactions and of their split parents."""
    ids = {t.id for t in transactions} | {t.parent_id for t in transactions if t.parent_id}
    found: dict[uuid.UUID, list[Receipt]] = {}
    if not ids:
        return found
    stmt = (
        select(Receipt)
        .where(Receipt.user_id == user_id, Receipt.transaction_id.in_(ids))
        .order_by(Receipt.uploaded_at, Receipt.id)
    )
    for receipt in db.scalars(stmt):
        found.setdefault(receipt.transaction_id, []).append(receipt)
    return found


def _label(transaction: Transaction, receipt: Receipt) -> str:
    return (
        f"{transaction.posted_date.isoformat()} "
        f"{transaction.merchant or transaction.description_raw}: "
        f"receipt {receipt.id} ({receipt.file_path})"
    )


def _entry_name(transaction: Transaction, receipt: Receipt) -> str:
    return (
        f"receipts/{transaction.posted_date.isoformat()}_"
        f"{_slug(transaction.merchant or transaction.description_raw)}_"
        f"{str(receipt.id)[:8]}.{_extension(receipt.file_path)}"
    )


def _plan_receipt(
    base: Path, transaction: Transaction, receipt: Receipt, plan: ExportPlan
) -> str | None:
    """Plan one receipt's zip entry, returning its name, or `None` after recording it
    as missing.
    """
    path = _receipt_path(base, receipt.file_path)
    label = _label(transaction, receipt)
    if path is None:
        plan.missing.append(label)
        return None
    name = _entry_name(transaction, receipt)
    plan.files.append(_PlannedFile(name, path, label))
    return name


def _optional(value: Decimal | None) -> str:
    return "" if value is None else str(value)


def _mileage_rows(db: Session, user_id: uuid.UUID, year: int) -> list[list[str]]:
    """The mileage CSV's cells: one row per trip, oldest first, then a blank row and
    the year's totals. Each trip's `amount` is shown to the cent; the totals come from
    `app.services.mileage.summarize_mileage`, the same figures the summary shows.
    """
    priced = priced_trips(db, user_id, year)
    rows = [
        [
            p.trip.date.isoformat(),
            str(p.trip.miles),
            escape_csv_cell(p.trip.purpose),
            escape_csv_cell(p.trip.start_location),
            escape_csv_cell(p.trip.end_location),
            _optional(p.rate.rate if p.rate else None),
            str(p.amount.quantize(_CENT, rounding=ROUND_HALF_EVEN)),
        ]
        for p in priced
    ]
    totals = summarize_mileage(priced, find_odometer(db, user_id, year))
    rows += [
        [],
        ["business miles", str(totals.business_miles)],
        ["mileage amount", str(totals.mileage_amount)],
        ["odometer start", _optional(totals.odometer_start)],
        ["odometer end", _optional(totals.odometer_end)],
        ["total miles", _optional(totals.total_miles)],
    ]
    return rows


def _ai_review(db: Session, user_id: uuid.UUID, year: int) -> str | None:
    """The year's AI review text (#175). A failure reading it leaves the file out rather than
    costing the export everything else."""
    try:
        return tax_review.as_text(db, user_id, year)
    except Exception as exc:
        logger.warning("Tax export: AI review left out: {}", type(exc).__name__)
        return None


def plan_export(db: Session, user_id: uuid.UUID, year: int, settings: Settings) -> ExportPlan:
    found = _query_rows(db, user_id, year, settings)
    receipts = _receipts_by_transaction(db, user_id, [t for t, *_ in found])
    base = settings.receipts_dir.resolve()
    plan = ExportPlan(
        year=year,
        rows=[],
        files=[],
        missing=[],
        mileage_rows=_mileage_rows(db, user_id, year),
        ai_review=_ai_review(db, user_id, year),
    )
    names: dict[uuid.UUID, str | None] = {}

    for transaction, category, tax_line, deductible_pct in found:
        row = _Row(
            posted_date=transaction.posted_date,
            merchant=transaction.merchant,
            description=transaction.description_raw,
            amount=str(transaction.amount),
            category=category,
            tax_line=tax_line,
            deductible_pct=effective_deductible_pct(tax_line, deductible_pct),
            bucket=transaction.bucket.value,
        )
        plan.rows.append(row)
        linked = receipts.get(transaction.id, [])
        if transaction.parent_id:
            linked = linked + receipts.get(transaction.parent_id, [])
        for receipt in linked:
            if receipt.id not in names:
                names[receipt.id] = _plan_receipt(base, transaction, receipt, plan)
            if (name := names[receipt.id]) is not None:
                row.receipt_names.append(name)
    return plan


def _csv_line(values: list[str]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer).writerow(values)
    return buffer.getvalue().encode()


def _row_values(row: _Row) -> list[str]:
    return [
        row.posted_date.isoformat(),
        escape_csv_cell(row.merchant),
        escape_csv_cell(row.description),
        row.amount,
        escape_csv_cell(row.category),
        escape_csv_cell(row.tax_line),
        "" if row.deductible_pct is None else str(row.deductible_pct),
        row.bucket,
        " ".join(row.receipt_names),
    ]


def _write_csv_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    sink: _Sink,
    header: tuple[str, ...],
    rows: Iterable[list[str]],
) -> Iterator[bytes]:
    """Write one CSV into `archive` (UTF-8 BOM, then `header`, then `rows`), yielding
    what's been written after the header and after each row.
    """
    with archive.open(info, "w") as out:
        out.write(b"\xef\xbb\xbf" + _csv_line(list(header)))
        yield sink.drain()
        for row in rows:
            out.write(_csv_line(row))
            yield sink.drain()


def stream_zip(plan: ExportPlan) -> Iterator[bytes]:
    """The zip as byte chunks, written through a non-seekable sink so memory stays at
    one chunk however many receipts there are.
    """
    sink = _Sink()
    stamp = datetime.now(UTC).timetuple()[:6]

    def entry(name: str, compress: int) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=stamp)  # pyright: ignore[reportArgumentType]
        info.compress_type = compress
        return info

    with zipfile.ZipFile(sink, "w") as archive:
        yield from _write_csv_entry(
            archive,
            entry(f"transactions-{plan.year}.csv", zipfile.ZIP_DEFLATED),
            sink,
            TAX_EXPORT_COLUMNS,
            (_row_values(row) for row in plan.rows),
        )
        yield from _write_csv_entry(
            archive,
            entry(f"mileage-{plan.year}.csv", zipfile.ZIP_DEFLATED),
            sink,
            MILEAGE_EXPORT_COLUMNS,
            plan.mileage_rows,
        )

        if plan.ai_review:
            archive.writestr(
                entry(f"ai-review-{plan.year}.txt", zipfile.ZIP_DEFLATED), plan.ai_review
            )
            yield sink.drain()

        missing = list(plan.missing)
        for planned in plan.files:
            try:
                src = planned.path.open("rb")
            except OSError:
                missing.append(planned.label)
                continue
            with src, archive.open(entry(planned.name, zipfile.ZIP_STORED), "w") as out:
                while chunk := src.read(_CHUNK_BYTES):
                    out.write(chunk)
                    yield sink.drain()

        if missing:
            archive.writestr(
                entry("missing.txt", zipfile.ZIP_DEFLATED),
                "Receipt files that could not be found on disk:\n" + "\n".join(missing) + "\n",
            )
    yield sink.drain()
