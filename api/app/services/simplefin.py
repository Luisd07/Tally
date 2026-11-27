"""SimpleFIN Bridge client: claim a setup token, fetch /accounts.

Every exception raised here is deliberately plain text, free of the claim URL,
the setup token and the Access URL's credentials, so a caller can log or surface
it without leaking either secret.
"""

import base64
import binascii
import re
import socket
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from typing import Any, NamedTuple
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import SimplefinConnection

# SimpleFIN answers from its cache in a second or two, but when it pulls fresh data from the
# bank it can take far longer; at 10s that request failed, so a sync only ever saw stale data.
# 45s stays inside the app's 60s request limit for a manual "Sync now".
TIMEOUT = httpx.Timeout(45.0, connect=10.0)

# Legacy inet_aton shorthand (`127.1`, `2130706433`, `0x7f000001`, octal
# `017700000001`): some resolvers still accept these as IP literals even though
# `ipaddress.ip_address` doesn't recognize them, so they must be refused outright
# rather than treated as an ordinary DNS name.
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-fA-F]+|[0-9]+)(\.(0x[0-9a-fA-F]+|[0-9]+)){0,3}$")

# NAT64: an IPv6 address in this block maps straight onto an IPv4 address (here,
# straight onto whatever private or metadata address followed ::), so it needs its
# own check; `is_global` alone doesn't know about the mapping.
_NAT64 = IPv6Network("64:ff9b::/96")


class InvalidSetupToken(Exception):
    """The setup token isn't valid base64, or doesn't decode to a public https URL."""


class ClaimRejected(Exception):
    """SimpleFIN returned 403: the token was already claimed, or never existed.

    Per the protocol's Required checklist, the caller should tell the user the
    token may be compromised and let them disable it in SimpleFIN Bridge.
    """


class SimplefinRequestFailed(Exception):
    """A network error, timeout, or unexpected response from SimpleFIN."""


def _is_global_address(ip: IPv4Address | IPv6Address) -> bool:
    return ip.is_global and ip not in _NAT64


def _vetted_addresses(host: str) -> list[IPv4Address | IPv6Address] | None:
    """Resolve `host` and return its public addresses, IPv4 first, or None if unsafe.

    Refuses a numeric-shorthand IP literal before ever resolving. Otherwise
    resolves via `socket.getaddrinfo` (a real network name could still answer with
    a private address) and requires every returned address to be global and
    outside the NAT64 range. The caller connects to the returned addresses
    themselves, so the connection can't be re-resolved to something else after this
    check. A resolver failure raises `SimplefinRequestFailed`, distinct from an
    unsafe answer. IPv4 comes first because a host without IPv6 egress would
    otherwise fail on its first attempt.
    """
    if _NUMERIC_HOST.match(host):
        return None
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except OSError:
        raise SimplefinRequestFailed(f"Could not resolve SimpleFIN host {host}.") from None
    addresses = [ip_address(entry[4][0]) for entry in addrinfo]
    if not addresses or not all(_is_global_address(a) for a in addresses):
        return None
    return sorted(dict.fromkeys(addresses), key=lambda a: a.version)


class PinnedRequest(NamedTuple):
    """A request target rewritten to a vetted IP, keeping the real hostname for the
    Host header, TLS SNI and certificate verification.
    """

    url: str
    headers: dict[str, str]
    extensions: dict[str, str]


def _pin(url: str) -> list[PinnedRequest] | None:
    """Rewrite `url` to connect to each of its vetted IPs, or None if it isn't safe.

    Safe means https, a hostname, a numeric port if any, and public addresses only.
    """
    u = urlsplit(url)
    host = u.hostname
    if host is None or u.scheme != "https":
        return None
    try:
        port = f":{u.port}" if u.port else ""
    except ValueError:
        return None
    addresses = _vetted_addresses(host)
    if addresses is None:
        return None
    return [
        PinnedRequest(
            u._replace(netloc=f"[{a}]{port}" if a.version == 6 else f"{a}{port}").geturl(),
            {"Host": f"{host}{port}"},
            {"sni_hostname": host},
        )
        for a in addresses
    ]


def _is_public_https(url: str) -> bool:
    """https-only, with a host that resolves to public addresses only (SSRF guard)."""
    return _pin(url) is not None


def _send(
    client: httpx.Client, method: str, pinned: list[PinnedRequest], **kwargs: Any
) -> httpx.Response:
    """Send to each vetted address in turn, moving on only after a `ConnectError`.

    The last address's error propagates.
    """
    *earlier, last = pinned
    for pin in earlier:
        try:
            return client.request(
                method, pin.url, headers=pin.headers, extensions=pin.extensions, **kwargs
            )
        except httpx.ConnectError:
            continue
    return client.request(
        method, last.url, headers=last.headers, extensions=last.extensions, **kwargs
    )


