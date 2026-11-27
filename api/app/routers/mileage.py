import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.config import APP_TIMEZONE
from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.mileage import (
    YEAR_MAX,
    YEAR_MIN,
    OdometerIn,
    OdometerOut,
    RateCreate,
    RateOut,
    RateUpdate,
    SavedTripCreate,
    SavedTripLog,
    SavedTripOut,
    SavedTripUpdate,
    TripCreate,
    TripOut,
    TripUpdate,
    Year,
)
from app.services import mileage as service

router = APIRouter()

YearPath = Annotated[int, Path(ge=YEAR_MIN, le=YEAR_MAX)]


@router.get("/trips")
def list_trips(
    db: DB, user: CurrentUser, year: Annotated[Year | None, Query()] = None
) -> list[TripOut]:
    return [TripOut.model_validate(row) for row in service.list_trips(db, user.id, year)]


@router.post("/trips", status_code=status.HTTP_201_CREATED)
def create_trip(body: TripCreate, response: Response, db: DB, user: CurrentUser) -> TripOut:
    row, created = service.create_trip(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return TripOut.model_validate(row)


@router.patch("/trips/{id}")
def update_trip(id: uuid.UUID, body: TripUpdate, db: DB, user: CurrentUser) -> TripOut:
    row = service.update_trip(db, user.id, id, body)
    db.commit()
    return TripOut.model_validate(row)


@router.delete("/trips/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    service.delete_trip(db, user.id, id)
    db.commit()


@router.get("/saved-trips")
def list_saved_trips(db: DB, user: CurrentUser) -> list[SavedTripOut]:
    return [SavedTripOut.model_validate(row) for row in service.list_saved_trips(db, user.id)]


@router.post("/saved-trips", status_code=status.HTTP_201_CREATED)
def create_saved_trip(
    body: SavedTripCreate, response: Response, db: DB, user: CurrentUser
) -> SavedTripOut:
    row, created = service.create_saved_trip(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return SavedTripOut.model_validate(row)


@router.patch("/saved-trips/{id}")
def update_saved_trip(
    id: uuid.UUID, body: SavedTripUpdate, db: DB, user: CurrentUser
) -> SavedTripOut:
    row = service.update_saved_trip(db, user.id, id, body)
    db.commit()
    return SavedTripOut.model_validate(row)


@router.delete("/saved-trips/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_trip(id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    service.delete_saved_trip(db, user.id, id)
    db.commit()


@router.post("/saved-trips/{id}/log", status_code=status.HTTP_201_CREATED)
def log_saved_trip(
    id: uuid.UUID,
    response: Response,
    db: DB,
    user: CurrentUser,
    body: SavedTripLog | None = None,
) -> TripOut:
    """Log the saved trip as a new trip dated today in the app's timezone."""
    today = datetime.now(UTC).astimezone(APP_TIMEZONE).date()
    trip_id = body.id if body else None
    row, created = service.log_saved_trip(db, user.id, id, today, trip_id)
    db.commit()
    status_for_create(response, created)
    return TripOut.model_validate(row)


@router.get("/odometer/{year}")
def get_odometer(year: YearPath, db: DB, user: CurrentUser) -> OdometerOut:
    return OdometerOut.model_validate(service.get_odometer(db, user.id, year))


@router.put("/odometer/{year}")
def put_odometer(year: YearPath, body: OdometerIn, db: DB, user: CurrentUser) -> OdometerOut:
    row = service.put_odometer(db, user.id, year, body)
    db.commit()
    return OdometerOut.model_validate(row)


@router.get("/rates")
def list_rates(db: DB, user: CurrentUser) -> list[RateOut]:
    return [RateOut.model_validate(row) for row in service.list_rates(db, user.id)]


@router.post("/rates", status_code=status.HTTP_201_CREATED)
def create_rate(body: RateCreate, response: Response, db: DB, user: CurrentUser) -> RateOut:
    row, created = service.create_rate(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return RateOut.model_validate(row)


@router.patch("/rates/{id}")
def update_rate(id: uuid.UUID, body: RateUpdate, db: DB, user: CurrentUser) -> RateOut:
    row = service.update_rate(db, user.id, id, body)
    db.commit()
    return RateOut.model_validate(row)


@router.delete("/rates/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rate(id: uuid.UUID, db: DB, user: CurrentUser) -> None:
    service.delete_rate(db, user.id, id)
    db.commit()
