from datetime import datetime

from app.schemas.base import Schema


class BackupStatus(Schema):
    last_success_at: datetime | None
    snapshot_id: str | None
