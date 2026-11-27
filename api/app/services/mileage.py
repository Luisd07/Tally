"""Mileage log CRUD (#167): business trips, saved trip templates, yearly odometer
readings and the per-user rate table, plus `rate_in_force`, which picks the rate a
trip is priced at, and `summarize_mileage`, the year's totals that the tax summary
and the tax export both show. See `app.scoping` for the user-scoping helpers.
"""

import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import ColumnElement
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.errors import NotFound
from app.models import MileageRate, MileageTrip, OdometerReading, SavedTrip
from app.schemas.mileage import (
    OdometerIn,
    RateCreate,
    RateUpdate,
    SavedTripCreate,
    SavedTripUpdate,
    TripCreate,
    TripUpdate,
)
from app.schemas.tax import MileageRateUsed
from app.scoping import get_owned, owned
from app.services.crud import apply_patch, create_idempotent

#: Zero miles, at the one decimal place miles are stored to (money's zero is `_ZERO`).
ZERO_MILES = Decimal("0.0")
_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class PricedTrip:
    """A trip and the rate in force on its date. `amount` is `miles * rate`, not
    quantized, so a year's total can be rounded once; `0` when no rate is in force.
    """

    trip: MileageTrip
    rate: MileageRate | None
    amount: Decimal


@dataclass(frozen=True)
class MileageTotals:
    """A year's priced trips, totalled. `unpriced_miles` fell on `unpriced_dates`
    (sorted, distinct), when no rate was in force. The odometer fields are `None`
    until the year's reading is set; `total_miles` also needs the end reading.
    """

    business_miles: Decimal
    mileage_amount: Decimal
    unpriced_miles: Decimal
    unpriced_dates: list[dt.date]
    rates: list[MileageRateUsed]
    odometer_start: Decimal | None
    odometer_end: Decimal | None
    total_miles: Decimal | None


_RATE_CONFLICTS = {
    "uq_mileage_rates_user_id_effective_from": "A rate already starts on this date.",
}


def rate_in_force(rates: Iterable[MileageRate], on: dt.date) -> MileageRate | None:
    """The rate with the latest `effective_from` on or before `on`, or `None` when
    every rate starts after it (or there are none).
    """
    in_force = [rate for rate in rates if rate.effective_from <= on]
    return max(in_force, key=lambda rate: rate.effective_from, default=None)


def _in_year(year: int) -> tuple[ColumnElement[bool], ...]:
    return (MileageTrip.date >= dt.date(year, 1, 1), MileageTrip.date < dt.date(year + 1, 1, 1))


def priced_trips(db: Session, user_id: uuid.UUID, year: int) -> list[PricedTrip]:
    """`year`'s trips oldest first, each priced at the rate in force on its date."""
    rates = list_rates(db, user_id)
    stmt = (
        owned(MileageTrip, user_id)
        .where(*_in_year(year))
        .order_by(MileageTrip.date.asc(), MileageTrip.created_at.asc(), MileageTrip.id.asc())
    )
    priced: list[PricedTrip] = []
    for trip in db.scalars(stmt):
        rate = rate_in_force(rates, trip.date)
        amount = trip.miles * rate.rate if rate else Decimal(0)
        priced.append(PricedTrip(trip=trip, rate=rate, amount=amount))
    return priced


def _mileage_amount(priced: list[PricedTrip]) -> Decimal:
    """Each trip's unrounded `miles * rate`, summed, then quantized once
    (`ROUND_HALF_EVEN`, to the cent) like the tax summary's line roll-up.
    """
    return sum((p.amount for p in priced), _ZERO).quantize(_CENT, rounding=ROUND_HALF_EVEN)


def _rates_used(priced: list[PricedTrip]) -> list[MileageRateUsed]:
    """The rates the priced trips used, oldest first, with the miles priced at each."""
    miles_by_rate: dict[tuple[dt.date, Decimal], Decimal] = {}
    for p in priced:
        if p.rate is not None:
            key = (p.rate.effective_from, p.rate.rate)
            miles_by_rate[key] = miles_by_rate.get(key, ZERO_MILES) + p.trip.miles
    return [
        MileageRateUsed(effective_from=effective_from, rate=rate, miles=miles)
        for (effective_from, rate), miles in sorted(miles_by_rate.items())
    ]


def summarize_mileage(priced: list[PricedTrip], odometer: OdometerReading | None) -> MileageTotals:
    """The year's totals from its priced trips and odometer reading: the one source
    for both the tax summary's line 9 and the tax export's mileage CSV.
    """
    unpriced = [p.trip for p in priced if p.rate is None]
    start = odometer.start_reading if odometer else None
    end = odometer.end_reading if odometer else None
    return MileageTotals(
        business_miles=sum((p.trip.miles for p in priced), ZERO_MILES),
        mileage_amount=_mileage_amount(priced),
        unpriced_miles=sum((trip.miles for trip in unpriced), ZERO_MILES),
        unpriced_dates=sorted({trip.date for trip in unpriced}),
        rates=_rates_used(priced),
        odometer_start=start,
        odometer_end=end,
        total_miles=end - start if start is not None and end is not None else None,
    )


