"""`docker-compose.yml` shape check for #38 (PR #116 review MV-1/UB-1): the `api`
and `worker` services must mount the same `backups_dir`, or the worker's
`last_success.json` is invisible to `GET /api/backup/status` in production.
"""

import re
from pathlib import Path

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _backups_target_via_yaml(text: str) -> dict[str, str]:
    import yaml

    compose = yaml.safe_load(text)
    targets: dict[str, str] = {}
    for name in ("api", "worker"):
        for volume in compose["services"][name]["volumes"]:
            parts = volume.split(":")
            if len(parts) >= 2 and "backups" in parts[0]:
                targets[name] = parts[1]
    return targets


def _backups_target_via_text(text: str) -> dict[str, str]:
    """Regex fallback for when PyYAML isn't installed: finds each service's
    `volumes:` line and pulls out the `./backups:<target>` entry.
    """
    targets: dict[str, str] = {}
    for name in ("api", "worker"):
        service_match = re.search(rf"^  {name}:\n((?:    .*\n)*)", text, re.MULTILINE)
        assert service_match, f"service {name!r} not found in {COMPOSE_FILE}"
        mount_match = re.search(r"\./backups:([^\s:,\]]+)", service_match.group(1))
        if mount_match:
            targets[name] = mount_match.group(1)
    return targets


def test_api_and_worker_mount_the_same_backups_path() -> None:
    text = COMPOSE_FILE.read_text()
    try:
        targets = _backups_target_via_yaml(text)
    except ImportError:
        targets = _backups_target_via_text(text)

    assert "api" in targets, "api service has no ./backups mount"
    assert "worker" in targets, "worker service has no ./backups mount"
    assert targets["api"] == targets["worker"]
