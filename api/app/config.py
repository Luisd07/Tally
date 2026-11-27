import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER = "change-me"

# The single user's local zone: schedules and "today" are both read in it.
APP_TIMEZONE = ZoneInfo("America/New_York")


def _is_valid_fernet_key(key: str) -> bool:
    try:
        Fernet(key.encode())
    except ValueError:
        return False
    return True


def _secrets_dir() -> str | None:
    """Production reads secrets from files, one per setting (`/run/secrets/jwt_signing_key`,
    ...), fetched from Secret Manager at boot into tmpfs (#23). `SECRETS_DIR` names the folder;
    unset (local runs, tests) means no secret files. Environment variables and `.env` still win
    over the files.
    """
    return os.environ.get("SECRETS_DIR") or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", secrets_dir=_secrets_dir())

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: SecretStr = SecretStr(
        f"postgresql+psycopg://tally:{PLACEHOLDER}@127.0.0.1:5433/tally"
    )

    jwt_signing_key: SecretStr = SecretStr(PLACEHOLDER)
    jwt_issuer: str = "tally-api"
    jwt_audience: str = "tally-ios"
    access_token_minutes: int = 15
    refresh_token_days: int = 60

    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Tally"
    webauthn_origin: str = "https://localhost"

    simplefin_encryption_key: SecretStr = SecretStr("")

    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_bundle_id: str = ""
    apns_private_key_path: str = ""

    # App Attest (#75). The App ID it checks is "<apns_team_id>.<apns_bundle_id>". While
    # false, sign-in and refresh still verify an assertion the app sends but allow none.
    app_attest_required: bool = False
    app_attest_environment: Literal["development", "production"] = "production"

    # Claude (#168). Every AI call goes through `app.services.ai`. An empty key turns AI off,
    # and the app works as it does without it. `ai_feature_models` overrides `ai_model` for
    # one feature, e.g. AI_FEATURE_MODELS='{"tax_review": "claude-opus-5-5"}'.
    anthropic_api_key: SecretStr = SecretStr("")
    # Workload Identity Federation, preferred over the key: the VM's Google identity token is
    # traded for a short-lived Claude token, so there is no key to rotate. The rule and
    # organization IDs turn it on; the workspace ID is needed only for a multi-workspace rule.
    anthropic_federation_rule_id: str = ""
    anthropic_organization_id: str = ""
    anthropic_service_account_id: str = ""
    anthropic_workspace_id: str = ""
    ai_model: str = "claude-sonnet-5-5"
    ai_feature_models: dict[str, str] = {}
    ai_monthly_budget_usd: Decimal = Decimal("90")

    receipts_dir: Path = Path("/data/receipts")
    backups_dir: Path = Path("/backups")
    reimbursements_on_1099: bool = True

    # restic repository, e.g. "gs:<bucket>:/tally". GCS credentials come from the VM's
    # service account via Application Default Credentials, so no key file is required;
    # gcs_key_file is only set where ADC isn't available (e.g. running the backup locally).
    restic_repository: str = ""
    restic_password: SecretStr = SecretStr(PLACEHOLDER)
    gcs_key_file: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _refuse_placeholders_in_production(self) -> Self:
        if not self.is_production:
            return self
        problems = []
        if PLACEHOLDER in self.database_url.get_secret_value():
            problems.append("DATABASE_URL still uses the placeholder password")
        if len(key := self.jwt_signing_key.get_secret_value()) < 32 or PLACEHOLDER in key:
            problems.append("JWT_SIGNING_KEY must be a random value of at least 32 characters")
        if not (key := self.simplefin_encryption_key.get_secret_value()):
            problems.append("SIMPLEFIN_ENCRYPTION_KEY is not set")
        elif not _is_valid_fernet_key(key):
            problems.append("SIMPLEFIN_ENCRYPTION_KEY must be a valid Fernet key")
        if not self.webauthn_origin.startswith("https://") or self.webauthn_rp_id == "localhost":
            problems.append("WEBAUTHN_RP_ID and WEBAUTHN_ORIGIN must name the real HTTPS domain")
        if not self.apns_key_id or PLACEHOLDER in self.apns_key_id:
            problems.append("APNS_KEY_ID is not set")
        if not self.apns_team_id or PLACEHOLDER in self.apns_team_id:
            problems.append("APNS_TEAM_ID is not set")
        if not self.apns_bundle_id or PLACEHOLDER in self.apns_bundle_id:
            problems.append("APNS_BUNDLE_ID is not set")
        if not self.apns_private_key_path or PLACEHOLDER in self.apns_private_key_path:
            problems.append("APNS_PRIVATE_KEY_PATH is not set")
        elif not Path(self.apns_private_key_path).is_file():
            problems.append("APNS_PRIVATE_KEY_PATH does not point to an existing file")
        if not self.restic_repository or PLACEHOLDER in self.restic_repository:
            problems.append("RESTIC_REPOSITORY is not set")
        if (
            not (restic_password := self.restic_password.get_secret_value())
            or PLACEHOLDER in restic_password
        ):
            problems.append("RESTIC_PASSWORD is not set")
        # AI is optional, so an empty key is fine; a placeholder is not.
        if PLACEHOLDER in self.anthropic_api_key.get_secret_value():
            problems.append("ANTHROPIC_API_KEY still holds the placeholder")
        if self.gcs_key_file and not Path(self.gcs_key_file).is_file():
            problems.append("GCS_KEY_FILE does not point to an existing file")
        # Compose's env_file parser keeps an inline comment after an empty value as the value.
        for name, value in (
            ("DATABASE_URL", self.database_url.get_secret_value()),
            ("JWT_SIGNING_KEY", self.jwt_signing_key.get_secret_value()),
            ("SIMPLEFIN_ENCRYPTION_KEY", self.simplefin_encryption_key.get_secret_value()),
            ("APNS_PRIVATE_KEY_PATH", self.apns_private_key_path),
            ("RESTIC_REPOSITORY", self.restic_repository),
            ("RESTIC_PASSWORD", self.restic_password.get_secret_value()),
            ("ANTHROPIC_API_KEY", self.anthropic_api_key.get_secret_value()),
            ("GCS_KEY_FILE", self.gcs_key_file),
        ):
            if value.startswith("#"):
                problems.append(f"{name} looks like a comment, not a value")
        if problems:
            raise ValueError("Unsafe production settings: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
