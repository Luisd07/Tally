import uuid
from datetime import date, datetime

from app.models.enums import AIActionStatus, InsightKind
from app.schemas.base import Schema


class InsightOut(Schema):
    id: uuid.UUID
    kind: InsightKind
    title: str
    message: str
    day: date
    category_id: uuid.UUID | None
    transaction_id: uuid.UUID | None
    subscription_id: uuid.UUID | None
    action_id: uuid.UUID | None
    action_status: AIActionStatus | None = None
    dismissed_at: datetime | None
    created_at: datetime
