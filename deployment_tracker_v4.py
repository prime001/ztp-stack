Writing the deployment-tracker script now.

```python
"""
033_deployment_tracker.py - ZTP Deployment Stall Detector

Purpose:
    Monitors active ZTP deployments by polling the ZTP stack REST API and
    flags devices that have been stuck in a provisioning stage longer than a
    configurable threshold.  Designed for NOC dashboards, CI gate checks, and
    post-deployment audits in zero-touch provisioning environments.

Usage:
    # One-shot check, table output
    python 033_deployment_tracker.py --api-url http://ztp-server:8000

    # Continuous polling every 60 s, exit non-zero if anything is stalled
    python 033_deployment_tracker.py --api-url http://ztp-server:8000 \
        --interval 60 --threshold 300 --exit-nonzero-on-stall

    # JSON output for pipeline integration
    python 033_deployment_tracker.py --format json | jq '.[] | select(.stalled)'

Prerequisites:
    - ZTP FastAPI service (007_fastapi_app.py / 017_fastapi_app.py) reachable
      from this host.
    - API must expose GET /devices returning a JSON list (or {"devices": [...]})
      where each device has: id, hostname, stage, status, last_seen (ISO-8601).
    - Python 3.10+; no third-party packages required (stdlib only).
"""

import argparse
import csv
import io
import json
import logging
import sys
import time
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)

ACTIVE_STATUSES = {"discovering", "booting", "configuring", "provisioning"}


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )


def fetch_devices(api_url: str, token: str | None) -> list[dict]:
    url = f"{api_url.rstrip('/')}/devices"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    LOG.debug("GET %s", url)
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except URLError as exc:
        LOG.error("API unreachable: %s", exc)
        raise SystemExit(1)
    except json.JSONDecodeError as exc:
        LOG.error("Invalid JSON from API: %s", exc)
        raise SystemExit(1)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "devices" in data:
        return data["devices"]
    LOG.error("Unexpected API response shape: %s", type(data).__name__)
    raise SystemExit(1)


def parse_timestamp(ts: str) -> datetime | None:
    if not ts:
        return None
    formats = (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(ts, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    LOG.warning("Cannot parse timestamp %r", ts)
    return None


def classify(device: dict, threshold: int, now: datetime) -> dict:
    last_seen = parse_timestamp(device.get("last_seen", ""))
    age_s = round((now - last_seen).total_seconds()) if last_seen else None
    status = device.get("status", "unknown").lower()
    stalled = (
        age_s is not None
        and age_s > threshold
        and status in ACTIVE_STATUSES
    )
    return {
        "id": device.get("id", ""),
        "hostname": device.get("hostname", "unknown"),
        "stage": device.get("stage", "unknown"),
        "status": status,
        "age_s": age_s,
        "stalled": stalled,
    }


def render_table(rows: list[dict]) -> str:
    if not rows:
        return "(no devices)"
    cols = ["hostname", "stage", "status", "age_s", "stalled"]
    widths = {c: max(len(c), max(len(str(r[c])) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    lines = [header, sep]
    for r in rows:
        line = "  ".join(str(r[c]).ljust(widths[c]) for c in cols)
        if r["stalled"]:
            line += "  <<< STALLED"
        lines.append(line)
    return "\n".join(lines)


def render_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    if not rows:
        return ""
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def poll_once(
    api_url: str,
    token: str | None,
    threshold: int,
    fmt: str,
) -> list[dict]:
    now = datetime.now(timezone.utc)
    devices = fetch_devices(api_url, token)
    rows = [classify(d, threshold, now) for d in devices]
    stalled = [r for r in rows if r["stalled"]]

    if fmt == "table":
        print(render_table(rows))
    elif fmt == "csv":
        print(render_csv(rows), end="")
    elif fmt == "json":
        print(json.dumps(rows, indent=2))

    if stalled:
        LOG.warning(
            "%d/%d device(s) stalled beyond %ds: %s",
            len(stalled),
            len(rows),
            threshold,
            ", ".join(r["hostname"] for r in stalled),
        )
    else:
        LOG.info("All %d device(s) progressing normally", len(rows))

    return stalled


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Detect stalled ZTP deployments by polling the stack API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--api-url", default="http://localhost:8000",
        help="Base URL of the ZTP FastAPI service",
    )
    p.add_argument(
        "--token", default=None,
        help="Bearer token for API authentication",
    )
    p.add_argument(
        "--threshold", type=int, default=300,
        help="Seconds without progress before a device is flagged as stalled",
    )
    p.add_argument(
        "--interval", type=int, default=0,
        help="Polling interval in seconds; 0 = run once and exit",
    )
    p.add_argument(
        "--format", dest="fmt", choices=["table", "csv", "json"],
        default="table", help="Output format",
    )
    p.add_argument(
        "--exit-nonzero-on-stall", action="store_true",
        help="Exit with code 2 when stalled devices are detected (for CI)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    configure_logging(args.verbose)

    stalled_ever = False
    try:
        if args.interval > 0:
            LOG.info("Polling every %ds — Ctrl-C to stop", args.interval)
            while True:
                stalled = poll_once(args.api_url, args.token, args.threshold, args.fmt)
                if stalled:
                    stalled_ever = True
                time.sleep(args.interval)
        else:
            stalled = poll_once(args.api_url, args.token, args.threshold, args.fmt)
            stalled_ever = bool(stalled)
    except KeyboardInterrupt:
        LOG.info("Interrupted.")

    if args.exit_nonzero_on_stall and stalled_ever:
        sys.exit(2)
```