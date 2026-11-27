import uuid
from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from app.deps import DB, CurrentUser
from app.schemas.imports import ImportMappingIn, ImportMappingOut, ImportResult
from app.services import imports as service

router = APIRouter()


def _read(file: UploadFile) -> bytes:
    # One byte past the cap, so the service can reject an oversize file without reading it all.
    return file.file.read(service.MAX_FILE_BYTES + 1)


@router.put("/mappings/{account_id}")
def put_mapping(
    account_id: uuid.UUID, body: ImportMappingIn, db: DB, user: CurrentUser
) -> ImportMappingOut:
    row = service.put_mapping(db, user.id, account_id, body)
    db.commit()
    return ImportMappingOut.model_validate(row)


@router.get("/mappings/{account_id}")
def get_mapping(account_id: uuid.UUID, db: DB, user: CurrentUser) -> ImportMappingOut:
    return ImportMappingOut.model_validate(service.get_mapping(db, user.id, account_id))


@router.post("/csv")
def import_csv(
    account_id: uuid.UUID, file: Annotated[UploadFile, File()], db: DB, user: CurrentUser
) -> ImportResult:
    result = service.import_csv(db, user.id, account_id, _read(file))
    db.commit()
    return result


@router.post("/ofx")
def import_ofx(
    account_id: uuid.UUID, file: Annotated[UploadFile, File()], db: DB, user: CurrentUser
) -> ImportResult:
    result = service.import_ofx(db, user.id, account_id, _read(file))
    db.commit()
    return result
