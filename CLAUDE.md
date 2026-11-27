# Tally

Private, single-user expense tracker: SwiftUI iPhone app (`ios/`) talking to a FastAPI + PostgreSQL server (`api/`), built to deploy with Docker Compose behind Caddy on one GCP VM, synced nightly from SimpleFIN Bridge. The hosted instance was shut down in November 2026 and the repo is now a portfolio piece: there is no production server, so local development with fake data is the only way to run it (deployment notes: `docs/deployment.md`). Area-specific conventions live in `.claude/rules/`.

## Scope

- This is a personal app for one user. Don't add multi-tenant, signup, billing or other public-launch features unless asked.
- Work is planned in GitHub Project "Tally" (github.com/users/Luisd07/projects/3): milestones M1–M8, one parent issue per milestone, features as sub-issues. Reference the issue number when working on one.

## Commands

All Python commands run from `api/` through uv:

```sh
uv sync                                               # install deps
uv run uvicorn --factory app.main:create_app --reload # dev server, docs at /docs
uv run pytest                                         # tests (needs dev Postgres)
uv run ruff check . && uv run ruff format .           # lint + format
uv run pyright                                        # type check
uv run alembic revision --autogenerate -m "..."       # new migration
uv run alembic upgrade head
uv run python -m scripts.enroll                       # one-time passkey enrollment code (VM: docker compose exec api python -m scripts.enroll)
```

iOS, from the repo root (simulator "iPhone 17 Pro"):

```sh
xcodebuild -project ios/Tally.xcodeproj -scheme Tally -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build
xcodebuild -project ios/Tally.xcodeproj -scheme Tally -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
# local API for the simulator, from api/ (token: uv run python -m scripts.dev_token)
uv run uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8000
# launch signed in, DEBUG only; the app must be installed on the booted simulator (run it from Xcode once)
SIMCTL_CHILD_TALLY_DEV_TOKEN=<token> xcrun simctl launch booted com.luisd07.tally
# signed in, the app opens behind the Face ID gate: enroll once with `xcrun simctl spawn booted notifyutil -s com.apple.BiometricKit.enrollmentChanged 1 && xcrun simctl spawn booted notifyutil -p com.apple.BiometricKit.enrollmentChanged`, then pass it with `xcrun simctl spawn booted notifyutil -p com.apple.BiometricKit_Sim.pearl.match`
```

Dev Postgres, from the repo root:

```sh
docker compose -f docker-compose.yml -f compose.dev.yml up -d db
```

## Environment gotchas

- Dev Postgres is on host port **5433**, not 5432; another project's container holds 5432 on this machine.
- Use `127.0.0.1`, never `localhost`, in database URLs and for the simulator's server URL. `localhost` can resolve to IPv6 first and stall.
- Running the API from `api/` does not load the repo-root `.env`, so local runs use the `config.py` defaults (DB at `127.0.0.1:5433`, `ENVIRONMENT=development`).
- Always include `-f compose.dev.yml` when running `docker compose up` locally. Without it, Compose recreates `db` without the published port and tests fail with a connection timeout.
- The API runs through the factory (`--factory app.main:create_app`); there is no module-level `app` object.

## Git and GitHub

- `master` is protected: changes land through a PR from `dev` (or a branch off it) with the `api` and `docker` CI checks passing. Never commit directly on `master`.
- Commit titles are short conventional commits (`feat: ...`, `fix: ...`, `chore: ...`, `test: ...`, `docs: ...`, `ci: ...`).
- IMPORTANT: Never add `Co-Authored-By` trailers, "Generated with" lines or chat/session links to commits or PR bodies.
- Pre-commit runs gitleaks, ruff, pyright and the uv lock check. Fix failures instead of skipping hooks.
- Pin GitHub Actions to a commit SHA with a version comment (`uses: owner/action@<sha> # vX.Y.Z`). Some actions publish no floating major tag.

## Secrets

- Never commit `.env`, `*.p8` or real keys. `.env.example` holds placeholders only.
- Secret settings are `SecretStr`. Read them with `.get_secret_value()` only where they're used, and never log them.
- With `ENVIRONMENT=production`, settings refuse placeholder secrets. Keep that check in step when adding new secret settings.
