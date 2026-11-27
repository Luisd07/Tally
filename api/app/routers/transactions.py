import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from app.deps import DB, AppSettings, CurrentUser
from app.routers import status_for_create
from app.schemas.receipts import ReceiptOut, ReceiptReadingOut, ReceiptWithReading
from app.schemas.transactions import (
    TransactionBulkResult,
    TransactionBulkUpdate,
    TransactionCreate,
    TransactionListQuery,
    TransactionOut,
    TransactionPage,
    TransactionSplit,
    TransactionSplitResult,
    TransactionUpdate,
)
from app.services import exports as exports_service
from app.services import receipt_reading
from app.services import receipts as receipts_service
from app.services import transactions as service

router = APIRouter()


@router.post("", status_code=status.HTTP_201_CREATED)
def create_transaction(
    body: TransactionCreate, response: Response, db: DB, user: CurrentUser
) -> TransactionOut:
    row, created = service.create_transaction(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return service.to_out(db, row)


@router.get("", response_model=TransactionPage)
def list_transactions(
    query: Annotated[TransactionListQuery, Query()], db: DB, user: CurrentUser
) -> TransactionPage | StreamingResponse:
    """Plain JSON page by default. `format=csv` streams every matching row (ignoring
    `cursor`/`limit` -- see `TransactionListQuery`) as a CSV file instead;
    `response_model` is pinned to `TransactionPage` since that's the only shape this
    returns as a plain object -- the CSV path returns a `StreamingResponse` directly,
    which FastAPI sends as-is without running it through that model.
    """
    now = datetime.now(UTC)
    if query.format != "csv":
        return service.list_transactions(db, user.id, query, now)

    rows = service.stream_export_rows(db, user.id, query, now)
    filename = f"tally-transactions-{query.month or 'all'}.csv"
    return StreamingResponse(
        exports_service.stream_transaction_csv(rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/bulk")
def bulk_update_transactions(
    body: TransactionBulkUpdate, db: DB, user: CurrentUser
) -> TransactionBulkResult:
    rows = service.bulk_update_transactions(db, user.id, body.ids, body.patch)
    db.commit()
    return TransactionBulkResult(updated=len(rows), items=service.to_out_many(db, rows))


@router.patch("/{id}")
def update_transaction(
    id: uuid.UUID, body: TransactionUpdate, db: DB, user: CurrentUser
) -> TransactionOut:
    row = service.update_transaction(db, user.id, id, body)
    db.commit()
    return service.to_out(db, row)


@router.post("/{id}/split")
def split_transaction(
    id: uuid.UUID, body: TransactionSplit, db: DB, user: CurrentUser
) -> TransactionSplitResult:
    parent, children = service.split_transaction(db, user.id, id, body)
    db.commit()
    outs = service.to_out_many(db, [parent, *children])
    return TransactionSplitResult(parent=outs[0], children=outs[1:])


@router.get("/{id}/receipts")
def list_receipts(id: uuid.UUID, db: DB, user: CurrentUser) -> list[ReceiptWithReading]:
    """The transaction's receipts with what Claude read off each (#170)."""
    return [
        ReceiptWithReading(
            **ReceiptOut.model_validate(receipt).model_dump(),
            reading=ReceiptReadingOut.model_validate(reading) if reading else None,
        )
        for receipt, reading in receipt_reading.receipts_for(db, user.id, id)
    ]


@router.post("/{id}/receipts", status_code=status.HTTP_201_CREATED)
def upload_receipt(
    id: uuid.UUID,
    response: Response,
    db: DB,
    settings: AppSettings,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    receipt_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ReceiptOut:
    row, created = receipts_service.upload_receipt(
        db,
        settings,
        user.id,
        id,
        file.file.read(receipts_service.MAX_UPLOAD_BYTES + 1),
        receipt_id,
        datetime.now(UTC),
    )
    db.commit()
    status_for_create(response, created)
    return ReceiptOut.model_validate(row)
