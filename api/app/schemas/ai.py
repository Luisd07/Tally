import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from app.models.enums import Bucket, FindingKind, TaxReviewKind, TaxReviewStatus
from app.schemas.ai_actions import AIActionOut
from app.schemas.base import Schema


class AIUsageOut(Schema):
    """This month's Claude spend against the cap. `enabled` is false when no key is set."""

    month: str
    spent_usd: Decimal
    budget_usd: Decimal
    enabled: bool


class ParseExpenseRequest(Schema):
    """`POST /api/ai/parse-expense` (#171): one short plain-language note."""

    text: str = Field(min_length=1, max_length=500)


class ExpenseDraft(Schema):
    """What the Add screen fills in from a note. Nothing is saved; any field may be empty."""

    direction: Literal["expense", "income"]
    # Always positive; the Add screen's direction sets the sign.
    amount: Decimal | None
    merchant: str | None
    description: str | None
    date: date
    category_id: uuid.UUID | None
    bucket: Bucket | None


class GuessMappingRequest(Schema):
    """`POST /api/ai/guess-mapping` (#172): a CSV's first records, header first. Only the
    first 6 non-blank ones are used."""

    records: list[Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=100)]] = (
        Field(min_length=1, max_length=25)
    )


class FindingTransaction(Schema):
    """Enough of a finding's transaction for Home to show it and open it."""

    id: uuid.UUID
    posted_date: date
    description_raw: str
    merchant: str | None
    amount: Decimal


class ReviewFindingOut(Schema):
    kind: FindingKind
    note: str
    # Null when the transaction has since been deleted.
    transaction: FindingTransaction | None


class WeeklyReviewOut(Schema):
    """The latest AI weekly review (#173) for Home."""

    week_start: date
    week_end: date
    summary: str
    findings: list[ReviewFindingOut]


class ChatRequest(Schema):
    """`POST /api/ai/chat` (#174). Send the `conversation_id` the last answer returned to
    carry on; leave it out (or send a new one) to start a chat. The reply's id may differ
    when the server had to start a new chat."""

    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=2000)
    # Whether this app build can show proposed changes; older builds leave it out.
    actions_supported: bool = False


class ChatReply(Schema):
    conversation_id: uuid.UUID
    reply: str
    # What the answer rests on, in plain words: "Spending by category, 2026-09".
    looked_up: list[str]
    # Changes proposed while answering this question, for the user to confirm or dismiss.
    actions: list[AIActionOut]


class TaxReviewRequest(Schema):
    """`POST /api/ai/tax-review` (#175)."""

    year: int = Field(ge=2000, le=2100)


class TaxReviewItemOut(Schema):
    kind: TaxReviewKind
    note: str
    # Null for a note not about one transaction, or once it's been deleted.
    transaction: FindingTransaction | None


class TaxReviewOut(Schema):
    """A year's AI review for the preparer. `requested` and `running` mean it's being
    written (poll again); `failed` carries `error`."""

    year: int
    status: TaxReviewStatus
    summary: str | None
    error: str | None
    updated_at: datetime
    items: list[TaxReviewItemOut]
