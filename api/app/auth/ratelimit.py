"""Rate limits, backed by `limits`' in-process moving-window counter: per client IP on the
auth routes, per user on the routes that call Claude.

`MemoryStorage` is safe because the API runs as a single uvicorn worker (see
`docker-compose.yml`): there's exactly one process to share counters across.
"""

import math
import time
from collections.abc import Callable

from fastapi import Request
from limits import RateLimitItem, RateLimitItemPerDay, RateLimitItemPerHour, RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

from app.deps import CurrentUser
from app.errors import AppError
from app.net import client_ip

storage = MemoryStorage()
limiter = MovingWindowRateLimiter(storage)

# A missing/invalid client IP (no proxy, or an unparseable X-Forwarded-For) shares one bucket.
UNKNOWN_CLIENT_KEY = "unknown"

ENROLL_LIMITS: tuple[RateLimitItem, ...] = (RateLimitItemPerMinute(5), RateLimitItemPerHour(20))
SIGNIN_LIMITS: tuple[RateLimitItem, ...] = (RateLimitItemPerMinute(10), RateLimitItemPerHour(60))
REFRESH_LIMITS: tuple[RateLimitItem, ...] = (RateLimitItemPerMinute(30),)
SIGNOUT_LIMITS: tuple[RateLimitItem, ...] = (RateLimitItemPerMinute(30),)
# Per user, so a stolen token used from a new IP doesn't get a fresh bucket.
AI_LIMITS: tuple[RateLimitItem, ...] = (RateLimitItemPerMinute(10), RateLimitItemPerHour(60))
TAX_REVIEW_LIMITS: tuple[RateLimitItem, ...] = (RateLimitItemPerHour(3), RateLimitItemPerDay(6))


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests."

    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.headers = {"Retry-After": str(retry_after)}


def rate_limit(namespace: str, *items: RateLimitItem) -> Callable[[Request], None]:
    """Build a dependency that enforces every limit in `items` for `namespace`, per client IP."""

    def dependency(request: Request) -> None:
        _hit(namespace, client_ip(request) or UNKNOWN_CLIENT_KEY, items)

    return dependency


def user_rate_limit(namespace: str, *items: RateLimitItem) -> Callable[[CurrentUser], None]:
    """Build a dependency that enforces every limit in `items` for `namespace`, per signed-in
    user. It depends on `CurrentUser`, so a request without a valid token gets 401 first."""

    def dependency(user: CurrentUser) -> None:
        _hit(namespace, str(user.id), items)

    return dependency


def _hit(namespace: str, key: str, items: tuple[RateLimitItem, ...]) -> None:
    for item in items:
        if not limiter.hit(item, namespace, key):
            stats = limiter.get_window_stats(item, namespace, key)
            retry_after = max(1, math.ceil(stats.reset_time - time.time()))
            raise RateLimited(retry_after)


enroll_limit = rate_limit("enroll", *ENROLL_LIMITS)
signin_limit = rate_limit("signin", *SIGNIN_LIMITS)
refresh_limit = rate_limit("refresh", *REFRESH_LIMITS)
signout_limit = rate_limit("signout", *SIGNOUT_LIMITS)
ai_limit = user_rate_limit("ai", *AI_LIMITS)
tax_review_limit = user_rate_limit("tax-review", *TAX_REVIEW_LIMITS)
