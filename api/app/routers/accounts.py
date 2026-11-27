import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.deps import DB, CurrentUser
from app.routers import status_for_create
from app.schemas.accounts import (
    AccountCreate,
    AccountOut,
    AccountUpdate,
    BalanceSnapshotOut,
    BalanceUpsert,
    CardLogOut,
    CardLogQuery,
    CardOut,
    InterestSummaryOut,
    LoanOut,
    PayoffPlanOut,
    PayoffQuery,
)
from app.services import accounts

router = APIRouter()


@router.get("")
def list_accounts(db: DB, user: CurrentUser, active: bool | None = None) -> list[AccountOut]:
    rows = accounts.list_accounts(db, user.id, active)
    return [AccountOut.model_validate(row) for row in rows]


# Registered ahead of the `/{id}...` routes below: a literal path segment, so it can
# never actually be captured by them, but `test_cards_route_is_not_captured_by_id`
# pins that down instead of leaving it to route-registration order alone.
@router.get("/cards")
def list_cards(db: DB, user: CurrentUser, include_inactive: bool = False) -> list[CardOut]:
    return accounts.list_cards(db, user.id, include_inactive)


@router.get("/loans")
def list_loans(db: DB, user: CurrentUser) -> list[LoanOut]:
    return accounts.list_loans(db, user.id)


@router.get("/interest")
def get_interest_summary(db: DB, user: CurrentUser) -> InterestSummaryOut:
    return accounts.get_interest_summary(db, user.id)


@router.get("/payoff")
def get_payoff_plan(
    query: Annotated[PayoffQuery, Query()], db: DB, user: CurrentUser
) -> PayoffPlanOut:
    return accounts.get_payoff_plan(db, user.id, query.budget)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(
    body: AccountCreate, db: DB, user: CurrentUser, response: Response
) -> AccountOut:
    account, created = accounts.create_account(db, user.id, body)
    db.commit()
    status_for_create(response, created)
    return AccountOut.model_validate(account)


@router.patch("/{id}")
def update_account(id: uuid.UUID, body: AccountUpdate, db: DB, user: CurrentUser) -> AccountOut:
    account = accounts.update_account(db, user.id, id, body)
    db.commit()
    return AccountOut.model_validate(account)


@router.put("/{id}/balances/{date}")
def upsert_balance(
    id: uuid.UUID, date: dt.date, body: BalanceUpsert, db: DB, user: CurrentUser
) -> BalanceSnapshotOut:
    row = accounts.upsert_balance(db, user.id, id, date, body.balance, dt.datetime.now(dt.UTC))
    db.commit()
    return BalanceSnapshotOut.model_validate(row)


@router.get("/{id}/balances")
def list_balances(
    id: uuid.UUID,
    db: DB,
    user: CurrentUser,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: dt.date | None = None,
) -> list[BalanceSnapshotOut]:
    rows = accounts.get_balance_history(db, user.id, id, from_, to)
    return [BalanceSnapshotOut.model_validate(row) for row in rows]


@router.get("/{id}/card-log")
def get_card_log(
    id: uuid.UUID, query: Annotated[CardLogQuery, Query()], db: DB, user: CurrentUser
) -> CardLogOut:
    return accounts.get_card_log(db, user.id, id, query.month)
