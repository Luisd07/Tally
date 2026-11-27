from typing import Literal

from app.schemas.base import Schema


class Health(Schema):
    status: Literal["ok"]
