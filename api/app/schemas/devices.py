import uuid
from datetime import datetime

from pydantic import ConfigDict

from app.models.enums import ApnsEnvironment
from app.schemas.base import Schema
from app.schemas.types import ApnsToken


class DeviceRegister(Schema):
    """Register or refresh the phone's push token."""

    model_config = ConfigDict(extra="forbid")

    apns_token: ApnsToken
    environment: ApnsEnvironment


class DeviceOut(Schema):
    id: uuid.UUID
    apns_token: str
    environment: ApnsEnvironment
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime
