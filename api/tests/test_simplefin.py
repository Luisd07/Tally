"""Service-level tests for `app.services.simplefin`: the crypto helpers and the HTTP
boundary (`claim`/`fetch`). Mocked at the transport per
https://www.simplefin.org/protocol.html `POST /claim/:token` ("Successful response
body ... is an Access URL" / "403 The claim token either does not exist or has
already been [claimed]") and `GET /accounts`. Never calls the real SimpleFIN.

The SSRF guard resolves the claim/Access URL host via `socket.getaddrinfo`, a second
boundary alongside HTTP; every test here stubs it rather than touching real DNS.
"""

import base64
import io
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from loguru import logger
from pydantic import SecretStr

from app.config import Settings
from app.log import setup_logging
from app.services import simplefin

CLAIM_URL = "https://bridge.simplefin.org/simplefin/claim/demo-token"
SETUP_TOKEN = base64.b64encode(CLAIM_URL.encode()).decode()
ACCESS_URL = "https://demo-user:demo-pass@bridge.simplefin.org/simplefin"

PUBLIC_SOCKADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
PRIVATE_SOCKADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
LOOPBACK_SOCKADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
NAT64_SOCKADDR = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::a9fe:a9fe", 443, 0, 0))]


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hostname in this module is `bridge.simplefin.org`-shaped; the SSRF guard
    resolves it via `socket.getaddrinfo`, so stub that boundary to a fixed public
    address by default. Individual tests override it to probe a specific resolution.
    """
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: PUBLIC_SOCKADDR)


def _never_resolve(*args: object, **kwargs: object) -> list[object]:
    raise AssertionError("a numeric-looking host must be refused before any DNS lookup")


def _claim_token_for_host(host: str) -> str:
    netloc = f"[{host}]" if ":" in host else host
    claim_url = f"https://{netloc}/simplefin/claim/demo"
    return base64.b64encode(claim_url.encode()).decode()


def _settings(key: str | None = None) -> Settings:
    return Settings(simplefin_encryption_key=SecretStr(key or Fernet.generate_key().decode()))


def test_encrypt_decrypt_round_trips() -> None:
    settings = _settings()
    blob = simplefin.encrypt_access_url(settings, ACCESS_URL)
    assert simplefin.decrypt_access_url(settings, blob) == ACCESS_URL
    assert ACCESS_URL.encode() not in blob


def test_decrypt_with_the_wrong_key_fails_cleanly() -> None:
    blob = simplefin.encrypt_access_url(_settings(), ACCESS_URL)
    with pytest.raises(InvalidToken):
        simplefin.decrypt_access_url(_settings(), blob)


def test_claim_returns_the_access_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://8.8.8.8/simplefin/claim/demo-token"
        assert request.headers["Host"] == "bridge.simplefin.org"
        assert request.extensions["sni_hostname"] == "bridge.simplefin.org"
        return httpx.Response(200, text=ACCESS_URL)

    access_url = simplefin.claim(SETUP_TOKEN, transport=httpx.MockTransport(handler))
    assert access_url == ACCESS_URL


def _counting_resolver(calls: list[str]) -> Callable[..., list[tuple[Any, ...]]]:
    def resolve(host: str, *args: object, **kwargs: object) -> list[tuple[Any, ...]]:
        calls.append(host)
        return PUBLIC_SOCKADDR

    return resolve


def test_claim_resolves_the_bridge_host_once_and_connects_to_the_vetted_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", _counting_resolver(calls))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="https://u:p@access.example.com/simplefin")

    simplefin.claim(SETUP_TOKEN, transport=httpx.MockTransport(handler))

    assert calls.count("bridge.simplefin.org") == 1
    assert [r.url.host for r in seen] == ["8.8.8.8"]
    assert seen[0].headers["Host"] == "bridge.simplefin.org"


def test_fetch_pins_an_ipv4_address_when_ipv6_resolves_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ipv6_first = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
        *PUBLIC_SOCKADDR,
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: ipv6_first)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"accounts": []})

    simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(handler))

    assert seen[0].url.host == "8.8.8.8"


def test_fetch_refuses_a_host_that_now_resolves_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PRIVATE_SOCKADDR)

    with pytest.raises(simplefin.SimplefinRequestFailed, match="public address"):
        simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(lambda r: pytest.fail()))


def test_fetch_resolves_once_and_connects_to_the_vetted_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", _counting_resolver(calls))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"accounts": []})

    simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(handler))

    assert calls == ["bridge.simplefin.org"]
    assert seen[0].url.host == "8.8.8.8"
    assert seen[0].headers["Host"] == "bridge.simplefin.org"
    assert seen[0].extensions["sni_hostname"] == "bridge.simplefin.org"


def test_fetch_tries_the_next_vetted_address_after_a_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    two_public = [*PUBLIC_SOCKADDR, (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.4.4", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: two_public)
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == "8.8.8.8":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"accounts": []})

    simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(handler))

    assert tried == ["8.8.8.8", "8.8.4.4"]


def test_claim_gives_up_after_the_last_address_fails_to_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    two_public = [*PUBLIC_SOCKADDR, (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.4.4", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: two_public)
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        raise httpx.ConnectError("down")

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.claim(SETUP_TOKEN, transport=httpx.MockTransport(handler))

    assert tried == ["8.8.8.8", "8.8.4.4"]


def test_fetch_reports_a_resolution_failure_apart_from_a_private_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> list[object]:
        raise socket.gaierror(-3, "temporary failure")

    monkeypatch.setattr(socket, "getaddrinfo", fail)

    with pytest.raises(simplefin.SimplefinRequestFailed, match="Could not resolve"):
        simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(lambda r: pytest.fail()))


def test_claim_rejects_a_non_numeric_port() -> None:
    claim_url = "https://bridge.simplefin.org:abc/simplefin/claim/demo"
    token = base64.b64encode(claim_url.encode()).decode()
    with pytest.raises(simplefin.InvalidSetupToken):
        simplefin.claim(token)


def test_claim_rejects_bad_base64() -> None:
    with pytest.raises(simplefin.InvalidSetupToken):
        simplefin.claim("not valid base64!!")


def test_claim_rejects_a_non_https_claim_url() -> None:
    token = base64.b64encode(b"http://bridge.simplefin.org/simplefin/claim/demo").decode()
    with pytest.raises(simplefin.InvalidSetupToken):
        simplefin.claim(token)


@pytest.mark.parametrize(
    ("host", "sockaddr"),
    [
        ("127.1", None),
        ("2130706433", None),
        ("0x7f000001", None),
        ("017700000001", None),
        ("169.254.169.254", None),
        ("64:ff9b::a9fe:a9fe", NAT64_SOCKADDR),
        ("localhost", LOOPBACK_SOCKADDR),
        ("bank.example.com", PRIVATE_SOCKADDR),
    ],
    ids=[
        "dotted-shorthand",
        "decimal",
        "hex",
        "octal",
        "canonical-metadata-ip",
        "nat64",
        "localhost",
        "dns-name-resolving-private",
    ],
)
def test_claim_rejects_unsafe_hosts(
    host: str, sockaddr: list[tuple[object, ...]] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A numeric-looking host (sockaddr is None) must be refused before any lookup;
    # everything else is resolved and must come back private, loopback, or NAT64.
    monkeypatch.setattr(
        socket, "getaddrinfo", _never_resolve if sockaddr is None else lambda *a, **k: sockaddr
    )

    with pytest.raises(simplefin.InvalidSetupToken):
        simplefin.claim(_claim_token_for_host(host))


def test_claim_403_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(simplefin.ClaimRejected):
        simplefin.claim(SETUP_TOKEN, transport=httpx.MockTransport(handler))


def test_claim_network_error_is_request_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.claim(SETUP_TOKEN, transport=httpx.MockTransport(handler))


def test_claim_server_error_is_request_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.claim(SETUP_TOKEN, transport=httpx.MockTransport(handler))


def test_claim_refuses_a_non_https_access_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="http://demo-user:demo-pass@bridge.simplefin.org/simplefin")

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.claim(SETUP_TOKEN, transport=httpx.MockTransport(handler))


def test_claim_refuses_an_access_url_without_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="https://bridge.simplefin.org/simplefin")

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.claim(SETUP_TOKEN, transport=httpx.MockTransport(handler))


def test_fetch_returns_the_accounts_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/simplefin/accounts"
        assert request.url.params["version"] == "2"
        return httpx.Response(200, json={"accounts": []})

    data = simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(handler))
    assert data == {"accounts": []}


def test_fetch_network_error_is_request_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(handler))


def test_fetch_subscription_lapsed_402_is_request_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402)

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(handler))


def test_fetch_access_revoked_403_is_request_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(handler))


def test_fetch_non_json_response_is_request_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(simplefin.SimplefinRequestFailed):
        simplefin.fetch(ACCESS_URL, 0, 1, transport=httpx.MockTransport(handler))


def test_claim_never_logs_the_decoded_claim_url(tmp_path: Path) -> None:
    """MV-1 repro: httpx logs `HTTP Request: POST <url> ...` at INFO, and that URL is
    the claim URL (the setup token, decoded). `setup_logging` must keep it out of
    every log, under the production settings/log level the finding reproduced with.
    """
    key_path = tmp_path / "AuthKey_TEST123.p8"
    key_path.write_text("fake-key-contents")
    settings = Settings(
        environment="production",
        log_level="INFO",
        database_url=SecretStr("postgresql+psycopg://tally:s3cure@db:5432/tally"),
        jwt_signing_key=SecretStr("k" * 48),
        simplefin_encryption_key=SecretStr(Fernet.generate_key().decode()),
        webauthn_rp_id="api.example.com",
        webauthn_origin="https://api.example.com",
        apns_key_id="TESTKEY123",
        apns_team_id="TESTTEAM456",
        apns_bundle_id="com.example.tally",
        apns_private_key_path=str(key_path),
        restic_repository="gs:tally-backups-prod:/tally",
        restic_password=SecretStr("s3cure-restic-password"),
    )
    setup_logging(settings)
    sink = io.StringIO()
    handler_id = logger.add(sink, level="DEBUG")
    try:
        simplefin.claim(
            SETUP_TOKEN,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ACCESS_URL)),
        )
    finally:
        logger.remove(handler_id)

    output = sink.getvalue()
    assert CLAIM_URL not in output
    assert "demo-token" not in output
