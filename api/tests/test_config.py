from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from app.config import Settings


@pytest.fixture
def safe_production(tmp_path: Path):
    key_path = tmp_path / "AuthKey_TEST123.p8"
    key_path.write_text("fake-key-contents")
    return {
        "environment": "production",
        "database_url": SecretStr("postgresql+psycopg://tally:s3cure@db:5432/tally"),
        "jwt_signing_key": SecretStr("k" * 48),
        "simplefin_encryption_key": SecretStr(Fernet.generate_key().decode()),
        "webauthn_rp_id": "api.example.com",
        "webauthn_origin": "https://api.example.com",
        "apns_key_id": "TESTKEY123",
        "apns_team_id": "TESTTEAM456",
        "apns_bundle_id": "com.example.tally",
        "apns_private_key_path": str(key_path),
        "restic_repository": "gs:tally-backups-prod:/tally",
        "restic_password": SecretStr("s3cure-restic-password"),
    }


def test_safe_production_settings_load(safe_production) -> None:
    assert Settings(**safe_production).is_production


@pytest.mark.parametrize(
    "override",
    [
        {"database_url": SecretStr("postgresql+psycopg://tally:change-me@db:5432/tally")},
        {"jwt_signing_key": SecretStr("change-me")},
        {"anthropic_api_key": SecretStr("change-me")},
        {"anthropic_api_key": SecretStr("# set me")},
        {"jwt_signing_key": SecretStr("too-short")},
        {"simplefin_encryption_key": SecretStr("")},
        {"simplefin_encryption_key": SecretStr("not-a-valid-fernet-key")},
        {"webauthn_rp_id": "localhost"},
        {"webauthn_origin": "http://api.example.com"},
        {"apns_key_id": ""},
        {"apns_team_id": ""},
        {"apns_bundle_id": ""},
        {"apns_private_key_path": ""},
        {"restic_repository": ""},
        {"restic_repository": "gs:change-me:/tally"},
        {"restic_password": SecretStr("change-me")},
        {"restic_password": SecretStr("")},
    ],
)
def test_production_refuses_placeholders(safe_production, override: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Unsafe production settings"):
        Settings(**{**safe_production, **override})


@pytest.mark.parametrize(
    "override",
    [
        {"restic_repository": "# e.g. gs:<bucket>:/tally"},
        {"gcs_key_file": "# optional: service-account key file path"},
        {"apns_private_key_path": "# path"},
        {"restic_password": SecretStr("# comment")},
        {"simplefin_encryption_key": SecretStr("# Fernet key")},
    ],
)
def test_production_refuses_values_that_are_comments(
    safe_production, override: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="looks like a comment"):
        Settings(**{**safe_production, **override})


def test_production_requires_the_apns_key_file_to_exist(safe_production, tmp_path: Path) -> None:
    missing = tmp_path / "missing.p8"
    with pytest.raises(ValidationError, match="Unsafe production settings"):
        Settings(**{**safe_production, "apns_private_key_path": str(missing)})


def test_production_allows_no_gcs_key_file(safe_production) -> None:
    """The VM's own service account (ADC) needs no key file at all."""
    assert Settings(**safe_production).is_production


def test_production_requires_the_gcs_key_file_to_exist_if_set(
    safe_production, tmp_path: Path
) -> None:
    missing = tmp_path / "missing-key.json"
    with pytest.raises(ValidationError, match="Unsafe production settings"):
        Settings(**{**safe_production, "gcs_key_file": str(missing)})


def test_production_allows_an_existing_gcs_key_file(safe_production, tmp_path: Path) -> None:
    key_path = tmp_path / "gcs-key.json"
    key_path.write_text("{}")
    assert Settings(**{**safe_production, "gcs_key_file": str(key_path)}).is_production


def test_reimbursements_on_1099_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REIMBURSEMENTS_ON_1099", raising=False)
    assert Settings(_env_file=None).reimbursements_on_1099 is True  # type: ignore[call-arg]


def test_development_allows_placeholders() -> None:
    assert not Settings(environment="development").is_production


def test_secrets_load_from_files(safe_production, tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    jwt = "j" * 48
    restic = "restic-from-secret-manager"
    (secrets / "jwt_signing_key").write_text(jwt + "\n")
    (secrets / "restic_password").write_text(restic)
    from_files = {"jwt_signing_key", "restic_password"}
    values = {k: v for k, v in safe_production.items() if k not in from_files}

    settings = Settings(_secrets_dir=secrets, **values)  # pyright: ignore[reportCallIssue]

    assert settings.jwt_signing_key.get_secret_value() == jwt
    assert settings.restic_password.get_secret_value() == restic


def test_missing_secret_file_is_still_refused_in_production(
    safe_production, tmp_path: Path
) -> None:
    values = {k: v for k, v in safe_production.items() if k != "jwt_signing_key"}
    with pytest.raises(ValidationError, match="JWT_SIGNING_KEY"):
        Settings(_secrets_dir=tmp_path, **values)  # pyright: ignore[reportCallIssue]
