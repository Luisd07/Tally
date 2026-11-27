"""One error shape for every failure: {"error": {"code", "message", "request_id", "details"}}."""

from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.errors import ErrorDetail, ErrorResponse


class AppError(Exception):
    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    message: ClassVar[str] = "Something went wrong."
    # Not a ClassVar: a subclass may need headers computed per instance (e.g. Retry-After).
    headers: dict[str, str] | None = None

    def __init__(
        self, message: str | None = None, *, details: list[dict[str, Any]] | None = None
    ) -> None:
        self.detail = message or self.message
        self.details = details
        super().__init__(self.detail)


class NotFound(AppError):
    """Raise for records that don't exist *and* for records the user may not see."""

    status_code = 404
    code = "not_found"
    message = "Not found."


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    message = "Sign-in required."
    headers = {"WWW-Authenticate": "Bearer"}


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    message = "Conflicts with the current state."


class BadGateway(AppError):
    """Raise when an upstream service (SimpleFIN) fails or times out."""

    status_code = 502
    code = "bad_gateway"
    message = "The upstream service did not respond."


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "File is too large."


class UnsupportedMediaType(AppError):
    status_code = 415
    code = "unsupported_media_type"
    message = "Unsupported file type."


class UnprocessableEntity(AppError):
    """Raise for a business-rule violation pydantic alone can't see, such as a partial
    update whose merged result would fail a DB CHECK. Pass `details` in the same
    `{type, loc, msg}` item shape FastAPI's own request-validation 422 uses, so the
    body is indistinguishable from that path.
    """

    status_code = 422
    code = "validation_error"
    message = "Invalid request."

    @classmethod
    def value_error(cls, msg: str) -> UnprocessableEntity:
        """A single `value_error` detail at `body`, matching the shape pydantic's own
        `ValueError`-raising validators produce, for a rule checked outside the schema
        (a merged PATCH result that would fail a DB CHECK).
        """
        return cls(details=[{"type": "value_error", "loc": ["body"], "msg": f"Value error, {msg}"}])


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    429: "rate_limited",
}


def error_body(
    code: str,
    message: str,
    request_id: str | None,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    detail = ErrorDetail(code=code, message=message, request_id=request_id, details=details)
    return ErrorResponse(error=detail).model_dump(exclude_none=True)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return JSONResponse(
        error_body(exc.code, exc.detail, _request_id(request), exc.details),
        status_code=exc.status_code,
        headers=exc.headers,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_CODES.get(exc.status_code, "error")
    return JSONResponse(
        error_body(code, str(exc.detail), _request_id(request)),
        status_code=exc.status_code,
        headers=exc.headers,
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [{k: v for k, v in err.items() if k not in ("input", "ctx")} for err in exc.errors()]
    return JSONResponse(
        error_body(
            "validation_error", "Invalid request.", _request_id(request), jsonable_encoder(details)
        ),
        status_code=422,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)


class ServiceUnavailable(AppError):
    """Raise when an optional feature is off or out of budget (AI with no key or past its cap)."""

    status_code = 503
    code = "service_unavailable"
    message = "This feature isn't available right now."
