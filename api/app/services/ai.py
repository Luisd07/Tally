"""The one path to Claude (#168). Every AI feature calls `create_message` (or submits a batch
through `make_client` after `ensure_available`, then records each result with
`record_usage`), so the key check, the monthly cap and the usage log can't be skipped.
`create_message` commits the usage row as soon as the call returns, so the money spent is
counted whatever the caller does next.

What may be sent: transaction descriptions, merchants, amounts, dates, category names and
receipt images. Never account names or numbers, balances, or anything from SimpleFIN beyond
a transaction's own description. Callers build their prompts from those fields only.

Suggestions Claude makes are stored for the user to confirm; nothing here changes data.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anthropic
import httpx
from anthropic.lib.credentials import WorkloadIdentityCredentials, WorkloadIdentityError
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import APP_TIMEZONE, Settings
from app.models import AIUsage

# Sonnet 5.5's server-side refusal fallback (`fallbacks: "default"`). Not accepted by the
# Batches API, so batch requests go without it.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
BATCH_DISCOUNT = Decimal("0.5")
# Workload Identity Federation: the VM's metadata server signs an ID token for its service
# account with this audience, and the federation rule pins the same value.
GCE_IDENTITY_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/identity"
)
IDENTITY_AUDIENCE = "https://api.anthropic.com"
CENT = Decimal("0.01")
_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


PRICES = {
    "claude-sonnet-5-5": Price(Decimal("2"), Decimal("10"), Decimal("0.20"), Decimal("2.50")),
    # Where Sonnet 5.5's refusal fallback can land.
    "claude-sonnet-5": Price(Decimal("2"), Decimal("10"), Decimal("0.20"), Decimal("2.50")),
    "claude-opus-5-5": Price(Decimal("4"), Decimal("20"), Decimal("0.20"), Decimal("5")),
    "claude-haiku-5-5": Price(Decimal("0.10"), Decimal("0.50"), Decimal("0.01"), Decimal("0.125")),
}
# An unknown model is priced at the dearest rates, so spend is never under-counted.
UNKNOWN_PRICE = Price(
    max(p.input for p in PRICES.values()),
    max(p.output for p in PRICES.values()),
    max(p.cache_read for p in PRICES.values()),
    max(p.cache_write for p in PRICES.values()),
)


class AIUnavailable(Exception):
    """No call was made, or the call didn't produce an answer. `reason` is one of
    `disabled` (no key), `budget` (the month's cap is reached), `error` (the API failed)
    or `refused` (Claude declined). Features fall back to how the app works without AI."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class TokenUsage:
    input: int
    output: int
    cache_read: int
    cache_write: int

    @classmethod
    def of(cls, usage: Any) -> TokenUsage:
        """From an SDK `Usage` (or `BetaUsage`); missing cache counts read as 0."""
        return cls(
            input=usage.input_tokens or 0,
            output=usage.output_tokens or 0,
            cache_read=getattr(usage, "cache_read_input_tokens", None) or 0,
            cache_write=getattr(usage, "cache_creation_input_tokens", None) or 0,
        )


def uses_federation(settings: Settings) -> bool:
    """Whether Claude is reached through Workload Identity Federation rather than a key."""
    return bool(settings.anthropic_federation_rule_id and settings.anthropic_organization_id)


def is_enabled(settings: Settings) -> bool:
    return uses_federation(settings) or bool(settings.anthropic_api_key.get_secret_value())


def model_for(settings: Settings, feature: str) -> str:
    return settings.ai_feature_models.get(feature, settings.ai_model)


def google_identity_token() -> str:
    """A new Google-signed ID token for the VM's service account, from the metadata server.
    The SDK calls this before each token exchange; Google tokens carry a `jti`, so each
    exchange needs a fresh one. `format=full` adds the `email` claim the rule matches."""
    try:
        response = httpx.get(
            GCE_IDENTITY_URL,
            params={"audience": IDENTITY_AUDIENCE, "format": "full"},
            headers={"Metadata-Flavor": "Google"},
            timeout=5.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WorkloadIdentityError(
            f"Couldn't get an identity token from the metadata server ({type(exc).__name__})"
        ) from None
    return response.text


def make_client(settings: Settings) -> anthropic.Anthropic:
    """The SDK client: federated when the rule is configured, else the API key. The SDK
    exchanges and refreshes the federated token itself. Tests replace this function, so CI
    never reaches the API."""
    if uses_federation(settings):
        credentials = WorkloadIdentityCredentials(
            identity_token_provider=google_identity_token,
            federation_rule_id=settings.anthropic_federation_rule_id,
            organization_id=settings.anthropic_organization_id,
            service_account_id=settings.anthropic_service_account_id or None,
            workspace_id=settings.anthropic_workspace_id or None,
        )
        return anthropic.Anthropic(credentials=credentials, timeout=120.0, max_retries=2)
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), timeout=120.0, max_retries=2
    )


