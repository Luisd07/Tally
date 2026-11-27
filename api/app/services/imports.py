"""Statement import (#54): bank CSV (with a saved per-account column mapping) and OFX/QFX.

Import fills only what the SimpleFIN backfill can't reach. SimpleFIN descriptions differ
from a bank's own export, so a fingerprint match across the two sources is unreliable;
instead a row is inserted only if it is dated before the account's earliest
SimpleFIN-sourced row, and the fingerprint (`app.services.sync.fingerprint`) dedupes
re-uploads of the same file. Inserted rows run through the same hooks a sync's new rows
do: `rules.apply_rules`, then `matching.match_transfers`.
"""

import uuid
from collections import Counter
from datetime import date

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import NotFound, PayloadTooLarge, UnprocessableEntity
from app.models import Account, ImportMapping, Transaction
from app.models.enums import Source
from app.schemas.imports import ImportMappingIn, ImportResult
from app.scoping import get_owned
from app.services import matching, rules
from app.services.import_parsers import Parsed, ParsedRow, parse_csv, parse_ofx
from app.services.sync import fingerprint, lock_user

MAX_FILE_BYTES = 2_000_000


# --- mapping: PUT/GET /api/imports/mappings/{account_id} -------------------------------


def _find_mapping(db: Session, user_id: uuid.UUID, account_id: uuid.UUID) -> ImportMapping | None:
    return db.scalars(
        select(ImportMapping).where(
            ImportMapping.account_id == account_id, ImportMapping.user_id == user_id
        )
    ).one_or_none()


def get_mapping(db: Session, user_id: uuid.UUID, account_id: uuid.UUID) -> ImportMapping:
    get_owned(db, Account, account_id, user_id)
    mapping = _find_mapping(db, user_id, account_id)
    if mapping is None:
        raise NotFound()
    return mapping


def put_mapping(
    db: Session, user_id: uuid.UUID, account_id: uuid.UUID, data: ImportMappingIn
) -> ImportMapping:
    get_owned(db, Account, account_id, user_id)
    values = data.model_dump()
    mapping = _find_mapping(db, user_id, account_id)
    if mapping is None:
        mapping = ImportMapping(user_id=user_id, account_id=account_id, **values)
        db.add(mapping)
    else:
        for field, value in values.items():
            setattr(mapping, field, value)
    db.flush()
    return mapping


# --- insert ------------------------------------------------------------------------------


def _earliest_synced(db: Session, user_id: uuid.UUID, account_id: uuid.UUID) -> date | None:
    return db.scalar(
        select(func.min(Transaction.posted_date)).where(
            Transaction.user_id == user_id,
            Transaction.account_id == account_id,
            Transaction.source == Source.simplefin,
        )
    )


def _existing_counts(
    db: Session, user_id: uuid.UUID, account_id: uuid.UUID, fingerprints: set[str]
) -> Counter[str]:
    rows = db.execute(
        select(Transaction.fingerprint, func.count())
        .where(
            Transaction.user_id == user_id,
            Transaction.account_id == account_id,
            Transaction.fingerprint.in_(fingerprints),
        )
        .group_by(Transaction.fingerprint)
    )
    return Counter({fp: count for fp, count in rows if fp is not None})


def _new_rows(
    db: Session, user_id: uuid.UUID, account_id: uuid.UUID, rows: list[ParsedRow]
) -> list[Transaction]:
    prints = [fingerprint(account_id, r.posted_date, r.amount, r.description) for r in rows]
    # Two identical charges on one day are legitimate, so a fingerprint is a multiset: a row
    # is a duplicate only while existing rows of that fingerprint remain to account for it.
    remaining = _existing_counts(db, user_id, account_id, set(prints))
    new_rows: list[Transaction] = []
    for row, print_ in zip(rows, prints, strict=True):
        if remaining[print_] > 0:
            remaining[print_] -= 1
            continue
        new_rows.append(
            Transaction(
                user_id=user_id,
                account_id=account_id,
                posted_date=row.posted_date,
                amount=row.amount,
                description_raw=row.description,
                source=Source.csv,
                pending=False,
                fingerprint=print_,
            )
        )
    return new_rows


def _run_hooks(db: Session, user_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    rules.apply_rules(db, user_id, ids)
    matching.match_transfers(db, user_id, ids, rules.load_rule_index(db, user_id))


def _insert(db: Session, user_id: uuid.UUID, account_id: uuid.UUID, parsed: Parsed) -> ImportResult:
    earliest = _earliest_synced(db, user_id, account_id)
    in_range = [r for r in parsed.rows if earliest is None or r.posted_date < earliest]
    new_rows = _new_rows(db, user_id, account_id, in_range)
    db.add_all(new_rows)
    db.flush()
    _run_hooks(db, user_id, [txn.id for txn in new_rows])
    return ImportResult(
        inserted=len(new_rows),
        skipped_duplicate=len(in_range) - len(new_rows),
        skipped_synced_range=len(parsed.rows) - len(in_range),
        errors=parsed.errors,
    )


def _check_size(content: bytes) -> None:
    if len(content) > MAX_FILE_BYTES:
        raise PayloadTooLarge(f"File is too large (max {MAX_FILE_BYTES // 1_000_000} MB).")


def import_csv(
    db: Session, user_id: uuid.UUID, account_id: uuid.UUID, content: bytes
) -> ImportResult:
    get_owned(db, Account, account_id, user_id)
    _check_size(content)
    lock_user(db, user_id)
    mapping = _find_mapping(db, user_id, account_id)
    if mapping is None:
        raise UnprocessableEntity.value_error("save a column mapping for this account first")
    result = _insert(db, user_id, account_id, parse_csv(content, mapping))
    logger.info("CSV import for account {}: {}", account_id, result.model_dump(exclude={"errors"}))
    return result


def import_ofx(
    db: Session, user_id: uuid.UUID, account_id: uuid.UUID, content: bytes
) -> ImportResult:
    get_owned(db, Account, account_id, user_id)
    _check_size(content)
    lock_user(db, user_id)
    result = _insert(db, user_id, account_id, parse_ofx(content))
    logger.info("OFX import for account {}: {}", account_id, result.model_dump(exclude={"errors"}))
    return result
