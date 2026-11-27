from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.devices import DeviceOut, DeviceRegister
from app.services import devices

router = APIRouter()


@router.post("", status_code=status.HTTP_201_CREATED)
def register_device(
    body: DeviceRegister, db: DB, user: CurrentUser, response: Response
) -> DeviceOut:
    device, created = devices.register_device(
        db, user.id, body.apns_token, body.environment, datetime.now(UTC)
    )
    db.commit()
    status_for_create(response, created)
    return DeviceOut.model_validate(device)
