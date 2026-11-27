"""scripts/dev_token.py: a dev-only access token for the seeded user (issue #131)."""

import jwt
import pytest
from sqlalchemy.orm import Session

from app.auth.tokens import decode_access_token
from app.config import Settings
from app.models import User
from scripts import dev_token


def test_mint_returns_an_access_token_for_the_owner(
    db_session: Session, user: User, settings: Settings
) -> None:
    token = dev_token.mint(db_session, settings, minutes=480)

    assert decode_access_token(token, settings) == user.id


def test_mint_token_outlives_the_default_lifetime(
    db_session: Session, user: User, settings: Settings
) -> None:
    token = dev_token.mint(db_session, settings, minutes=480)
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["exp"] - claims["iat"] == 480 * 60


def test_mint_refuses_without_a_user(db_session: Session, settings: Settings) -> None:
    with pytest.raises(SystemExit, match="No user found"):
        dev_token.mint(db_session, settings, minutes=480)


def test_mint_refuses_in_production(db_session: Session, user: User, settings: Settings) -> None:
    production = settings.model_copy(update={"environment": "production"})

    with pytest.raises(SystemExit, match="production"):
        dev_token.mint(db_session, production, minutes=480)


def test_main_refuses_in_production(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    production = settings.model_copy(update={"environment": "production"})
    monkeypatch.setattr(dev_token, "get_settings", lambda: production)
    monkeypatch.setattr("sys.argv", ["dev_token"])

    with pytest.raises(SystemExit, match="production"):
        dev_token.main()
