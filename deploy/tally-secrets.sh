#!/bin/bash
# Fetches Tally's secrets from Secret Manager into tmpfs before Docker starts (#23), so no secret
# is ever written to the VM's disk. Installed as /usr/local/sbin/tally-secrets and run by
# tally-secrets.service. The VM's service account can read only these secrets.
set -euo pipefail

DIR=/run/tally/secrets
APP_UID=10001   # the api/worker containers' user
PG_UID=999      # postgres in the db container
PROJECT=$(curl -fsS -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/project/project-id)

# The folder must be listable: pydantic-settings lists it to match file names. Each file stays
# readable only by its owner (mode 400).
install -d -m 0755 -o root -g root /run/tally "$DIR"

# fetch <secret> <file> <owner uid>
fetch() {
  local tmp
  tmp=$(mktemp "$DIR/.fetch.XXXXXX")
  for attempt in $(seq 1 "${ATTEMPTS:-30}"); do
    if gcloud secrets versions access latest --secret="$1" --project="$PROJECT" \
        --out-file="$tmp" --quiet 2>/dev/null; then
      chown "$3:$3" "$tmp"
      chmod 0400 "$tmp"
      mv -f "$tmp" "$DIR/$2"
      return 0
    fi
    sleep 5
  done
  rm -f "$tmp"
  echo "tally-secrets: couldn't fetch $1" >&2
  return 1
}

fetch tally-database-url database_url "$APP_UID"
fetch tally-jwt-signing-key jwt_signing_key "$APP_UID"
fetch tally-simplefin-encryption-key simplefin_encryption_key "$APP_UID"
fetch tally-restic-password restic_password "$APP_UID"
fetch tally-apns-key apns.p8 "$APP_UID"
fetch tally-postgres-password postgres_password "$PG_UID"

# Optional: without this secret AI assist (M9) stays off, so a missing one doesn't stop the boot.
# The service account can read secret values but not list or describe secrets, so just try.
if ! ATTEMPTS=3 fetch tally-anthropic-api-key anthropic_api_key "$APP_UID" 2>/dev/null; then
  rm -f "$DIR/anthropic_api_key"
  echo "tally-secrets: no tally-anthropic-api-key; AI assist stays off"
fi
echo "tally-secrets: fetched into $DIR"
