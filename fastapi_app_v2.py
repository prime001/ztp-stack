017_enrollment_watchdog.py — ZTP Enrollment Watchdog

Purpose:
    Monitors the ZTP stack API for devices stuck in the provisioning pipeline.
    Alerts on enrollments that exceed a configurable timeout, optionally retries
    them via the API, and can forward alerts to a webhook endpoint.

Usage:
    # One-shot check; exits 1 if stuck devices found
    python 017_enrollment_watchdog.py --api-url http://localhost:8000

    # Continuous polling with auto-retry and Slack/Teams/custom webhook
    python 017_enrollment_watchdog.py \
        --api-url http://localhost:8000 \
        --stuck-minutes 20 \
        --retry \
        --webhook-url http://alerts.internal/ztp \
        --interval 60

Prerequisites:
    - ZTP FastAPI app running (007_fastapi_app.py)
    - Device registration and deployment tracker active (010, 003)
    - Python 3.8+; no third-party libraries required
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
log = logging.getLogger(__name__)

STUCK_STATUSES = {"pending", "provisioning", "downloading"}


def _http(url: str, payload: dict | None = None, timeout: int = 10) -> dict | list:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection failed to {url}: {exc.reason}") from exc


def get_pending_devices(api_url: str) -> list[dict]:
    try:
        devices = _http(f"{api_url}/devices")
    except RuntimeError as exc:
        log.error("Failed to fetch device list: %s", exc)
        return []
    if not isinstance(devices, list):
        log.error("Unexpected response shape from /devices")
        return []
    return [d for d in devices if d.get("status") in STUCK_STATUSES]


def find_stuck(devices: list[dict], stuck_minutes: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    stuck = []
    for dev in devices:
        ts_raw = dev.get("updated_at") or dev.get("created_at")
        if not ts_raw:
            continue
        try:
            updated = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            log.debug("Unparseable timestamp for %s: %s", dev.get("mac"), ts_raw)
            continue
        age = (now - updated).total_seconds() / 60
        if age > stuck_minutes:
            dev["_age_min"] = round(age, 1)
            stuck.append(dev)
    return stuck


def retry_device(api_url: str, device_id: str) -> bool:
    try:
        result = _http(f"{api_url}/devices/{device_id}/retry", payload={})
        log.info("Retry triggered for %s → %s", device_id, result.get("status", "accepted"))
        return True
    except RuntimeError as exc:
        log.error("Retry failed for %s: %s", device_id, exc)
        return False


def send_webhook(webhook_url: str, stuck: list[dict]) -> None:
    payload = {
        "event": "ztp.stuck_enrollments",
        "count": len(stuck),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "devices": [
            {
                "mac": d.get("mac"),
                "hostname": d.get("hostname"),
                "status": d.get("status"),
                "age_minutes": d.get("_age_min"),
            }
            for d in stuck
        ],
    }
    try:
        _http(webhook_url, payload=payload)
        log.info("Webhook alert delivered: %d device(s) reported", len(stuck))
    except RuntimeError as exc:
        log.warning("Webhook delivery failed: %s", exc)


def run_once(args: argparse.Namespace) -> int:
    log.info("Polling %s/devices …", args.api_url)
    pending = get_pending_devices(args.api_url)
    log.info("%d device(s) in active provisioning states", len(pending))

    stuck = find_stuck(pending, args.stuck_minutes)
    if not stuck:
        log.info("No stuck enrollments (threshold: %d min)", args.stuck_minutes)
        return 0

    log.warning("%d stuck enrollment(s):", len(stuck))
    for dev in stuck:
        log.warning(
            "  mac=%-17s  status=%-14s  age=%.1f min",
            dev.get("mac", "unknown"),
            dev.get("status", "?"),
            dev.get("_age_min", 0.0),
        )

    if args.webhook_url:
        send_webhook(args.webhook_url, stuck)

    if args.retry:
        for dev in stuck:
            dev_id = str(dev.get("id") or dev.get("mac", ""))
            if dev_id:
                retry_device(args.api_url, dev_id)

    return len(stuck)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ZTP enrollment watchdog — surfaces devices stuck in provisioning",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--api-url", default="http://localhost:8000", help="ZTP FastAPI base URL"
    )
    parser.add_argument(
        "--stuck-minutes",
        type=int,
        default=30,
        metavar="N",
        help="Minutes without progress before a device is flagged",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="POST /devices/<id>/retry for each stuck device",
    )
    parser.add_argument(
        "--webhook-url",
        default=None,
        metavar="URL",
        help="Deliver a JSON alert payload to this URL when stuck devices are found",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Poll on this interval; 0 = run once and exit",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.interval <= 0:
        sys.exit(1 if run_once(args) else 0)

    log.info("Watchdog polling every %ds — Ctrl-C to stop", args.interval)
    try:
        while True:
            run_once(args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Watchdog stopped")
        sys.exit(0)