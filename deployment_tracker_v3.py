Writing the script now — this is a portfolio-context generation task, no local repo needed.

#!/usr/bin/env python3
"""
ZTP Deployment Stage Monitor

Continuously polls a ZTP stack REST API to track devices progressing through
Zero Touch Provisioning stages: DISCOVERED -> DHCP_ASSIGNED -> CONFIG_FETCHED
-> PROVISIONED -> VERIFIED. Flags devices stuck in a single stage beyond a
configurable threshold and prints a structured summary each poll cycle.

Usage:
    # Watch devices every 30s, flag anything stuck >5 minutes
    python 023_deployment_tracker.py --api-url http://localhost:8000

    # One-shot JSON snapshot (useful in CI or dashboards)
    python 023_deployment_tracker.py --once --output json

    # Exit automatically once all devices reach a terminal state
    python 023_deployment_tracker.py --exit-on-complete --stuck-threshold 120

Prerequisites:
    - ZTP stack API running and reachable (see 007_fastapi_app.py)
    - Devices registered and visible at GET /devices
    - Python 3.8+; no third-party packages required
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

TERMINAL_STAGES = {"VERIFIED", "FAILED"}


def fetch_devices(api_url: str, timeout: int = 10) -> list[dict[str, Any]]:
    url = f"{api_url.rstrip('/')}/devices"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        raise RuntimeError(f"API returned {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc


def classify_devices(
    devices: list[dict[str, Any]], stuck_threshold: int
) -> dict[str, list[dict[str, Any]]]:
    now = datetime.now(timezone.utc).timestamp()
    buckets: dict[str, list] = {
        "progressing": [],
        "stuck": [],
        "completed": [],
        "failed": [],
    }

    for dev in devices:
        stage = dev.get("stage", "UNKNOWN")

        if stage == "VERIFIED":
            buckets["completed"].append(dev)
            continue
        if stage == "FAILED":
            buckets["failed"].append(dev)
            continue

        last_str = dev.get("last_updated") or dev.get("updated_at")
        if last_str:
            try:
                last_ts = datetime.fromisoformat(
                    last_str.replace("Z", "+00:00")
                ).timestamp()
                age = int(now - last_ts)
                if age > stuck_threshold:
                    dev["_stuck_seconds"] = age
                    buckets["stuck"].append(dev)
                    continue
            except ValueError:
                ident = dev.get("hostname") or dev.get("mac", "?")
                log.warning("Unparseable timestamp for %s: %s", ident, last_str)

        buckets["progressing"].append(dev)

    return buckets


def _ident(dev: dict[str, Any]) -> str:
    return dev.get("hostname") or dev.get("mac") or "unknown"


def format_table(buckets: dict[str, list[dict[str, Any]]]) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"\n=== ZTP Deployment Monitor [{ts}] ===",
        (
            f"  Progressing: {len(buckets['progressing'])}"
            f"  Stuck: {len(buckets['stuck'])}"
            f"  Completed: {len(buckets['completed'])}"
            f"  Failed: {len(buckets['failed'])}"
        ),
    ]

    if buckets["stuck"]:
        lines.append("\n[STUCK]")
        for d in buckets["stuck"]:
            lines.append(
                f"  {_ident(d):<32} stage={d.get('stage', '?'):<18}"
                f" stuck_for={d['_stuck_seconds']}s"
            )

    if buckets["failed"]:
        lines.append("\n[FAILED]")
        for d in buckets["failed"]:
            reason = d.get("failure_reason") or d.get("error") or "—"
            lines.append(f"  {_ident(d):<32} reason={reason}")

    if buckets["progressing"]:
        lines.append("\n[IN PROGRESS]")
        for d in buckets["progressing"]:
            lines.append(f"  {_ident(d):<32} stage={d.get('stage', '?')}")

    return "\n".join(lines)


def format_json(buckets: dict[str, list[dict[str, Any]]], poll_count: int) -> str:
    return json.dumps(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "poll": poll_count,
            "counts": {k: len(v) for k, v in buckets.items()},
            "stuck": [
                {
                    "identifier": _ident(d),
                    "stage": d.get("stage"),
                    "stuck_seconds": d.get("_stuck_seconds"),
                }
                for d in buckets["stuck"]
            ],
            "failed": [
                {
                    "identifier": _ident(d),
                    "reason": d.get("failure_reason") or d.get("error"),
                }
                for d in buckets["failed"]
            ],
        }
    )


def run_monitor(args: argparse.Namespace) -> None:
    poll_count = 0
    log.info(
        "Monitoring %s | interval=%ds stuck_threshold=%ds",
        args.api_url,
        args.interval,
        args.stuck_threshold,
    )

    while True:
        poll_count += 1
        try:
            devices = fetch_devices(args.api_url, timeout=args.timeout)
            buckets = classify_devices(devices, args.stuck_threshold)

            output = (
                format_json(buckets, poll_count)
                if args.output == "json"
                else format_table(buckets)
            )
            print(output, flush=True)

            if args.once:
                break

            all_terminal = not buckets["progressing"] and not buckets["stuck"]
            if args.exit_on_complete and all_terminal and devices:
                log.info("All %d devices reached terminal state. Exiting.", len(devices))
                break

        except RuntimeError as exc:
            log.error("Poll %d failed: %s", poll_count, exc)
            if args.once:
                sys.exit(1)

        time.sleep(args.interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor ZTP provisioning stage progression across devices",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--api-url", default="http://localhost:8000", help="ZTP API base URL"
    )
    parser.add_argument(
        "--interval", type=int, default=30, help="Poll interval in seconds"
    )
    parser.add_argument(
        "--stuck-threshold",
        type=int,
        default=300,
        help="Seconds in the same stage before a device is flagged as stuck",
    )
    parser.add_argument(
        "--timeout", type=int, default=10, help="HTTP request timeout in seconds"
    )
    parser.add_argument(
        "--output",
        choices=["table", "json"],
        default="table",
        help="Output format",
    )
    parser.add_argument(
        "--once", action="store_true", help="Poll once and exit"
    )
    parser.add_argument(
        "--exit-on-complete",
        action="store_true",
        help="Exit when all devices reach a terminal stage",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args()


if __name__ == "__main__":
    _args = parse_args()
    logging.basicConfig(
        level=getattr(logging, _args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        run_monitor(_args)
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(0)