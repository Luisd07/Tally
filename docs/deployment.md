# Deployment

This is how Tally ran in production from October to November 2026: one small Google Cloud VM (e2-small, Debian 12) running Docker Compose, with Caddy terminating HTTPS in front of the API. That instance has since been shut down. The notes are kept as a record of the setup and work for a fresh one.

The shape around the VM:

- A static IP, with a DNS A record for the API domain pointing at it. The domain is also the passkey relying-party ID, so it has to be permanent.
  The repo carries placeholders, not a real domain or Apple Team ID. For your own instance, set `API_DOMAIN`, `WEBAUTHN_RP_ID`, `WEBAUTHN_ORIGIN` and `APNS_TEAM_ID` in `.env` (the `Caddyfile` reads the domain and the Team ID from there for HTTPS and the passkey association file), and in the iOS app set the domain in `Session.productionServerURL` and `ios/Config/Tally.entitlements`, and your team under Signing in Xcode.
- Firewall: 80 and 443 open to the world, SSH only through Identity-Aware Proxy.
- A dedicated service account for the VM with access to its own secrets, the backup bucket and log writing, and nothing else.
- A Cloud Storage bucket for the nightly encrypted (restic) database backup.
- A daily disk snapshot schedule.

`docker-compose.yml` pins `ENVIRONMENT=production` on the `api` and `worker` services, overriding `.env`, so production never runs as development. In production the settings refuse placeholder secrets: `.env` needs real values for the JWT signing key, database password and the other secrets before the containers start.

## First deploy on a new VM

Secrets live in Google Secret Manager (#23), never on the VM's disk. At boot, `tally-secrets.service` runs `deploy/tally-secrets.sh` before Docker starts. It fetches them into `/run/tally/secrets` (tmpfs, one read-only file per setting), and Compose mounts that folder at `/run/secrets`.

| Secret Manager name | File | Holds |
|---|---|---|
| `tally-database-url` | `database_url` | `postgresql+psycopg://tally:<password>@db:5432/tally` |
| `tally-postgres-password` | `postgres_password` | the same password, for the db container |
| `tally-jwt-signing-key` | `jwt_signing_key` | 64+ random characters |
| `tally-simplefin-encryption-key` | `simplefin_encryption_key` | a Fernet key |
| `tally-restic-password` | `restic_password` | the backup repository password |
| `tally-apns-key` | `apns.p8` | the APNs `.p8` key |
| `tally-anthropic-api-key` | `anthropic_api_key` | optional: the Claude API key for AI assist (M9). Not needed with Workload Identity Federation (below) |

Claude can instead use Workload Identity Federation, with no key at all. In the Claude Console, under Workload identity, choose Connect workload, then Google Cloud. The issuer is `https://accounts.google.com`. The rule matches audience `https://api.anthropic.com`, plus `sub` (from `gcloud iam service-accounts describe tally-vm@<project-id>.iam.gserviceaccount.com --format='value(uniqueId)'`) and `email` (`tally-vm@<project-id>.iam.gserviceaccount.com`). Then put `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID` and `ANTHROPIC_SERVICE_ACCOUNT_ID` in `.env` and restart `api` and `worker`. The server gets a fresh identity token from the metadata server for each exchange, and the SDK refreshes the Claude token on its own.

The VM's service account (`tally-vm`) gets `roles/secretmanager.secretAccessor` on each of these secrets and nothing broader. `.env` holds only non-secret settings, plus `TALLY_SECRETS_DIR=/run/tally/secrets`, `SECRETS_DIR=/run/secrets` and `POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password` (see `.env.example`).

From the repo checkout (`/opt/tally` on the VM):

```sh
sudo install -m 755 deploy/tally-secrets.sh /usr/local/sbin/tally-secrets
sudo install -m 644 deploy/tally-secrets.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tally-secrets

# The containers run as uid 10001: give it the backups folder.
sudo chown 10001:10001 backups && sudo chmod 700 backups

sudo docker compose build
sudo docker compose up -d db
sudo docker compose run --rm api alembic upgrade head
sudo docker compose up -d

# Once only: create the restic repository the nightly backup pushes to.
sudo docker compose exec worker sh -c 'RESTIC_PASSWORD_FILE=/run/secrets/restic_password restic init'
```

Keep a copy of the restic password and the SimpleFIN key outside Google Cloud too: without them a backup can't be restored, and the stored SimpleFIN access can't be decrypted. To rotate a secret, add a new version in Secret Manager, then run `sudo systemctl restart tally-secrets && sudo docker compose up -d --force-recreate`.

Later deploys: `git pull`, then `sudo docker compose build`, `sudo docker compose run --rm api alembic upgrade head` and `sudo docker compose up -d`.

## Monitoring

Container logs go to Cloud Logging through Docker's `gcplogs` driver. `/etc/docker/daemon.json` on the VM:

```json
{"log-driver": "gcplogs", "log-opts": {"gcp-log-cmd": "false"}}
```

The `tally-vm` service account needs `roles/logging.logWriter`. Grant it **before** switching the driver: Docker refuses to start containers while it can't write logs. `docker logs` still works locally (dual logging).

`deploy/monitoring/apply.py` creates the rest. It's idempotent: `python3 deploy/monitoring/apply.py --project <project-id> --email <you> --host <api-domain>`. It sets up:
- Log-based metrics for refused sign-ins, new passkeys, refresh-token reuse, 5xx responses and failed nightly jobs.
- An uptime check on `/healthz` every 5 minutes.
- Email alerts:
  - 3+ refused sign-ins in 15 minutes
  - any new passkey
  - any refresh-token reuse
  - 5+ server errors in 5 minutes
  - a failed backup or sync
  - the API down for 10 minutes

## App Attest

Sign-in and refresh accept an App Attest assertion (#75). The app gets a one-time challenge from `GET /api/auth/attest/challenge`, attests its key once with `POST /api/auth/attest`, then sends `X-App-Attest-Key-Id`, `X-App-Attest-Assertion` and `X-App-Attest-Challenge` headers, signing the request body followed by the challenge bytes. The App ID checked is `APNS_TEAM_ID.APNS_BUNDLE_ID`, and `APP_ATTEST_ENVIRONMENT` (`production` or `development`) must match the app's entitlement. `APP_ATTEST_REQUIRED` defaults to `false`: an assertion that is sent is still verified, but requests without one go through. Set it to `true` in production once the iOS app sends assertions; from then on sign-in and refresh without a valid one get a 401.