def list_trips(db: Session, user_id: uuid.UUID, year: int | None) -> list[MileageTrip]:
    """Newest first; only `year`'s trips when it's given."""
    stmt = owned(MileageTrip, user_id)
    if year is not None:
        stmt = stmt.where(*_in_year(year))
    stmt = stmt.order_by(
        MileageTrip.date.desc(), MileageTrip.created_at.desc(), MileageTrip.id.desc()
    )
    return list(db.scalars(stmt))


def create_trip(db: Session, user_id: uuid.UUID, body: TripCreate) -> tuple[MileageTrip, bool]:
    """Idempotent create: a repeated `id` returns the existing row, ignoring the new body."""
    return create_idempotent(db, MileageTrip, user_id, body.model_dump())


def update_trip(db: Session, user_id: uuid.UUID, id: uuid.UUID, body: TripUpdate) -> MileageTrip:
    return apply_patch(db, get_owned(db, MileageTrip, id, user_id), body)


def delete_trip(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> None:
    db.delete(get_owned(db, MileageTrip, id, user_id))
    db.flush()


def list_saved_trips(db: Session, user_id: uuid.UUID) -> list[SavedTrip]:
    stmt = owned(SavedTrip, user_id).order_by(SavedTrip.name.asc(), SavedTrip.id.asc())
    return list(db.scalars(stmt))


def create_saved_trip(
    db: Session, user_id: uuid.UUID, body: SavedTripCreate
) -> tuple[SavedTrip, bool]:
    """Idempotent create: a repeated `id` returns the existing row, ignoring the new body."""
    return create_idempotent(db, SavedTrip, user_id, body.model_dump())


def update_saved_trip(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, body: SavedTripUpdate
) -> SavedTrip:
    return apply_patch(db, get_owned(db, SavedTrip, id, user_id), body)


def delete_saved_trip(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> None:
    db.delete(get_owned(db, SavedTrip, id, user_id))
    db.flush()


def log_saved_trip(
    db: Session, user_id: uuid.UUID, id: uuid.UUID, today: dt.date, trip_id: uuid.UUID | None
) -> tuple[MileageTrip, bool]:
    """A new trip dated `today`, copied from the saved trip's miles, purpose and places.
    Idempotent on `trip_id` like `create_trip`: a repeated id returns the existing trip.
    """
    saved = get_owned(db, SavedTrip, id, user_id)
    values = {
        "id": trip_id,
        "date": today,
        "miles": saved.miles,
        "purpose": saved.purpose,
        "start_location": saved.start_location,
        "end_location": saved.end_location,
    }
    return create_idempotent(db, MileageTrip, user_id, values)


def get_odometer(db: Session, user_id: uuid.UUID, year: int) -> OdometerReading:
    """Raises `NotFound` when no reading is stored for `year`."""
    stmt = owned(OdometerReading, user_id).where(OdometerReading.year == year)
    row = db.scalars(stmt).one_or_none()
    if row is None:
        raise NotFound()
    return row


def find_odometer(db: Session, user_id: uuid.UUID, year: int) -> OdometerReading | None:
    stmt = owned(OdometerReading, user_id).where(OdometerReading.year == year)
    return db.scalars(stmt).one_or_none()


def put_odometer(db: Session, user_id: uuid.UUID, year: int, body: OdometerIn) -> OdometerReading:
    """Insert or replace `year`'s reading in one statement."""
    values = body.model_dump()
    stmt = (
        insert(OdometerReading)
        .values(id=uuid.uuid4(), user_id=user_id, year=year, **values)
        .on_conflict_do_update(index_elements=["user_id", "year"], set_=values)
        .returning(OdometerReading)
        .execution_options(populate_existing=True)
    )
    return db.scalars(stmt).one()


def list_rates(db: Session, user_id: uuid.UUID) -> list[MileageRate]:
    stmt = owned(MileageRate, user_id).order_by(MileageRate.effective_from.asc())
    return list(db.scalars(stmt))


def create_rate(db: Session, user_id: uuid.UUID, body: RateCreate) -> tuple[MileageRate, bool]:
    """Idempotent create; `Conflict` when another of the user's rates starts on the
    same `effective_from`.
    """
    return create_idempotent(
        db, MileageRate, user_id, body.model_dump(), unique_conflicts=_RATE_CONFLICTS
    )


def update_rate(db: Session, user_id: uuid.UUID, id: uuid.UUID, body: RateUpdate) -> MileageRate:
    row = get_owned(db, MileageRate, id, user_id)
    return apply_patch(db, row, body, unique_conflicts=_RATE_CONFLICTS)


def delete_rate(db: Session, user_id: uuid.UUID, id: uuid.UUID) -> None:
    db.delete(get_owned(db, MileageRate, id, user_id))
    db.flush()
