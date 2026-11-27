from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.deps import DB, AppSettings, CurrentUser
from app.schemas.tax import (
    MissingReceiptListQuery,
    MissingReceiptPage,
    TaxSummaryOut,
    TaxSummaryQuery,
)
from app.services import tax as service
from app.services import tax_export as export_service

router = APIRouter()


@router.get("/missing-receipts")
def list_missing_receipts(
    query: Annotated[MissingReceiptListQuery, Query()], db: DB, user: CurrentUser
) -> MissingReceiptPage:
    return service.list_missing_receipts(db, user.id, query)


@router.get("/summary")
def get_tax_summary(
    query: Annotated[TaxSummaryQuery, Query()], db: DB, user: CurrentUser, settings: AppSettings
) -> TaxSummaryOut:
    return service.get_tax_summary(db, user.id, query, settings)


@router.get("/export")
def export_tax_year(
    query: Annotated[TaxSummaryQuery, Query()], db: DB, user: CurrentUser, settings: AppSettings
) -> StreamingResponse:
    """The year's business transactions as a CSV plus their receipt files, as a zip."""
    plan = export_service.plan_export(db, user.id, query.year, settings)
    return StreamingResponse(
        export_service.stream_zip(plan),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="tally-tax-{query.year}.zip"',
            "Cache-Control": "no-store",
        },
    )
