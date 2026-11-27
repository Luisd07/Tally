import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.events import EventCreate, EventListItem, EventOut, EventUpdate
from app.services import events

router = APIRouter()


@router.get("")
def list_events(
    db: DB, user: CurrentUser, year: Annotated[int, Query(ge=1900, le=2100)] | None = None
) -> list[EventListItem]:
    return events.list_events(db, user.id, year)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_event(body: EventCreate, db: DB, user: CurrentUser, response: Response) -> EventOut:
    event, created = events.create_event(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return EventOut.model_validate(event)


@router.patch("/{id}")
def update_event(id: uuid.UUID, body: EventUpdate, db: DB, user: CurrentUser) -> EventOut:
    event = events.update_event(db, user.id, id, body)
    db.commit()
    return EventOut.model_validate(event)
