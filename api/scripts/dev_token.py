"""Print an access token and a /dev preview link for the seeded user. Dev only.

Lets you click through the local web preview without a passkey ceremony. From api/:

    uv run python -m scripts.dev_token [--minutes 480]

Refuses with ENVIRONMENT=production, and never creates a user: run `scripts.seed_fake_data`
(or `scripts.enroll`) first. The token is a normal access token signed with the same key
the API uses, only with a longer lifetime than the 15-minute default.
"""

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.tokens import create_access_token
from app.config import Settings, get_settings
from app.db import make_engine
from app.models import User

PREVIEW_URL = "http://127.0.0.1:8000/dev/"


def mint(db: Session, settings: Settings, minutes: int) -> str:
    """An access token for the owner ("oldest user" rule, as in `get_or_create_user`)."""
    if settings.is_production:
        raise SystemExit("Refusing to mint a dev token with ENVIRONMENT=production.")
    user = db.scalars(select(User).order_by(User.created_at).limit(1)).first()
    if user is None:
        raise SystemExit("No user found. Run `uv run python -m scripts.seed_fake_data` first.")
    return create_access_token(
        user.id, settings.model_copy(update={"access_token_minutes": minutes})
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a dev access token and preview link.")
    parser.add_argument("--minutes", type=int, default=480, help="token lifetime (default 480)")
    args = parser.parse_args()

    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            token = mint(db, settings, args.minutes)
    finally:
        engine.dispose()
    # Plain stdout, not loguru: the token is for the operator's terminal, never the logs.
    print(f"Access token (valid {args.minutes} minutes):\n{token}\n")
    print(f"Open: {PREVIEW_URL}#token={token}")


if __name__ == "__main__":
    main()
