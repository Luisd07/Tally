import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.subscriptions import (
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionPage,
    SubscriptionSuggestion,
    SubscriptionUpdate,
)
from app.services import subscription_detection
from app.services import subscriptions as service

router = APIRouter()


@router.get("")
def list_subscriptions(
    db: DB, user: CurrentUser, include_inactive: bool = False
) -> SubscriptionPage:
    return service.list_subscriptions(db, user.id, include_inactive, datetime.now(UTC))


# Declared before any `/{id}` route so "suggestions" is never parsed as an id.
@router.get("/suggestions")
def list_suggestions(db: DB, user: CurrentUser) -> list[SubscriptionSuggestion]:
    return subscription_detection.suggest_subscriptions(db, user.id, datetime.now(UTC))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_subscription(
    body: SubscriptionCreate, response: Response, db: DB, user: CurrentUser
) -> SubscriptionOut:
    row, created = service.create_subscription(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return service.subscription_out(db, user.id, row, datetime.now(UTC))


@router.patch("/{id}")
def update_subscription(
    id: uuid.UUID, body: SubscriptionUpdate, db: DB, user: CurrentUser
) -> SubscriptionOut:
    row = service.update_subscription(db, user.id, id, body)
    db.commit()
    return service.subscription_out(db, user.id, row, datetime.now(UTC))
