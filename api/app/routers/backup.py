from fastapi import APIRouter

from app.deps import AppSettings
from app.schemas.backup import BackupStatus
from app.services import backup as service

router = APIRouter()


@router.get("/status")
def backup_status(settings: AppSettings) -> BackupStatus:
    """Last successful nightly backup, for the Settings screen (#38)."""
    return service.read_backup_status(settings)
