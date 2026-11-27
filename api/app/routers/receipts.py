import uuid

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.deps import DB, AppSettings, CurrentUser
from app.services import receipts as service

router = APIRouter()


@router.get("/{id}/file")
def get_receipt_file(
    id: uuid.UUID, db: DB, settings: AppSettings, user: CurrentUser
) -> FileResponse:
    path = service.resolve_receipt_file(db, settings, user.id, id)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"}
    )
