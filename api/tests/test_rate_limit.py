"""Per-IP rate limits on the auth routes (issue #21)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.ratelimit import ENROLL_LIMITS, REFRESH_LIMITS, SIGNIN_LIMITS, SIGNOUT_LIMITS

ENROLL_OPTIONS = "/api/auth/enroll/options"
ENROLL_VERIFY = "/api/auth/enroll/verify"
SIGNIN_OPTIONS = "/api/auth/signin/options"
SIGNIN_VERIFY = "/api/auth/signin/verify"
REFRESH = "/api/auth/refresh"
SIGNOUT = "/api/auth/signout"

# (path, body, per-minute limit). Bodies are minimal/invalid: the rate-limit dependency
# runs before body validation, so a 429 must show up even for a request that would
# otherwise fail with a 422.
LIMITED_ROUTES = [
    (ENROLL_OPTIONS, {"code": "AAAA-BBBB-CCCC"}, ENROLL_LIMITS[0].amount),
    (ENROLL_VERIFY, {}, ENROLL_LIMITS[0].amount),
    (SIGNIN_OPTIONS, None, SIGNIN_LIMITS[0].amount),
    (SIGNIN_VERIFY, {}, SIGNIN_LIMITS[0].amount),
    (REFRESH, {"refresh_token": "not-a-real-token"}, REFRESH_LIMITS[0].amount),
    (SIGNOUT, {"refresh_token": "not-a-real-token"}, SIGNOUT_LIMITS[0].amount),
]


@pytest.mark.parametrize(("path", "body", "per_minute_limit"), LIMITED_ROUTES)
def test_hammering_a_limited_route_returns_429_in_the_standard_shape(
    db_client: TestClient, path: str, body: dict | None, per_minute_limit: int
) -> None:
    for _ in range(per_minute_limit):
        r = db_client.post(path, json=body)
        assert r.status_code != 429, r.text

    r = db_client.post(path, json=body)
    assert r.status_code == 429
    error = r.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["message"] == "Too many requests."
    assert error["request_id"] == r.headers["X-Request-ID"]
    retry_after = int(r.headers["Retry-After"])
    assert 0 < retry_after <= 60


SHARED_BUCKETS = [
    (ENROLL_OPTIONS, {"code": "AAAA-BBBB-CCCC"}, ENROLL_VERIFY, {}, ENROLL_LIMITS[0].amount),
    (SIGNIN_OPTIONS, None, SIGNIN_VERIFY, {}, SIGNIN_LIMITS[0].amount),
]


@pytest.mark.parametrize(
    ("options_path", "options_body", "verify_path", "verify_body", "per_minute_limit"),
    SHARED_BUCKETS,
)
def test_options_and_verify_share_one_bucket_per_group(
    db_client: TestClient,
    options_path: str,
    options_body: dict | None,
    verify_path: str,
    verify_body: dict,
    per_minute_limit: int,
) -> None:
    for _ in range(per_minute_limit):
        r = db_client.post(options_path, json=options_body)
        assert r.status_code != 429, r.text

    # The bucket is already full; the sibling route in the same group is refused too.
    r = db_client.post(verify_path, json=verify_body)
    assert r.status_code == 429


def test_rate_limit_is_per_ip(db_client: TestClient, app: FastAPI) -> None:
    """`db_client` only exists to install the `get_db` override before `app` serves a request."""
    with (
        TestClient(app, client=("203.0.113.1", 1234)) as first,
        TestClient(app, client=("203.0.113.2", 1234)) as second,
    ):
        for _ in range(10):
            assert first.post(SIGNIN_OPTIONS).status_code == 200
        assert first.post(SIGNIN_OPTIONS).status_code == 429

        # A different IP has its own, untouched bucket.
        assert second.post(SIGNIN_OPTIONS).status_code == 200
