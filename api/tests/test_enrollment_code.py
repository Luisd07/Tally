import re
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.orm import Session

from app.auth.enrollment import CODE_TTL, hash_code, issue_code, issue_code_for_owner, redeem_code
from app.models import EnrollmentCode, User
from tests.locks import wait_until_blocked_on_lock


def test_code_is_grouped_and_unambiguous(db_session: Session, user: User) -> None:
    code = issue_code(db_session, user.id, datetime.now(UTC))
    assert re.fullmatch(r"[A-HJKMNP-TW-Z2-9]{4}(-[A-HJKMNP-TW-Z2-9]{4}){2}", code)


def test_only_the_hash_is_stored(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    code = issue_code(db_session, user.id, now)

    row = db_session.scalars(select(EnrollmentCode)).one()
    assert row.code_hash == hash_code(code)
    assert code.replace("-", "") not in row.code_hash.upper()
    assert row.expires_at == now + CODE_TTL
    assert row.used_at is None


def test_valid_code_redeems_once(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    code = issue_code(db_session, user.id, now)

    assert redeem_code(db_session, code, now) == user.id
    assert redeem_code(db_session, code, now) is None
    assert db_session.scalars(select(EnrollmentCode.used_at)).one() == now


def test_code_can_be_typed_loosely(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    code = issue_code(db_session, user.id, now)
    assert redeem_code(db_session, f" {code.replace('-', '').lower()} ", now) == user.id


def test_expired_code_is_rejected(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    code = issue_code(db_session, user.id, now)
    assert redeem_code(db_session, code, now + CODE_TTL) is None
    assert redeem_code(db_session, code, now + CODE_TTL - timedelta(seconds=1)) == user.id


def test_unknown_code_is_rejected(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    issue_code(db_session, user.id, now)
    assert redeem_code(db_session, "AAAA-BBBB-CCCC", now) is None


def test_new_code_replaces_unused_ones(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    used = issue_code(db_session, user.id, now)
    redeem_code(db_session, used, now)
    old = issue_code(db_session, user.id, now)
    new = issue_code(db_session, user.id, now)

    assert redeem_code(db_session, old, now) is None
    assert redeem_code(db_session, new, now) == user.id
    # Used codes stay as a record.
    assert len(db_session.scalars(select(EnrollmentCode)).all()) == 2


def test_issue_code_for_owner_creates_the_user_once(db_session: Session) -> None:
    now = datetime.now(UTC)
    first = issue_code_for_owner(db_session, "Luis", now)
    second = issue_code_for_owner(db_session, "Someone Else", now)

    user = db_session.scalars(select(User)).one()
    assert user.display_name == "Luis"
    assert redeem_code(db_session, first, now) is None
    assert redeem_code(db_session, second, now) == user.id


def test_issue_code_for_owner_attaches_to_existing_user(db_session: Session, user: User) -> None:
    now = datetime.now(UTC)
    code = issue_code_for_owner(db_session, "Ignored", now)
    assert db_session.scalars(select(User)).one() == user
    assert redeem_code(db_session, code, now) == user.id


@pytest.fixture
def committed_code(engine: Engine) -> Iterator[tuple[uuid.UUID, str, datetime]]:
    """A user and code committed to the real engine, visible to the two sessions in the race."""
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    with Session(engine) as setup:
        setup.add(User(id=user_id, display_name="Race"))
        setup.flush()
        code = issue_code(setup, user_id, now)
        setup.commit()
    try:
        yield user_id, code, now
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()


def test_concurrent_redeems_succeed_only_once(
    engine: Engine, committed_code: tuple[uuid.UUID, str, datetime]
) -> None:
    """Needs committed rows, so it runs outside the rolled-back test transaction."""
    user_id, code, now = committed_code
    with Session(engine) as first, Session(engine) as second:
        assert redeem_code(first, code, now) == user_id  # holds the row lock, uncommitted

        second_pid = second.scalar(text("SELECT pg_backend_pid()"))
        assert second_pid is not None
        result: list[uuid.UUID | None] = []
        racer = threading.Thread(target=lambda: result.append(redeem_code(second, code, now)))
        racer.start()
        wait_until_blocked_on_lock(engine, second_pid)
        first.commit()
        racer.join(timeout=10)

        assert result == [None]
