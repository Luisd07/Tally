from typing import Any

from app.schemas.base import Schema


class ErrorDetail(Schema):
    code: str
    message: str
    request_id: str | None = None
    details: list[dict[str, Any]] | None = None


class ErrorResponse(Schema):
    error: ErrorDetail