def month_start(now: datetime) -> datetime:
    """The start of `now`'s month in the app's timezone, as UTC."""
    local = now.astimezone(APP_TIMEZONE)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


def month_spend(db: Session, user_id: uuid.UUID, now: datetime) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(AIUsage.cost_usd), 0)).where(
            AIUsage.user_id == user_id, AIUsage.created_at >= month_start(now)
        )
    )
    return Decimal(total or 0)


def cost_of(model: str, usage: TokenUsage, *, batch: bool) -> Decimal:
    price = PRICES.get(model, UNKNOWN_PRICE)
    cost = (
        usage.input * price.input
        + usage.output * price.output
        + usage.cache_read * price.cache_read
        + usage.cache_write * price.cache_write
    ) / _MILLION
    if batch:
        cost *= BATCH_DISCOUNT
    return cost.quantize(Decimal("0.000001"))


def ensure_available(db: Session, settings: Settings, user_id: uuid.UUID, now: datetime) -> None:
    """Raise `AIUnavailable` before any call when AI is off or the month's cap is reached."""
    if not is_enabled(settings):
        raise AIUnavailable("disabled")
    if month_spend(db, user_id, now) >= settings.ai_monthly_budget_usd:
        raise AIUnavailable("budget")


def record_usage(
    db: Session,
    user_id: uuid.UUID,
    feature: str,
    model: str,
    usage: TokenUsage,
    *,
    batch: bool,
    now: datetime,
) -> AIUsage:
    """Log one call's tokens and cost. Flushed, not committed: `create_message` commits it
    right away; a batch's caller commits its own."""
    row = AIUsage(
        user_id=user_id,
        feature=feature,
        model=model,
        input_tokens=usage.input,
        output_tokens=usage.output,
        cache_read_tokens=usage.cache_read,
        cache_write_tokens=usage.cache_write,
        batch=batch,
        cost_usd=cost_of(model, usage, batch=batch),
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def billed_attempts(response: Any) -> list[tuple[str, TokenUsage]]:
    """Each billed attempt behind `response`, with the model it ran on. With the refusal
    fallback, `usage.iterations` is the per-attempt record: a declined attempt bills at its
    own model's rates, and the top-level usage covers only the attempt that answered."""
    iterations = getattr(response.usage, "iterations", None)
    if not iterations:
        return [(response.model, TokenUsage.of(response.usage))]
    return [
        (getattr(entry, "model", None) or response.model, TokenUsage.of(entry))
        for entry in iterations
    ]


def create_message(
    db: Session,
    settings: Settings,
    user_id: uuid.UUID,
    feature: str,
    now: datetime,
    **params: Any,
) -> Any:
    """One Messages API call for `feature`, after the key and cap checks. `params` are the
    request's own fields (`messages`, `system`, `max_tokens`, `output_config`, ...); the
    model comes from settings. Returns the SDK message; raises `AIUnavailable` instead of
    an SDK error. The usage rows (one per billed attempt) are written and committed whether
    or not Claude answered, so they survive anything that fails afterwards; that commit
    also commits whatever the caller had pending on `db`."""
    ensure_available(db, settings, user_id, now)
    client = make_client(settings)
    try:
        response = client.beta.messages.create(
            model=model_for(settings, feature),
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **params,
        )
    except (anthropic.APIError, WorkloadIdentityError) as exc:
        # The SDK already retried 429s, 5xx and connection errors.
        logger.warning("Claude call for {} failed: {}", feature, type(exc).__name__)
        raise AIUnavailable("error") from exc
    for model, usage in billed_attempts(response):
        record_usage(db, user_id, feature, model, usage, batch=False, now=now)
    db.commit()
    if response.stop_reason == "refusal":
        logger.warning("Claude declined the {} request", feature)
        raise AIUnavailable("refused")
    return response


def clean_text(text: str) -> str:
    """`text` without NUL, which Postgres text and JSONB can't hold and valid JSON can carry
    as \\u0000. For every string from Claude or the user that gets stored."""
    return text.replace("\x00", "")


def text_of(response: Any) -> str:
    """The first text block of a response, which structured output guarantees is the JSON."""
    return next((block.text for block in response.content if block.type == "text"), "")
