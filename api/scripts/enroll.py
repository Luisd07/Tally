"""Print a one-time passkey enrollment code, valid for 10 minutes.

Also the recovery path when the passkey is lost. Creates the user on the first run. On the VM:

    docker compose exec api python -m scripts.enroll [--name "Your Name"] [--replace]

Locally, from api/: uv run python -m scripts.enroll

--replace deletes the owner's existing passkeys and revokes their refresh tokens in the
same transaction as the new code, for a lost or compromised phone. Revocation happens when
the script runs, not when the new enrollment finishes: if the enrollment is abandoned,
re-run the script. Access tokens expire on their own (15 minutes) and aren't tracked here.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.auth import events
from app.auth.enrollment import CODE_TTL, get_or_create_user, issue_code_for_owner
from app.config import get_settings
from app.db import make_engine

SCRIPT_USER_AGENT = "scripts.enroll"


@dataclass(frozen=True)
class _Result:
    code: str
    replaced: bool = False
    passkeys_deleted: int = 0
    tokens_revoked: int = 0


def _run(db: Session, args: argparse.Namespace, now: datetime) -> _Result:
    passkeys_deleted = tokens_revoked = 0
    if args.replace:
        user = get_or_create_user(db, args.name)
        client = events.ClientInfo(ip=None, user_agent=SCRIPT_USER_AGENT)
        passkeys_deleted, tokens_revoked = events.replace_owner_credentials(
            db, client, user.id, now
        )
    code = issue_code_for_owner(db, args.name, now)
    return _Result(code, args.replace, passkeys_deleted, tokens_revoked)


def _print(result: _Result, minutes: int) -> None:
    if result.replaced:
        print(
            f"Revoked {result.passkeys_deleted} passkey(s) and "
            f"{result.tokens_revoked} refresh token(s)."
        )
    # Plain stdout, not loguru: the code is for the operator's terminal, never the logs.
    print(f"Enrollment code: {result.code}  (valid for {minutes} minutes, works once)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a one-time passkey enrollment code.")
    parser.add_argument("--name", default="Tally", help="display name if the user is new")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="revoke the owner's existing passkeys and refresh tokens first",
    )
    args = parser.parse_args()

    engine = make_engine(get_settings())
    try:
        with Session(engine) as db:
            result = _run(db, args, datetime.now(UTC))
            db.commit()
    finally:
        engine.dispose()
    _print(result, int(CODE_TTL.total_seconds() // 60))


if __name__ == "__main__":
    main()