def _fernet(settings: Settings) -> Fernet:
    """Build the Fernet cipher from the server's key.

    The only place in this module that reads the raw key.
    """
    key = settings.simplefin_encryption_key.get_secret_value()
    return Fernet(key.encode())


def encrypt_access_url(settings: Settings, url: str) -> bytes:
    """Encrypt an Access URL with the server's Fernet key."""
    return _fernet(settings).encrypt(url.encode())


def decrypt_access_url(settings: Settings, blob: bytes) -> str:
    """Decrypt a stored Access URL. For #48 (the nightly sync worker)."""
    return _fernet(settings).decrypt(blob).decode()


def _decode_claim_url(setup_token: str) -> list[PinnedRequest]:
    """Base64-decode the setup token into a claim URL pinned to its vetted IP,
    refusing anything but a public https URL (SSRF guard) before any request is made.
    """
    try:
        claim_url = base64.b64decode(setup_token, validate=True).decode()
    except binascii.Error, UnicodeDecodeError:
        raise InvalidSetupToken("The setup token is not valid base64.") from None
    pinned = _pin(claim_url)
    if pinned is None:
        raise InvalidSetupToken("The setup token must decode to a public https URL.")
    return pinned


def _post_claim(pinned: list[PinnedRequest], transport: httpx.BaseTransport | None) -> str:
    """POST the pinned claim URL and return the raw Access URL text from a 200 response."""
    with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
        try:
            r = _send(client, "POST", pinned)
        except httpx.HTTPError:
            raise SimplefinRequestFailed(
                "Could not reach SimpleFIN to claim the setup token."
            ) from None
        if r.status_code == 403:
            raise ClaimRejected("The claim token either does not exist or has already been used.")
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            raise SimplefinRequestFailed(
                f"SimpleFIN returned {r.status_code} while claiming."
            ) from None
    return r.text.strip()  # the Bridge's plain-text body sometimes ends in a newline


def claim(setup_token: str, *, transport: httpx.BaseTransport | None = None) -> str:
    """Exchange a one-time setup token for an Access URL.

    Raises `InvalidSetupToken` for a malformed token or an unsafe claim URL,
    `ClaimRejected` for a 403 (already claimed or invalid), and
    `SimplefinRequestFailed` for anything else, including a timeout or an
    Access URL that doesn't come back as https with credentials.
    """
    access_url = _post_claim(_decode_claim_url(setup_token), transport)
    parsed = urlsplit(access_url)
    if not _is_public_https(access_url) or parsed.username is None or parsed.password is None:
        raise SimplefinRequestFailed("SimpleFIN returned an unexpected Access URL.")
    return access_url


def fetch(
    access_url: str, start: int, end: int, *, transport: httpx.BaseTransport | None = None
) -> dict:
    """Fetch `/accounts` for `[start, end)`.

    Raises `SimplefinRequestFailed` on any network error, timeout, non-2xx response
    (403 = access revoked, 402 = subscription lapsed), or a 200 that isn't valid
    JSON; the message never names the Access URL or its credentials.
    """
    u = urlsplit(access_url)
    base = f"{u.scheme}://{u.netloc.rpartition('@')[2]}{u.path}"
    pinned = _pin(f"{base}/accounts")
    if pinned is None:
        raise SimplefinRequestFailed(
            f"SimpleFIN host {u.hostname} did not resolve to a public address."
        )
    with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
        try:
            r = _send(
                client,
                "GET",
                pinned,
                auth=(u.username or "", u.password or ""),
                params={"version": 2, "start-date": start, "end-date": end, "pending": 1},
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            raise SimplefinRequestFailed(f"SimpleFIN fetch failed for {u.hostname}.") from None
        except ValueError:
            raise SimplefinRequestFailed(
                f"SimpleFIN returned a non-JSON response for {u.hostname}."
            ) from None


def claim_connection(
    db: Session,
    settings: Settings,
    user_id: UUID,
    setup_token: str,
    now: datetime,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[SimplefinConnection, str]:
    """Claim a setup token and store its Access URL, encrypted, replacing any
    previous connection for this user.

    Returns the row and the Access URL's host; the URL itself is never returned.
    """
    fernet = _fernet(settings)  # fail before the one-time token is spent on a bad key
    access_url = claim(setup_token, transport=transport)
    host = urlsplit(access_url).hostname or ""
    blob = fernet.encrypt(access_url.encode())
    stmt = (
        insert(SimplefinConnection)
        .values(id=uuid4(), user_id=user_id, access_url_encrypted=blob, claimed_at=now)
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={"access_url_encrypted": blob, "claimed_at": now, "updated_at": now},
        )
        .returning(SimplefinConnection)
    )
    connection = db.scalars(stmt).one()
    logger.info("SimpleFIN connection claimed (host={})", host)
    return connection, host
