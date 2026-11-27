"""Claude through Workload Identity Federation: the VM's Google identity token instead of a key."""

from typing import Any

import httpx
import pytest
from anthropic.lib.credentials import WorkloadIdentityCredentials, WorkloadIdentityError
from pydantic import SecretStr

from app.config import Settings
from app.services import ai
from app.services.ai import make_client as real_make_client


@pytest.fixture
def federated(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "anthropic_federation_rule_id": "fdrl_test",
            "anthropic_organization_id": "00000000-0000-0000-0000-000000000000",
            "anthropic_service_account_id": "svac_test",
        }
    )


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The keyword arguments `make_client` gives the SDK client, without building one."""
    kwargs: dict[str, Any] = {}
    monkeypatch.setattr(ai.anthropic, "Anthropic", lambda **kw: kwargs.update(kw))
    return kwargs


def test_federation_turns_ai_on_without_a_key(federated: Settings) -> None:
    assert not federated.anthropic_api_key.get_secret_value()
    assert ai.is_enabled(federated)


def test_the_organization_alone_is_not_enough(settings: Settings) -> None:
    half = settings.model_copy(update={"anthropic_organization_id": "org"})
    assert not ai.is_enabled(half)


def test_client_uses_federated_credentials(federated: Settings, built: dict[str, Any]) -> None:
    real_make_client(federated)
    assert "api_key" not in built
    credentials = built["credentials"]
    assert isinstance(credentials, WorkloadIdentityCredentials)


def test_client_falls_back_to_the_key(settings: Settings, built: dict[str, Any]) -> None:
    real_make_client(settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")}))
    assert built["api_key"] == "sk-ant-test"
    assert "credentials" not in built


def test_identity_token_comes_from_the_metadata_server(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def get(url: str, **kwargs: Any) -> httpx.Response:
        seen.update(url=url, **kwargs)
        return httpx.Response(200, text="header.payload.sig", request=httpx.Request("GET", url))

    monkeypatch.setattr(ai.httpx, "get", get)
    assert ai.google_identity_token() == "header.payload.sig"
    assert seen["url"] == ai.GCE_IDENTITY_URL
    assert seen["params"] == {"audience": "https://api.anthropic.com", "format": "full"}
    assert seen["headers"] == {"Metadata-Flavor": "Google"}


def test_metadata_server_failure_is_a_workload_identity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get(url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("no route", request=httpx.Request("GET", url))

    monkeypatch.setattr(ai.httpx, "get", get)
    with pytest.raises(WorkloadIdentityError):
        ai.google_identity_token()
