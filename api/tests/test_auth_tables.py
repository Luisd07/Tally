import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import tokens
from app.models import AuthEvent, Device, Passkey, RefreshToken, User
from app.models.enums import ApnsEnvironment, AuthEventKind


def _passkey(user: User, credential_id: bytes) -> Passkey:
    return Passkey(
        user_id=user.id, credential_id=credential_id, public_key=b"\x01" * 77, sign_count=0
    )


def _refresh_token(user: User, token_hash: str, family_id: uuid.UUID) -> RefreshToken:
    return RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        family_id=family_id,
        expires_at=datetime.now(UTC) + timedelta(days=60),
    )


def _device(user: User, apns_token: str) -> Device:
    return Device(
        user_id=user.id,
        apns_token=apns_token,
        environment=ApnsEnvironment.sandbox,
        last_seen_at=datetime.now(UTC),
    )


def test_passkey_round_trips_bytes_and_large_sign_count(db_session: Session, user: User) -> None:
    credential_id = bytes(range(64))
    passkey = _passkey(user, credential_id)
    passkey.sign_count = 2**32 - 1
    db_session.add(passkey)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.scalars(select(Passkey).where(Passkey.credential_id == credential_id)).one()
    assert found.credential_id == credential_id
    assert found.sign_count == 2**32 - 1


def test_duplicate_credential_id_is_rejected(db_session: Session, user: User) -> None:
    db_session.add(_passkey(user, b"same-credential"))
    db_session.flush()
    db_session.add(_passkey(user, b"same-credential"))
    with pytest.raises(IntegrityError, match="uq_passkeys_credential_id"):
        db_session.flush()


def test_duplicate_token_hash_is_rejected(db_session: Session, user: User) -> None:
    _, token_hash = tokens.new_refresh_token()
    db_session.add(_refresh_token(user, token_hash, uuid.uuid4()))
    db_session.flush()
    db_session.add(_refresh_token(user, token_hash, uuid.uuid4()))
    with pytest.raises(IntegrityError, match="uq_refresh_tokens_token_hash"):
        db_session.flush()


def test_tokens_in_a_family_share_family_id(db_session: Session, user: User) -> None:
    family_id = uuid.uuid4()
    for _ in range(3):
        db_session.add(_refresh_token(user, tokens.new_refresh_token()[1], family_id))
    db_session.add(_refresh_token(user, tokens.new_refresh_token()[1], uuid.uuid4()))
    db_session.flush()

    family = db_session.scalars(select(RefreshToken).where(RefreshToken.family_id == family_id))
    assert len(family.all()) == 3


def test_duplicate_apns_token_is_rejected(db_session: Session, user: User) -> None:
    db_session.add(_device(user, "a" * 64))
    db_session.flush()
    db_session.add(_device(user, "a" * 64))
    with pytest.raises(IntegrityError, match="uq_devices_apns_token"):
        db_session.flush()


def test_auth_event_saves_without_a_user(db_session: Session) -> None:
    event = AuthEvent(kind=AuthEventKind.sign_in_failed, ip="203.0.113.7", user_agent="Tally/1.0")
    db_session.add(event)
    db_session.flush()
    db_session.expunge_all()

    found = db_session.get_one(AuthEvent, event.id)
    assert found.user_id is None
    assert found.kind is AuthEventKind.sign_in_failed


def test_deleting_a_user_cascades_but_keeps_auth_events(db_session: Session, user: User) -> None:
    db_session.add_all(
        [
            _passkey(user, b"credential"),
            _refresh_token(user, tokens.new_refresh_token()[1], uuid.uuid4()),
            _device(user, "b" * 64),
            AuthEvent(user_id=user.id, kind=AuthEventKind.enroll),
        ]
    )
    db_session.flush()
    db_session.delete(user)
    db_session.flush()
    db_session.expunge_all()

    for model in (Passkey, RefreshToken, Device):
        assert db_session.scalars(select(model)).all() == []
    event = db_session.scalars(select(AuthEvent)).one()
    assert event.user_id is None


@pytest.mark.parametrize(
    ("table", "index_name", "columns"),
    [
        ("refresh_tokens", "ix_refresh_tokens_family_id", ["family_id"]),
        ("auth_events", "ix_auth_events_created_at", ["created_at"]),
    ],
)
def test_lookup_indexes_exist(
    db_session: Session, table: str, index_name: str, columns: list[str]
) -> None:
    indexes = inspect(db_session.connection()).get_indexes(table)
    assert {"name": index_name, "column_names": columns} in [
        {"name": ix["name"], "column_names": ix["column_names"]} for ix in indexes
    ]
