"""Loguru setup. Standard-library logging (uvicorn, SQLAlchemy, APScheduler) is routed into it."""

import inspect
import logging
import sys
from types import FrameType

from loguru import logger

from app.config import Settings

DEV_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<cyan>{extra[request_id]}</cyan> | {name}:{line} - <level>{message}</level>"
)


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk out of the logging module so the record points at the real caller.
        frame: FrameType | None = inspect.currentframe()
        depth = 0
        while frame is not None:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(settings: Settings) -> None:
    logger.remove()
    logger.configure(extra={"request_id": "-"})
    if settings.is_production:
        # JSON lines for Cloud Logging. diagnose=False keeps variable values
        # (and so secrets) out of tracebacks.
        logger.add(sys.stderr, level=settings.log_level, serialize=True, diagnose=False)
    else:
        logger.add(sys.stderr, level=settings.log_level, format=DEV_FORMAT, diagnose=True)

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "sqlalchemy", "apscheduler"):
        std = logging.getLogger(name)
        std.handlers = [InterceptHandler()]
        std.propagate = False
    # The request middleware writes one access line per request instead.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs "HTTP Request: <method> <url> ..." at INFO, and the URL in that line
    # is the SimpleFIN claim URL: the decoded setup token itself. WARNING keeps the
    # request/response line out of every log, in every environment.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The Anthropic SDK (and its httpx2 transport) log full request options at DEBUG:
    # prompts, receipt images and financial text. Keep them out of every log.
    for name in ("anthropic", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
