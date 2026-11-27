"""`docker-compose.yml` must pin ENVIRONMENT=production for `api` and `worker` (#135), so a
`.env` that omits it can't silently run production as development.

The Caddyfile takes the Apple Team ID from APNS_TEAM_ID, so the `caddy` service must be handed it.
"""

from pathlib import Path

import pytest
import yaml

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
CADDYFILE = COMPOSE_FILE.parent / "Caddyfile"


@pytest.mark.parametrize("service", ["api", "worker"])
def test_service_forces_production_environment(service: str) -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text())

    assert compose["services"][service]["environment"]["ENVIRONMENT"] == "production"


def test_caddy_gets_the_team_id_the_caddyfile_reads() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text())

    assert '"{$APNS_TEAM_ID}.com.luisd07.tally"' in CADDYFILE.read_text()
    assert compose["services"]["caddy"]["environment"]["APNS_TEAM_ID"] == "${APNS_TEAM_ID:-}"
