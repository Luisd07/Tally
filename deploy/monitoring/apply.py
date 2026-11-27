"""Create Tally's log-based metrics, email channel, uptime check and alert policies (#24).

Idempotent: anything that already exists (matched by name or display name) is left alone.
Run from a machine signed in to gcloud as a project owner:

    python3 deploy/monitoring/apply.py --project your-gcp-project --email you@example.com --host api.example.com

Logs reach Cloud Logging through Docker's gcplogs driver (see docs/deployment.md): each container line is in
`jsonPayload.message`, under the `gcplogs-docker-driver` log on the VM's `gce_instance`.
"""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = 'logName="projects/{project}/logs/gcplogs-docker-driver"'
API = 'jsonPayload.container.name="/tally-api-1"'
WORKER = 'jsonPayload.container.name="/tally-worker-1"'

# name -> (description, filter beyond the log name)
METRICS = {
    "tally_auth_refused": (
        "Refused passkey enrollments, sign-ins, refreshes and attestations.",
        f'{API} AND jsonPayload.message:"refused: "',
    ),
    "tally_passkey_enrolled": (
        "A new passkey was enrolled.",
        f'{API} AND jsonPayload.message:"Passkey enrolled"',
    ),
    "tally_refresh_reuse": (
        "A rotated refresh token was used again: the token family was revoked.",
        f'{API} AND jsonPayload.message:"refresh token reuse detected"',
    ),
    "tally_server_errors": (
        "API responses with a 5xx status, and unhandled errors.",
        f'{API} AND (jsonPayload.message=~"- [A-Z]+ [^ ]+ 5[0-9][0-9] [0-9.]+ms" '
        'OR jsonPayload.message:"Unhandled error")',
    ),
    "tally_job_failures": (
        "A failed nightly backup or SimpleFIN sync.",
        f'{WORKER} AND (jsonPayload.message:"nightly backup" AND jsonPayload.message:"| ERROR" '
        'OR jsonPayload.message:"Nightly sync finished: status=failed")',
    ),
}

# display name -> (metric, window seconds, fire when the window's count is above this, docs)
LOG_ALERTS = {
    "Tally: repeated refused sign-ins": (
        "tally_auth_refused", 900, 2,
        "Three or more refused passkey ceremonies or refreshes in 15 minutes. Check More → Sign-in "
        "activity in the app. If it wasn't you, someone is trying the API.",
    ),
    "Tally: new passkey enrolled": (
        "tally_passkey_enrolled", 300, 0,
        "A passkey was enrolled. If it wasn't you, run `scripts.enroll --replace` on the VM.",
    ),
    "Tally: refresh token reused": (
        "tally_refresh_reuse", 300, 0,
        "An already-rotated refresh token was presented, so the whole session family was revoked. "
        "This can mean a stolen token.",
    ),
    "Tally: burst of server errors": (
        "tally_server_errors", 300, 4,
        "Five or more 5xx responses in 5 minutes. `docker compose logs api` on the VM.",
    ),
    "Tally: nightly job failed": (
        "tally_job_failures", 600, 0,
        "The nightly backup or SimpleFIN sync failed. `docker compose logs worker` on the VM.",
    ),
}

UPTIME_NAME = "Tally /healthz"


def token() -> str:
    return subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True
    ).stdout.strip()


def call(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token()}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def ensure_metrics(project: str) -> None:
    base = f"https://logging.googleapis.com/v2/projects/{project}/metrics"
    for name, (description, extra) in METRICS.items():
        try:
            call("GET", f"{base}/{name}")
            print(f"metric {name}: exists")
            continue
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        call("POST", base, {
            "name": name,
            "description": description,
            "filter": f"{LOG.format(project=project)} AND {extra}",
            "metricDescriptor": {"metricKind": "DELTA", "valueType": "INT64"},
        })
        print(f"metric {name}: created")


def ensure_channel(project: str, email: str) -> str:
    base = f"https://monitoring.googleapis.com/v3/projects/{project}/notificationChannels"
    for channel in call("GET", base).get("notificationChannels", []):
        if channel.get("type") == "email" and channel.get("labels", {}).get("email_address") == email:
            print(f"email channel {email}: exists")
            return channel["name"]
    created = call("POST", base, {
        "type": "email", "displayName": f"Tally alerts ({email})", "labels": {"email_address": email},
    })
    print(f"email channel {email}: created")
    return created["name"]


