"""Request-derived network facts shared by auth events and rate limiting."""

import ipaddress

from fastapi import Request


def client_ip(request: Request) -> str | None:
    """The caller's address, normalized and validated.

    `request.client.host` comes from uvicorn's proxy-header handling, which (per
    `--forwarded-allow-ips="*"`) trusts the whole chain and uses whatever X-Forwarded-For value
    it finds. In production, Caddy sits in front and replaces any client-supplied
    X-Forwarded-For, so this is defense in depth: parse it as an IP and reject anything else,
    including TestClient's "testclient" host. Callers that need a value for an invalid or
    missing IP supply their own.
    """
    if request.client is None:
        return None
    host = request.client.host.split("%", 1)[0]  # drop an IPv6 zone id, e.g. fe80::1%eth0
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    text = str(ip)
    return text if len(text) <= 45 else None
