import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from app.schemas.base import PatchSchema, Schema
from app.schemas.types import Name100, Text200

# mileage_trips.miles / saved_trips.miles: NUMERIC(8,1) CHECK > 0.
Miles = Annotated[Decimal, Field(max_digits=8, decimal_places=1, gt=0)]
# odometer_readings.start_reading / end_reading: NUMERIC(8,1) CHECK >= 0.
OdometerMiles = Annotated[Decimal, Field(max_digits=8, decimal_places=1, ge=0)]
# mileage_rates.rate: dollars per mile, NUMERIC(6,4) CHECK > 0.
Rate = Annotated[Decimal, Field(max_digits=6, decimal_places=4, gt=0)]
# The IRS log needs a business purpose for every trip; whitespace alone isn't one.
Purpose = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
YEAR_MIN, YEAR_MAX = 1900, 2100
Year = Annotated[int, Field(ge=YEAR_MIN, le=YEAR_MAX)]


class TripCreate(Schema):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    date: dt.date
    miles: Miles
    purpose: Purpose
    start_location: Text200 | None = None
    end_location: Text200 | None = None


class TripUpdate(PatchSchema):
    """Partial update; only fields present in the request body are applied."""

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"date", "miles", "purpose"})

    date: dt.date | None = None
    miles: Miles | None = None
    purpose: Purpose | None = None
    start_location: Text200 | None = None
    end_location: Text200 | None = None


class TripOut(Schema):
    id: uuid.UUID
    date: dt.date
    miles: Decimal
    purpose: str
    start_location: str | None
    end_location: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class SavedTripCreate(Schema):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    name: Name100
    miles: Miles
    purpose: Purpose
    start_location: Text200 | None = None
    end_location: Text200 | None = None


class SavedTripUpdate(PatchSchema):
    """Partial update; only fields present in the request body are applied."""

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"name", "miles", "purpose"})

    name: Name100 | None = None
    miles: Miles | None = None
    purpose: Purpose | None = None
    start_location: Text200 | None = None
    end_location: Text200 | None = None


class SavedTripOut(Schema):
    id: uuid.UUID
    name: str
    miles: Decimal
    purpose: str
    start_location: str | None
    end_location: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class SavedTripLog(Schema):
    """Optional body for logging a saved trip; a client-made `id` makes a retried
    tap return the trip it already logged instead of a second one.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None


class OdometerIn(Schema):
    """The whole reading for a year; PUT replaces whatever was stored."""

    model_config = ConfigDict(extra="forbid")

    start_reading: OdometerMiles
    end_reading: OdometerMiles | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end_reading is not None and self.end_reading < self.start_reading:
            raise ValueError("end_reading must be at least start_reading")
        return self


class OdometerOut(Schema):
    year: int
    start_reading: Decimal
    end_reading: Decimal | None
    updated_at: dt.datetime


class RateCreate(Schema):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    effective_from: dt.date
    rate: Rate


class RateUpdate(PatchSchema):
    """Partial update; only fields present in the request body are applied."""

    model_config = ConfigDict(extra="forbid")

    _required = frozenset({"effective_from", "rate"})

    effective_from: dt.date | None = None
    rate: Rate | None = None


class RateOut(Schema):
    id: uuid.UUID
    effective_from: dt.date
    rate: Decimal
    created_at: dt.datetime
    updated_at: dt.datetime
