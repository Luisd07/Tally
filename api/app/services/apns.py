"""Apple Push Notification service client (HTTP/2, ES256 token auth).

See Apple's "Establishing a token-based connection to APNs" for the provider JWT shape
(header `kid`, claims `iss`/`iat`, ES256, refreshed every 20-60 minutes) and "Sending
notification requests to APNs" and "Handling notification responses from APNs" for the
request and response shapes: POST `/3/device/<token>` with
`apns-topic`/`apns-push-type`/`apns-priority`/`authorization` headers, a 200 with no
body on success, and on failure a JSON body `{"reason": ...}`
(developer.apple.com/documentation/usernotifications).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import jwt
from loguru import logger
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Device
from app.models.enums import ApnsEnvironment
from app.scoping import owned

ALGORITHM = "ES256"
# Apple requires the provider token be refreshed every 20-60 minutes; stay in the
# middle of that window.
REFRESH_INTERVAL = timedelta(minutes=50)
REQUEST_TIMEOUT = 10.0
# The only expired-provider-token reason worth retrying: InvalidProviderToken means the
# key itself is wrong, and a retry can't fix that.
RETRYABLE_REASON = "ExpiredProviderToken"

_HOSTS = {
    ApnsEnvironment.sandbox: "https://api.sandbox.push.apple.com",
    ApnsEnvironment.production: "https://api.push.apple.com",
}

# (status, reason) pairs Apple says never to retry because the token itself is dead.
_DEAD_TOKEN = {
    (410, "Unregistered"),
    (410, "ExpiredToken"),
    (400, "BadDeviceToken"),
    (400, "DeviceTokenNotForTopic"),
}


@dataclass
class _CachedToken:
    token: str
    minted_at: datetime


# Keyed by the settings that identify a signing key, so unrelated settings (tests, or a
# future key rotation) never share a cached token.
_cache: dict[tuple[str, str, str], _CachedToken] = {}


@dataclass
class _Push:
    """The constants one `send_alert` call shares across every device it pushes to."""

    client: httpx.Client
    settings: Settings
    now: datetime
    title: str
    body: str


def make_client() -> httpx.Client:
    """An HTTP/2 client for APNs.

    Apple's "Sending notification requests to APNs" requires HTTP/2; httpx only
    negotiates it when built with `http2=True`, so callers (the sync worker) must
    build their client here rather than with a bare `httpx.Client()`.
    """
    return httpx.Client(http2=True, timeout=REQUEST_TIMEOUT)


def get_provider_token(settings: Settings, now: datetime) -> str:
    """Return the cached ES256 provider JWT, minting a new one if there isn't one yet
    or the cached one is older than `REFRESH_INTERVAL`. Never logged: it is a bearer
    credential, same as the private key it is signed with.
    """
    key = (settings.apns_team_id, settings.apns_key_id, settings.apns_private_key_path)
    cached = _cache.get(key)
    if cached is not None and now - cached.minted_at < REFRESH_INTERVAL:
        return cached.token
    return mint_provider_token(settings, now)


def mint_provider_token(settings: Settings, now: datetime) -> str:
    """Sign and cache a fresh ES256 provider JWT, bypassing whatever is cached."""
    key = (settings.apns_team_id, settings.apns_key_id, settings.apns_private_key_path)
    private_key = Path(settings.apns_private_key_path).read_text()
    token = jwt.encode(
        {"iss": settings.apns_team_id, "iat": now},
        private_key,
        algorithm=ALGORITHM,
        headers={"kid": settings.apns_key_id},
    )
    _cache[key] = _CachedToken(token, now)
    return token


def _reason(response: httpx.Response) -> str:
    """The `reason` Apple's error body carries, or "" for a 200, a body that isn't
    JSON (an intermediary's error page) or JSON that isn't an object.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    return body.get("reason", "") if isinstance(body, dict) else ""


def _post(push: _Push, device: Device, token: str) -> httpx.Response | None:
    """POST one alert to `device`, or `None` if the request itself failed (logged)."""
    url = f"{_HOSTS[device.environment]}/3/device/{device.apns_token}"
    headers = {
        "apns-topic": push.settings.apns_bundle_id,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "authorization": f"bearer {token}",
    }
    payload = {"aps": {"alert": {"title": push.title, "body": push.body}}}
    try:
        return push.client.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("APNs request failed for device {}: {}", device.id, exc)
        return None


def _post_with_refresh(
    push: _Push, device: Device, token: str, refreshed: bool
) -> tuple[httpx.Response | None, str, bool]:
    """POST once; if APNs reports the token expired and this call hasn't refreshed
    yet, mint a fresh one and retry once. Returns the final response (or `None` on a
    transport error), the token now in use, and whether a refresh has happened.
    """
    response = _post(push, device, token)
    if response is None or refreshed or response.status_code != 403:
        return response, token, refreshed
    if _reason(response) != RETRYABLE_REASON:
        return response, token, refreshed
    try:
        token = mint_provider_token(push.settings, push.now)
    except (OSError, ValueError) as exc:
        logger.warning("APNs provider token unavailable: {}", type(exc).__name__)
        return response, token, True
    return _post(push, device, token), token, True


def _prune_if_dead(db: Session, device: Device, response: httpx.Response) -> None:
    """Delete `device` if Apple says its token is dead; otherwise log and move on."""
    if response.status_code == 200:
        return
    reason = _reason(response)
    if (response.status_code, reason) in _DEAD_TOKEN:
        db.delete(device)
        db.flush()
        return
    logger.warning("APNs push failed for device {}: {} {}", device.id, response.status_code, reason)


def send_alert(
    db: Session,
    user_id: uuid.UUID,
    title: str,
    body: str,
    now: datetime,
    client: httpx.Client,
    settings: Settings,
) -> None:
    """Push one alert to every device registered to `user_id`.

    A dead token (410 Unregistered/ExpiredToken, 400 BadDeviceToken/
    DeviceTokenNotForTopic) deletes the device (flushed, not committed: the caller
    commits together with the rest of its work). An expired provider token refreshes
    the signing token at most once for the whole call and retries that one device;
    any other failure, including an unreadable signing key, is logged and does not
    stop the remaining devices.
    """
    try:
        token = get_provider_token(settings, now)
    except (OSError, ValueError) as exc:
        logger.warning("APNs provider token unavailable: {}", type(exc).__name__)
        return

    push = _Push(client, settings, now, title, body)
    refreshed = False
    for device in db.scalars(owned(Device, user_id)):
        response, token, refreshed = _post_with_refresh(push, device, token, refreshed)
        if response is not None:
            _prune_if_dead(db, device, response)
