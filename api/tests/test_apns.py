import importlib.util
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from loguru import logger
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Device, User
from app.models.enums import ApnsEnvironment
from app.services import apns

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)


@dataclass
class _ApnsFixture:
    settings: Settings
    public_key_pem: bytes


@pytest.fixture
def apns_fixture(tmp_path: Path) -> _ApnsFixture:
    """A real P-256 keypair on disk, so the service's ES256 signing can be verified
    with the public half, the same way APNs would.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "AuthKey_TESTKEY123.p8"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_key_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    settings = Settings(
        environment="test",
        apns_key_id="TESTKEY123",
        apns_team_id="TESTTEAM456",
        apns_bundle_id="com.example.tally",
        apns_private_key_path=str(key_path),
    )
    return _ApnsFixture(settings=settings, public_key_pem=public_key_pem)


def _make_device(user: User, **kwargs: object) -> Device:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "apns_token": "ab" * 32,
        "environment": ApnsEnvironment.sandbox,
        "last_seen_at": NOW,
    }
    defaults.update(kwargs)
    return Device(**defaults)


# --- provider JWT -----------------------------------------------------------------


def test_get_provider_token_header_and_claims(apns_fixture: _ApnsFixture) -> None:
    token = apns.get_provider_token(apns_fixture.settings, NOW)

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "TESTKEY123"

    claims = jwt.decode(token, apns_fixture.public_key_pem, algorithms=["ES256"])
    assert claims["iss"] == "TESTTEAM456"
    assert claims["iat"] == int(NOW.timestamp())


def test_get_provider_token_is_cached_within_the_refresh_window(
    apns_fixture: _ApnsFixture,
) -> None:
    first = apns.get_provider_token(apns_fixture.settings, NOW)
    second = apns.get_provider_token(apns_fixture.settings, NOW + timedelta(minutes=10))

    assert first == second


def test_get_provider_token_refreshes_after_50_minutes(apns_fixture: _ApnsFixture) -> None:
    first = apns.get_provider_token(apns_fixture.settings, NOW)
    later = NOW + timedelta(minutes=51)
    second = apns.get_provider_token(apns_fixture.settings, later)

    assert first != second
    claims = jwt.decode(second, apns_fixture.public_key_pem, algorithms=["ES256"])
    assert claims["iat"] == int(later.timestamp())


def test_mint_provider_token_bypasses_and_replaces_the_cache(apns_fixture: _ApnsFixture) -> None:
    first = apns.get_provider_token(apns_fixture.settings, NOW)
    second = apns.mint_provider_token(apns_fixture.settings, NOW)

    assert first != second
    assert apns.get_provider_token(apns_fixture.settings, NOW) == second


# --- make_client ----------------------------------------------------------------------


def test_make_client_is_configured_for_http2() -> None:
    client = apns.make_client()
    try:
        # httpx exposes no public attribute for this; the http2 flag only shows up on
        # httpcore's connection pool underneath the transport.
        transport = client._transport
        assert isinstance(transport, httpx.HTTPTransport)
        assert transport._pool._http2 is True
    finally:
        client.close()


def test_h2_is_installed() -> None:
    assert importlib.util.find_spec("h2") is not None


# --- send_alert ---------------------------------------------------------------------


def test_send_alert_posts_to_sandbox_host_with_expected_shape(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user, environment=ApnsEnvironment.sandbox)
    db_session.add(device)
    db_session.flush()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    title, body = "4 new charges", "1 looks reimbursable"
    apns.send_alert(db_session, user.id, title, body, NOW, client, apns_fixture.settings)

    assert len(requests) == 1
    req = requests[0]
    assert str(req.url) == f"https://api.sandbox.push.apple.com/3/device/{device.apns_token}"
    assert req.headers["apns-topic"] == "com.example.tally"
    assert req.headers["apns-push-type"] == "alert"
    assert req.headers["apns-priority"] == "10"
    assert req.headers["authorization"].startswith("bearer ")
    assert json.loads(req.content) == {
        "aps": {"alert": {"title": "4 new charges", "body": "1 looks reimbursable"}}
    }


def test_send_alert_posts_to_production_host(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user, environment=ApnsEnvironment.production)
    db_session.add(device)
    db_session.flush()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)

    assert str(requests[0].url) == f"https://api.push.apple.com/3/device/{device.apns_token}"


def test_send_alert_sends_to_every_device_of_the_user_and_no_one_elses(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    other = User(id=uuid.uuid4(), display_name="Other User")
    db_session.add(other)
    db_session.flush()
    mine_1 = _make_device(user, apns_token="aa" * 32)
    mine_2 = _make_device(user, apns_token="bb" * 32)
    theirs = _make_device(other, apns_token="cc" * 32)
    db_session.add_all([mine_1, mine_2, theirs])
    db_session.flush()
    tokens_hit: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens_hit.append(str(request.url).rsplit("/", 1)[-1])
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)

    assert sorted(tokens_hit) == sorted([mine_1.apns_token, mine_2.apns_token])


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [
        (410, "Unregistered"),
        (410, "ExpiredToken"),
        (400, "BadDeviceToken"),
        (400, "DeviceTokenNotForTopic"),
    ],
    ids=["410-unregistered", "410-expired-token", "400-bad-device-token", "400-not-for-topic"],
)
def test_send_alert_deletes_the_device(
    db_session: Session,
    user: User,
    apns_fixture: _ApnsFixture,
    status_code: int,
    reason: str,
) -> None:
    device = _make_device(user)
    db_session.add(device)
    db_session.flush()
    device_id = device.id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"reason": reason})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)

    assert db_session.get(Device, device_id) is None


def test_send_alert_refreshes_the_token_once_and_retries_on_expired_provider_token(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user)
    db_session.add(device)
    db_session.flush()
    device_id = device.id
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["authorization"])
        if len(authorizations) == 1:
            return httpx.Response(403, json={"reason": "ExpiredProviderToken"})
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)

    assert len(authorizations) == 2
    assert authorizations[0] != authorizations[1]
    assert db_session.get(Device, device_id) is not None


def test_send_alert_does_not_retry_an_invalid_provider_token(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user)
    db_session.add(device)
    db_session.flush()
    device_id = device.id
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        return httpx.Response(403, json={"reason": "InvalidProviderToken"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)

    assert len(calls) == 1  # the key itself is wrong; retrying can't fix that
    assert db_session.get(Device, device_id) is not None


def test_send_alert_does_not_raise_when_the_retry_also_fails(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user)
    db_session.add(device)
    db_session.flush()
    device_id = device.id
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(403, json={"reason": "ExpiredProviderToken"})
        return httpx.Response(500, json={"reason": "InternalServerError"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)

    assert len(calls) == 2
    assert db_session.get(Device, device_id) is not None


def test_send_alert_does_not_raise_when_the_provider_token_cannot_be_minted(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user)
    db_session.add(device)
    db_session.flush()
    device_id = device.id
    missing_key_path = Path(apns_fixture.settings.apns_private_key_path).parent / "missing.p8"
    broken_settings = apns_fixture.settings.model_copy(
        update={"apns_private_key_path": str(missing_key_path)}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never reach APNs")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        apns.send_alert(db_session, user.id, "t", "b", NOW, client, broken_settings)
    finally:
        logger.remove(sink_id)

    assert db_session.get(Device, device_id) is not None
    assert any("APNs provider token unavailable" in message for message in messages)


def test_send_alert_does_not_raise_on_a_connect_error(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user)
    db_session.add(device)
    db_session.flush()
    device_id = device.id

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)
    finally:
        logger.remove(sink_id)

    assert db_session.get(Device, device_id) is not None
    assert any("APNs request failed" in message for message in messages)


def test_send_alert_refreshes_the_provider_token_at_most_once_per_call(
    db_session: Session,
    user: User,
    apns_fixture: _ApnsFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine_1 = _make_device(user, apns_token="aa" * 32)
    mine_2 = _make_device(user, apns_token="bb" * 32)
    db_session.add_all([mine_1, mine_2])
    db_session.flush()
    apns.get_provider_token(apns_fixture.settings, NOW)  # warm the cache before counting

    mint_calls: list[None] = []
    real_mint = apns.mint_provider_token

    def counting_mint(settings: Settings, now: datetime) -> str:
        mint_calls.append(None)
        return real_mint(settings, now)

    monkeypatch.setattr(apns, "mint_provider_token", counting_mint)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"reason": "ExpiredProviderToken"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)

    assert len(mint_calls) == 1


def test_send_alert_does_not_raise_on_an_unrelated_server_error(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user)
    db_session.add(device)
    db_session.flush()
    device_id = device.id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"reason": "InternalServerError"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    # Must not raise.
    apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)

    assert db_session.get(Device, device_id) is not None


@pytest.mark.parametrize(
    ("status_code", "text"),
    [
        (502, "<html>bad gateway</html>"),
        (400, ""),
    ],
    ids=["html-error-body", "empty-body"],
)
def test_send_alert_does_not_raise_on_a_non_json_error_body(
    db_session: Session,
    user: User,
    apns_fixture: _ApnsFixture,
    status_code: int,
    text: str,
) -> None:
    mine_1 = _make_device(user, apns_token="aa" * 32)
    mine_2 = _make_device(user, apns_token="bb" * 32)
    db_session.add_all([mine_1, mine_2])
    db_session.flush()
    urls_hit: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls_hit.append(str(request.url))
        return httpx.Response(status_code, text=text)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)
    finally:
        logger.remove(sink_id)

    assert len(urls_hit) == 2
    assert len(set(urls_hit)) == 2
    assert db_session.get(Device, mine_1.id) is not None
    assert db_session.get(Device, mine_2.id) is not None
    assert any("APNs push failed" in message for message in messages)


def test_send_alert_never_logs_the_private_key_or_the_provider_token(
    db_session: Session, user: User, apns_fixture: _ApnsFixture
) -> None:
    device = _make_device(user)
    db_session.add(device)
    db_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"reason": "InternalServerError"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        token = apns.mint_provider_token(apns_fixture.settings, NOW)
        apns.send_alert(db_session, user.id, "t", "b", NOW, client, apns_fixture.settings)
    finally:
        logger.remove(sink_id)

    private_key_pem = Path(apns_fixture.settings.apns_private_key_path).read_text()
    log_text = "\n".join(messages)
    assert private_key_pem not in log_text
    assert token not in log_text
