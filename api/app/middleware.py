import re
import time
import uuid

from loguru import logger
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import error_body

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_QUIET_PATHS = {"/healthz"}


class RequestContextMiddleware:
    """Tags every log line with a request ID, writes one access line per request,
    and turns unhandled exceptions into a logged 500 in the standard error shape."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        status = 500
        started = False
        start = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        with logger.contextualize(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                logger.exception("Unhandled error")
                if started:
                    raise
                body = error_body("internal_error", "Something went wrong.", request_id)
                await JSONResponse(body, status_code=500)(scope, receive, send_with_id)
            finally:
                ms = (time.perf_counter() - start) * 1000
                level = "DEBUG" if scope["path"] in _QUIET_PATHS else "INFO"
                logger.log(level, "{} {} {} {:.1f}ms", scope["method"], scope["path"], status, ms)
