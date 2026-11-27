import base64
import uuid
from datetime import UTC, datetime
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Request, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import app_attest, events, passkeys, tokens
from app.auth.errors import CeremonyFailed
from app.auth.ratelimit import (
    ENROLL_LIMITS,
    REFRESH_LIMITS,
    enroll_limit,
    rate_limit,
    refresh_limit,
    signin_limit,
    signout_limit,
)
from app.deps import DB, AppSettings, no_store
from app.errors import Unauthorized
from app.models import RefreshToken
from app.models.enums import AuthEventKind
from app.net import client_ip
from app.schemas.auth import (
    AttestChallenge,
    AttestedKeyOut,
    AttestRequest,
    CeremonyOptions,
    EnrollOptionsRequest,
    EnrollVerifyRequest,
    PasskeyOut,
    RefreshRequest,
    SignInVerifyRequest,
    TokenResponse,
)

router = APIRouter()

# Every sign-in and refresh fetches a challenge, so it gets the refresh route's allowance.
attest_challenge_limit = rate_limit("attest_challenge", *REFRESH_LIMITS)
attest_limit = rate_limit("attest", *ENROLL_LIMITS)


def _client(request: Request) -> events.ClientInfo:
    return events.ClientInfo(
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def _refuse(
    db: Session, client: events.ClientInfo, kind: AuthEventKind, exc: CeremonyFailed
) -> NoReturn:
    """Map a refused ceremony to a bare 401 that doesn't say which check failed."""
    events.refuse(db, client, kind, exc)
    logger.warning("{} refused: {}", kind, exc.reason)
    raise Unauthorized() from exc


def _refuse_refresh(
    db: Session, client: events.ClientInfo, exc: tokens.RefreshRejected
) -> NoReturn:
    """Map a refused refresh to a bare 401 that doesn't say which check failed."""
    is_reuse = isinstance(exc, tokens.RefreshReused)
    kind = AuthEventKind.refresh_reuse if is_reuse else AuthEventKind.refresh_failed
    events.record(db, client, kind, exc.user_id)
    db.commit()
    if is_reuse:
        logger.warning("refresh token reuse detected")
    raise Unauthorized() from exc


async def _raw_body(request: Request) -> bytes:
    """The body exactly as sent, which is what an App Attest assertion signs."""
    return await request.body()


def _token_owner(db: Session, token: str) -> uuid.UUID | None:
    return db.scalar(
        select(RefreshToken.user_id).where(
            RefreshToken.token_hash == tokens.hash_refresh_token(token)
        )
    )


@router.post("/enroll/options", dependencies=[Depends(enroll_limit)])
def enroll_options(
    body: EnrollOptionsRequest, request: Request, db: DB, settings: AppSettings
) -> CeremonyOptions:
    client = _client(request)
    rp = passkeys.RelyingParty.from_settings(settings)
    try:
        pending = passkeys.start_enrollment(db, rp, body.code, datetime.now(UTC))
    except CeremonyFailed as exc:
        _refuse(db, client, AuthEventKind.enroll_failed, exc)
    db.commit()
    return CeremonyOptions(challenge_id=pending.challenge_id, options=pending.options)


@router.post(
    "/enroll/verify", status_code=status.HTTP_201_CREATED, dependencies=[Depends(enroll_limit)]
)
def enroll_verify(
    body: EnrollVerifyRequest, request: Request, db: DB, settings: AppSettings
) -> PasskeyOut:
    client = _client(request)
    rp = passkeys.RelyingParty.from_settings(settings)
    attempt = passkeys.RegistrationAttempt(body.challenge_id, body.credential, body.device_name)
    try:
        passkey = passkeys.finish_enrollment(db, rp, attempt, datetime.now(UTC))
    except CeremonyFailed as exc:
        _refuse(db, client, AuthEventKind.enroll_failed, exc)
    events.record(db, client, AuthEventKind.enroll, passkey.user_id)
    db.commit()
    logger.info("Passkey enrolled")
    return PasskeyOut.model_validate(passkey)


@router.post("/signin/options", dependencies=[Depends(signin_limit)])
def signin_options(db: DB, settings: AppSettings) -> CeremonyOptions:
    rp = passkeys.RelyingParty.from_settings(settings)
    pending = passkeys.start_sign_in(db, rp, datetime.now(UTC))
    db.commit()
    return CeremonyOptions(challenge_id=pending.challenge_id, options=pending.options)


@router.post("/signin/verify", dependencies=[Depends(no_store), Depends(signin_limit)])
def signin_verify(
    body: SignInVerifyRequest,
    request: Request,
    db: DB,
    settings: AppSettings,
    raw_body: Annotated[bytes, Depends(_raw_body)],
) -> TokenResponse:
    client = _client(request)
    now = datetime.now(UTC)
    rp = passkeys.RelyingParty.from_settings(settings)
    attempt = passkeys.SignInAttempt(body.challenge_id, body.credential)
    try:
        key = app_attest.check_request(db, settings, request.headers, raw_body, now)
        passkey = passkeys.finish_sign_in(db, rp, attempt, now)
        if key is not None:
            app_attest.bind_to_user(db, key, passkey.user_id)
    except CeremonyFailed as exc:
        _refuse(db, client, AuthEventKind.sign_in_failed, exc)
    session = tokens.issue_session(db, passkey.user_id, settings, now)
    events.record(db, client, AuthEventKind.sign_in, passkey.user_id)
    db.commit()
    logger.info("Signed in")
    return TokenResponse(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        expires_in=session.expires_in,
    )


@router.post("/refresh", dependencies=[Depends(no_store), Depends(refresh_limit)])
def refresh(
    body: RefreshRequest,
    request: Request,
    db: DB,
    settings: AppSettings,
    raw_body: Annotated[bytes, Depends(_raw_body)],
) -> TokenResponse:
    client = _client(request)
    now = datetime.now(UTC)
    # Everything that can refuse for App Attest runs before the rotation: refusing after it
    # would commit the old token's revocation and strand the app without the new one.
    try:
        key = app_attest.check_request(db, settings, request.headers, raw_body, now)
        owner = _token_owner(db, body.refresh_token)
        if key is not None and owner is not None:
            app_attest.ensure_bindable(key, owner)
    except CeremonyFailed as exc:
        _refuse(db, client, AuthEventKind.refresh_failed, exc)
    try:
        rotated = tokens.rotate_refresh_token(db, body.refresh_token, settings, now)
    except tokens.RefreshRejected as exc:
        _refuse_refresh(db, client, exc)
    if key is not None:
        app_attest.bind_to_user(db, key, rotated.user_id)
    events.record(db, client, AuthEventKind.refresh, rotated.user_id)
    db.commit()
    return TokenResponse(
        access_token=rotated.session.access_token,
        refresh_token=rotated.session.refresh_token,
        expires_in=rotated.session.expires_in,
    )


@router.post(
    "/signout", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(signout_limit)]
)
def signout(body: RefreshRequest, request: Request, db: DB) -> None:
    client = _client(request)
    now = datetime.now(UTC)
    user_id = tokens.revoke_refresh_token_family(db, body.refresh_token, now)
    if user_id is not None:
        events.record(db, client, AuthEventKind.sign_out, user_id)
    db.commit()


@router.get("/attest/challenge", dependencies=[Depends(no_store), Depends(attest_challenge_limit)])
def attest_challenge(db: DB) -> AttestChallenge:
    row = app_attest.issue_challenge(db, datetime.now(UTC))
    db.commit()
    return AttestChallenge(challenge_id=row.id, challenge=base64.b64encode(row.challenge).decode())


@router.post("/attest", status_code=status.HTTP_201_CREATED, dependencies=[Depends(attest_limit)])
def attest(body: AttestRequest, request: Request, db: DB, settings: AppSettings) -> AttestedKeyOut:
    client = _client(request)
    attempt = app_attest.AttestationAttempt(body.challenge_id, body.key_id, body.attestation_object)
    try:
        key = app_attest.verify_attestation(db, settings, attempt, datetime.now(UTC))
    except CeremonyFailed as exc:
        _refuse(db, client, AuthEventKind.attest_failed, exc)
    # The key has no user until a sign-in binds it.
    events.record(db, client, AuthEventKind.attest)
    db.commit()
    logger.info("App Attest key attested")
    return AttestedKeyOut(key_id=base64.b64encode(key.key_id).decode(), created_at=key.created_at)
