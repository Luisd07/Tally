import enum


class Bucket(enum.StrEnum):
    personal = "personal"
    business = "business"
    reimbursable = "reimbursable"
    transfer = "transfer"
    income = "income"


class Source(enum.StrEnum):
    simplefin = "simplefin"
    csv = "csv"
    manual = "manual"
    sheet_import = "sheet_import"


class AccountKind(enum.StrEnum):
    checking = "checking"
    credit_card = "credit_card"
    cash = "cash"
    loan = "loan"


class CategoryKind(enum.StrEnum):
    expense = "expense"
    income = "income"


class IncomeType(enum.StrEnum):
    event_pay = "event_pay"
    remote_hours = "remote_hours"
    reimbursement = "reimbursement"
    other = "other"


class Cadence(enum.StrEnum):
    weekly = "weekly"
    monthly = "monthly"
    quarterly = "quarterly"
    yearly = "yearly"


class SubscriptionKind(enum.StrEnum):
    subscription = "subscription"
    bill = "bill"


class ReimbursementStatus(enum.StrEnum):
    draft = "draft"
    submitted = "submitted"
    paid = "paid"


class AuthEventKind(enum.StrEnum):
    enroll = "enroll"
    enroll_failed = "enroll_failed"
    sign_in = "sign_in"
    sign_in_failed = "sign_in_failed"
    refresh = "refresh"
    refresh_reuse = "refresh_reuse"
    refresh_failed = "refresh_failed"
    sign_out = "sign_out"
    passkeys_replaced = "passkeys_replaced"
    attest = "attest"
    attest_failed = "attest_failed"


class ApnsEnvironment(enum.StrEnum):
    sandbox = "sandbox"
    production = "production"


class AppAttestEnvironment(enum.StrEnum):
    """The app's `com.apple.developer.devicecheck.appattest-environment` entitlement."""

    development = "development"
    production = "production"


class ChallengePurpose(enum.StrEnum):
    registration = "registration"
    authentication = "authentication"


class RunStatus(enum.StrEnum):
    running = "running"
    ok = "ok"
    partial = "partial"
    failed = "failed"


class RunTrigger(enum.StrEnum):
    scheduled = "scheduled"
    manual = "manual"


class MatchField(enum.StrEnum):
    """`merchant` is deliberately absent: `apply_rules` only ever runs on a sync's
    brand-new rows, and sync never sets `merchant` before rules run (#51 UB-1/
    MV-2), so a `merchant` rule could never match. The column stays (rather than
    being dropped) so a later path that supplies `merchant` before rules run
    (CSV import, #54) can reintroduce the member without a migration.
    """

    description = "description"


class MatchType(enum.StrEnum):
    contains = "contains"
    equals = "equals"
    starts_with = "starts_with"


class AIBatchStatus(enum.StrEnum):
    in_progress = "in_progress"
    ended = "ended"


class AISuggestionStatus(enum.StrEnum):
    """`pending` while its batch runs; `ready` once Claude answered; `failed` when the batch
    request errored or Claude declined, so the row isn't sent again."""

    pending = "pending"
    ready = "ready"
    failed = "failed"


class AIConfidence(enum.StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class ReceiptReadingStatus(enum.StrEnum):
    """`ready` once read; `retry` after a failed call (tried again, up to a limit); `failed`
    when Claude declined, the answer was unreadable, or the retries ran out."""

    ready = "ready"
    retry = "retry"
    failed = "failed"


class FindingKind(enum.StrEnum):
    """What a weekly review (#173) flagged about a transaction."""

    double_charge = "double_charge"
    price_increase = "price_increase"
    missing_receipt = "missing_receipt"
    unusual = "unusual"


class TaxReviewStatus(enum.StrEnum):
    requested = "requested"
    running = "running"
    ready = "ready"
    failed = "failed"


class TaxReviewKind(enum.StrEnum):
    """What a year-end tax review (#175) noted for the preparer."""

    missed_deduction = "missed_deduction"
    wrong_bucket = "wrong_bucket"
    missing_receipt = "missing_receipt"
    mileage_gap = "mileage_gap"
    other = "other"


class AIActionStatus(enum.StrEnum):
    """An AI-proposed change (#222). Nothing runs until the user confirms a `proposed` one."""

    proposed = "proposed"
    applied = "applied"
    dismissed = "dismissed"
    undone = "undone"
    failed = "failed"


class AIActionSource(enum.StrEnum):
    """Where an AI action was proposed: the money chat or an insight card."""

    chat = "chat"
    insight = "insight"


class InsightKind(enum.StrEnum):
    """A spending moment worth a heads-up (#225)."""

    pace = "pace"
    unusual_charge = "unusual_charge"
    new_subscription = "new_subscription"
    price_change = "price_change"
    promo_pace = "promo_pace"
