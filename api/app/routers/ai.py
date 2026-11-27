import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.ratelimit import ai_limit, tax_review_limit
from app.config import APP_TIMEZONE
from app.deps import DB, AppSettings, CurrentUser
from app.errors import Conflict, ServiceUnavailable
from app.models.enums import AIActionStatus
from app.schemas.ai import (
    AIUsageOut,
    ChatReply,
    ChatRequest,
    ExpenseDraft,
    FindingTransaction,
    GuessMappingRequest,
    ParseExpenseRequest,
    ReviewFindingOut,
    TaxReviewItemOut,
    TaxReviewOut,
    TaxReviewRequest,
    WeeklyReviewOut,
)
from app.schemas.ai_actions import AIActionOut
from app.schemas.imports import ImportMappingIn
from app.services import ai as service
from app.services import ai_actions, ai_capture, ai_chat, ai_mapping, tax_review, weekly_review

router = APIRouter()


@router.get("/usage")
def ai_usage(db: DB, settings: AppSettings, user: CurrentUser) -> AIUsageOut:
    """The month's Claude spend so far and the cap, for the More screen (#168)."""
    now = datetime.now(UTC)
    return AIUsageOut(
        month=now.astimezone(APP_TIMEZONE).strftime("%Y-%m"),
        spent_usd=service.month_spend(db, user.id, now).quantize(service.CENT),
        budget_usd=settings.ai_monthly_budget_usd.quantize(service.CENT),
        enabled=service.is_enabled(settings),
    )


@router.get("/weekly-review")
def latest_weekly_review(db: DB, user: CurrentUser) -> WeeklyReviewOut | None:
    """The newest weekly review (#173), or null before the first one."""
    found = weekly_review.latest(db, user.id)
    if found is None:
        return None
    review, findings = found
    return WeeklyReviewOut(
        week_start=review.week_start,
        week_end=review.week_end,
        summary=review.summary,
        findings=[
            ReviewFindingOut(
                kind=finding.kind,
                note=finding.note,
                transaction=FindingTransaction.model_validate(transaction) if transaction else None,
            )
            for finding, transaction in findings
        ],
    )


def _tax_review_out(db: DB, user_id: uuid.UUID, year: int) -> TaxReviewOut | None:
    found = tax_review.get(db, user_id, year)
    if found is None:
        return None
    review, items = found
    return TaxReviewOut(
        year=review.year,
        status=review.status,
        summary=review.summary,
        error=review.error,
        updated_at=review.updated_at,
        items=[
            TaxReviewItemOut(
                kind=item.kind,
                note=item.note,
                transaction=FindingTransaction.model_validate(transaction) if transaction else None,
            )
            for item, transaction in items
        ],
    )


@router.post(
    "/tax-review", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(tax_review_limit)]
)
def request_tax_review(
    body: TaxReviewRequest, db: DB, settings: AppSettings, user: CurrentUser
) -> TaxReviewOut:
    """Ask for a year-end review for the preparer (#175). The worker writes it within a
    couple of minutes; poll `GET /api/ai/tax-review`. 503 when AI is off, 409 while one is
    already being written."""
    if not service.is_enabled(settings):
        raise ServiceUnavailable(_UNAVAILABLE["disabled"])
    tax_review.request(db, user.id, body.year, datetime.now(UTC))
    db.commit()
    out = _tax_review_out(db, user.id, body.year)
    assert out is not None
    return out


@router.get("/tax-review")
def get_tax_review(year: int, db: DB, user: CurrentUser) -> TaxReviewOut | None:
    """The year's review and its state, or null before one is asked for."""
    return _tax_review_out(db, user.id, year)


@router.post("/parse-expense", dependencies=[Depends(ai_limit)])
def parse_expense(
    body: ParseExpenseRequest, db: DB, settings: AppSettings, user: CurrentUser
) -> ExpenseDraft:
    """A draft expense from plain text, for the Add screen to fill in (#171). Saves nothing
    but the usage row. 503 when AI is off, over budget or didn't answer."""
    try:
        return ai_capture.parse_expense(db, settings, user.id, body.text, datetime.now(UTC))
    except service.AIUnavailable as exc:
        raise ServiceUnavailable(_UNAVAILABLE[exc.reason]) from exc


