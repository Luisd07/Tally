"""Listing for `GET /api/auth/events` (#22).

Lists every row in `auth_events`, newest first, instead of scoping to the caller's
`user_id`. `auth_events.user_id` is nullable (#15): a failed sign-in from an unknown
credential, or a bad enrollment code, can't be tied to a user, and those rows are the
feed's documented reason to exist (#22's acceptance criterion). This is the one
exception to "every query filters by user_id" (`.claude/rules/api.md`); it doesn't use
`app.scoping`.
"""

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.models import AuthEvent
from app.schemas.auth_events import AuthEventListQuery, AuthEventOut, AuthEventPage, decode_cursor
from app.services.paging import paginate


def list_auth_events(db: Session, query: AuthEventListQuery) -> AuthEventPage:
    stmt = select(AuthEvent)
    if query.kind is not None:
        stmt = stmt.where(AuthEvent.kind == query.kind)
    if query.cursor is not None:
        cursor_created_at, cursor_id = decode_cursor(query.cursor)
        last = tuple_(cursor_created_at, cursor_id)
        stmt = stmt.where(tuple_(AuthEvent.created_at, AuthEvent.id) < last)

    stmt = stmt.order_by(AuthEvent.created_at.desc(), AuthEvent.id.desc())
    rows, next_cursor = paginate(db, stmt, query.limit, lambda row: (row.created_at, row.id))

    return AuthEventPage(
        items=[AuthEventOut.model_validate(row) for row in rows], next_cursor=next_cursor
    )