def ensure_uptime(project: str, host: str) -> str:
    base = f"https://monitoring.googleapis.com/v3/projects/{project}/uptimeCheckConfigs"
    for check in call("GET", base).get("uptimeCheckConfigs", []):
        if check.get("displayName") == UPTIME_NAME:
            print("uptime check: exists")
            return check["name"].rsplit("/", 1)[-1]
    created = call("POST", base, {
        "displayName": UPTIME_NAME,
        "monitoredResource": {"type": "uptime_url", "labels": {"project_id": project, "host": host}},
        "httpCheck": {"path": "/healthz", "port": 443, "useSsl": True, "validateSsl": True},
        "contentMatchers": [{"content": '"ok"'}],
        "period": "300s",
        "timeout": "10s",
    })
    print("uptime check: created")
    return created["name"].rsplit("/", 1)[-1]


def existing_policies(project: str) -> set[str]:
    base = f"https://monitoring.googleapis.com/v3/projects/{project}/alertPolicies"
    return {p["displayName"] for p in call("GET", base).get("alertPolicies", [])}


def create_policy(project: str, policy: dict) -> None:
    base = f"https://monitoring.googleapis.com/v3/projects/{project}/alertPolicies"
    for attempt in range(12):  # a brand-new log-based metric can take a minute to be usable
        try:
            call("POST", base, policy)
            print(f"alert '{policy['displayName']}': created")
            return
        except urllib.error.HTTPError as error:
            if error.code not in (400, 404) or attempt == 11:
                raise RuntimeError(f"{policy['displayName']}: {error.read().decode()[:300]}") from error
            time.sleep(10)


def ensure_alerts(project: str, channel: str, check_id: str, host: str) -> None:
    have = existing_policies(project)
    for display, (metric, window, above, docs) in LOG_ALERTS.items():
        if display in have:
            print(f"alert '{display}': exists")
            continue
        create_policy(project, {
            "displayName": display,
            "combiner": "OR",
            "documentation": {"content": docs, "mimeType": "text/markdown"},
            "notificationChannels": [channel],
            "alertStrategy": {"autoClose": "1800s"},
            "conditions": [{
                "displayName": display,
                "conditionThreshold": {
                    "filter": f'metric.type="logging.googleapis.com/user/{metric}" '
                              'AND resource.type="gce_instance"',
                    "aggregations": [{
                        "alignmentPeriod": f"{window}s",
                        "perSeriesAligner": "ALIGN_SUM",
                        "crossSeriesReducer": "REDUCE_SUM",
                    }],
                    "comparison": "COMPARISON_GT",
                    "thresholdValue": above,
                    "duration": "0s",
                    "trigger": {"count": 1},
                },
            }],
        })
    display = "Tally: API is down"
    if display in have:
        print(f"alert '{display}': exists")
        return
    create_policy(project, {
        "displayName": display,
        "combiner": "OR",
        "documentation": {
            "content": f"`https://{host}/healthz` has failed its uptime check for 10 minutes.",
            "mimeType": "text/markdown",
        },
        "notificationChannels": [channel],
        "conditions": [{
            "displayName": display,
            "conditionThreshold": {
                "filter": 'metric.type="monitoring.googleapis.com/uptime_check/check_passed" '
                          f'AND resource.type="uptime_url" AND metric.label.check_id="{check_id}"',
                "aggregations": [{
                    "alignmentPeriod": "300s",
                    "perSeriesAligner": "ALIGN_NEXT_OLDER",
                    "crossSeriesReducer": "REDUCE_COUNT_FALSE",
                    "groupByFields": ["resource.label.host"],
                }],
                "comparison": "COMPARISON_GT",
                "thresholdValue": 1,
                "duration": "600s",
                "trigger": {"count": 1},
            },
        }],
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--host", default="api.example.com", help="the API domain to check; replace the placeholder")
    args = parser.parse_args()
    ensure_metrics(args.project)
    channel = ensure_channel(args.project, args.email)
    check_id = ensure_uptime(args.project, args.host)
    ensure_alerts(args.project, channel, check_id, args.host)


if __name__ == "__main__":
    main()