@router.post("/guess-mapping", dependencies=[Depends(ai_limit)])
def guess_mapping(
    body: GuessMappingRequest, db: DB, settings: AppSettings, user: CurrentUser
) -> ImportMappingIn:
    """A CSV column mapping for the user to review before saving (#172). Saves nothing but
    the usage row. 503 when AI is off or didn't answer; 422 `no_guess` when its answer
    doesn't fit the file."""
    try:
        return ai_mapping.guess_mapping(db, settings, user.id, body.records, datetime.now(UTC))
    except service.AIUnavailable as exc:
        raise ServiceUnavailable(_UNAVAILABLE[exc.reason]) from exc


# Exclude unset: a proposed step's patch lists only the fields it changes.
@router.post("/chat", dependencies=[Depends(ai_limit)], response_model_exclude_unset=True)
def chat(body: ChatRequest, db: DB, settings: AppSettings, user: CurrentUser) -> ChatReply:
    """Ask about your money (#174): Claude answers with lookups of your data, and may
    propose changes for you to confirm (#223); it changes nothing itself.
    503 when AI is off, over budget or didn't answer; 409 when the chat is full."""
    try:
        answer = ai_chat.ask(
            db,
            settings,
            user.id,
            body.conversation_id,
            body.message,
            datetime.now(UTC),
            body.actions_supported,
        )
    except service.AIUnavailable as exc:
        raise ServiceUnavailable(_UNAVAILABLE[exc.reason]) from exc
    db.commit()
    seen: list[str] = []
    for call in answer.tool_calls:
        if (text := ai_chat.describe(call)) not in seen:
            seen.append(text)
    return ChatReply(
        conversation_id=answer.conversation_id,
        reply=answer.text,
        looked_up=seen,
        actions=[AIActionOut.model_validate(action) for action in answer.actions],
    )


# A step's patch lists only the fields it changes, so an unset field isn't sent as null.
@router.get("/actions", response_model_exclude_unset=True)
def list_actions(
    db: DB,
    user: CurrentUser,
    status_: Annotated[AIActionStatus | None, Query(alias="status")] = None,
) -> list[AIActionOut]:
    """Changes Claude proposed (#222), newest first, at most 100."""
    return [
        AIActionOut.model_validate(action)
        for action in ai_actions.list_actions(db, user.id, status_)
    ]


@router.get("/actions/{id}", response_model_exclude_unset=True)
def get_action(id: uuid.UUID, db: DB, user: CurrentUser) -> AIActionOut:
    return AIActionOut.model_validate(ai_actions.get_action(db, user.id, id))


@router.post("/actions/{id}/confirm", response_model_exclude_unset=True)
def confirm_action(id: uuid.UUID, db: DB, user: CurrentUser) -> AIActionOut:
    """Apply every step of a proposed action at once. 409 when it isn't `proposed`, or
    when a step fails: then nothing is applied and the action is kept as `failed`."""
    action = ai_actions.confirm_action(db, user.id, id, datetime.now(UTC))
    db.commit()
    if action.status == AIActionStatus.failed:
        raise Conflict(action.error)
    return AIActionOut.model_validate(action)


@router.post("/actions/{id}/dismiss", response_model_exclude_unset=True)
def dismiss_action(id: uuid.UUID, db: DB, user: CurrentUser) -> AIActionOut:
    """Decline a proposed action. 409 when it isn't `proposed`."""
    action = ai_actions.dismiss_action(db, user.id, id)
    db.commit()
    return AIActionOut.model_validate(action)


@router.post("/actions/{id}/undo", response_model_exclude_unset=True)
def undo_action(id: uuid.UUID, db: DB, user: CurrentUser) -> AIActionOut:
    """Revert an applied action. 409 when it isn't `applied`, or when something it changed
    was edited or removed since: then nothing is changed."""
    action = ai_actions.undo_action(db, user.id, id)
    db.commit()
    return AIActionOut.model_validate(action)


_UNAVAILABLE = {
    "disabled": "AI assist is off.",
    "budget": "AI assist has reached this month's budget.",
    "error": "AI assist didn't answer. Try again.",
    "refused": "AI assist couldn't read that. Fill it in by hand.",
}
