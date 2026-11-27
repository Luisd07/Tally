import uuid
from datetime import UTC, datetime

from fastapi import APIRouter

from app.deps import DB, CurrentUser
from app.schemas.insights import InsightOut
from app.services import insights as service

router = APIRouter()


@router.get("")
def list_insights(db: DB, user: CurrentUser) -> list[InsightOut]:
    """Undismissed insights from the last 14 days, newest first, for Home (#225). An action
    past its expiry is left off (`action_id` and `action_status` null): it can't be
    confirmed any more, so the card offers only Dismiss."""
    return [
        InsightOut.model_validate(insight).model_copy(
            update={
                "action_id": insight.action_id if status is not None else None,
                "action_status": status,
            }
        )
        for insight, status in service.recent(db, user.id, datetime.now(UTC))
    ]


@router.post("/{id}/dismiss")
def dismiss_insight(id: uuid.UUID, db: DB, user: CurrentUser) -> InsightOut:
    """Hide an insight from Home. Its proposed action, if any, is left as it is."""
    insight = service.dismiss(db, user.id, id, datetime.now(UTC))
    db.commit()
    return InsightOut.model_validate(insight)
