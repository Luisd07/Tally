"""Device registration: upsert the phone's push token. See `app.services.apns`."""

import uuid
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Device
from app.models.enums import ApnsEnvironment


def register_device(
    db: Session,
    user_id: uuid.UUID,
    apns_token: str,
    environment: ApnsEnvironment,
    now: datetime,
) -> tuple[Device, bool]:
    """Upsert a device by `apns_token`, scoped to `user_id`.

    A token already in the table is reassigned to `user_id`: a device token belongs to
    the phone, and with one user this only happens across a reinstall. One atomic
    INSERT ... ON CONFLICT (apns_token) DO UPDATE ... RETURNING; `updated_at` is set
    explicitly, because the ORM's `onupdate` doesn't fire on a Core upsert. Returns
    `(row, created)`; `created` is `False` when an existing token was refreshed instead
    of inserted, told apart by comparing the returned `created_at` and `updated_at`:
    equal only when the insert branch ran, since the update branch leaves `created_at`
    untouched.
    """
    stmt = (
        insert(Device)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            apns_token=apns_token,
            environment=environment,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=["apns_token"],
            set_={
                "user_id": user_id,
                "environment": environment,
                "last_seen_at": now,
                "updated_at": now,
            },
        )
        .returning(Device)
    )
    # Without populate_existing, a conflict that lands on a row already in the
    # session's identity map (e.g. one loaded earlier in the same request) would come
    # back with its stale, pre-upsert attributes instead of what RETURNING just gave us.
    row = db.scalars(stmt, execution_options={"populate_existing": True}).one()
    return row, row.created_at == row.updated_at
