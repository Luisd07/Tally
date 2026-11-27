---
paths:
  - "api/**"
---

# API conventions

## Structure

- `routers/` handles HTTP only, `services/` holds logic, `models/` holds tables, `schemas/` holds Pydantic request/response models (subclass `app.schemas.base.Schema`).
- New routers go in `PROTECTED_ROUTERS` in `app/main.py`, which adds the `get_current_user` dependency. Only `/healthz` and the auth ceremony routes (`app.routers.auth`: enroll, signin, refresh, signout, attest, mounted at `/api/auth`) are open. `/api/auth/events` looks like it's under the open prefix but is its own router in `PROTECTED_ROUTERS`, protected the same way as everything else — don't add a route to `auth.router` unless it's meant to be open.
- Inside requests, get settings from the `AppSettings` dependency (`app.deps`), not `get_settings()`. Use the `DB` and `CurrentUser` dependencies for the session and user.

## Data

- Every table uses `UUIDPk` and `Timestamps` from `app.models.base`. Every data table has `user_id`, and every query filters on the current user's id. Use `app.scoping.get_owned` for a single row and `app.scoping.owned` for a list.
- Exception: `auth_events.user_id` is nullable (a failed sign-in can't be tied to a user), and `GET /api/auth/events` (#22) reads it unscoped on purpose, newest rows first regardless of owner. Don't "fix" that query to filter by `user_id` — it would hide the NULL-user rows the endpoint exists to show.
- Import each new model in `app/models/__init__.py` and generate a migration in the same change. `tests/test_migrations.py` fails if models and migrations drift.
- Money is `NUMERIC(12,2)` / `Decimal`, never float. Amounts follow SimpleFIN's sign: negative is money leaving the account.
- Enums live in `app/models/enums.py` (`Bucket`, `Source`, ...). `tax_line` stores a name, not a line number.

## Errors and logging

- Raise `AppError` subclasses from `app.errors` (`NotFound`, `Unauthorized`, `Conflict`) instead of `HTTPException`. All errors render as `{"error": {"code", "message", "request_id"}}`.
- Use `NotFound` both for missing rows and for rows owned by someone else, so the responses are identical.
- Log with `from loguru import logger`, never `print` or stdlib `logging`. The request ID is attached automatically.
- Exception: operator CLI output in `scripts/` (e.g. the enrollment code) goes to stdout with `print`, never loguru, so secrets stay out of the logs.

## Tests

- `client`: HTTP-only tests, no database needed.
- `db_session` / `db_client`: database tests. Each test runs in a transaction that is rolled back.
- `user` and `auth_headers` give a signed-in user.
- Auth tests use the `authenticator` fixture (`tests/softauthn.py`, a real P-256 software passkey). Never mock py_webauthn's verify functions.
- Warnings are errors (`filterwarnings = ["error"]`). Fix the cause instead of filtering the warning.
- `test_auth_required.py` checks that every `/api` route mounted through `PROTECTED_ROUTERS` returns 401 without a token. Coverage is derived from the routes themselves — it excludes whatever's actually mounted on the open `auth.router`, not a `/api/auth/` path prefix — so a new protected route gets this check for free even if it lives under `/api/auth/`.
