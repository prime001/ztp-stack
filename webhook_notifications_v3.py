```python
"""
029_webhook_notifications.py — ZTP Webhook Dispatcher with Retry and HMAC Signing

Purpose:
    Reliable webhook delivery for ZTP lifecycle events with exponential backoff,
    HMAC-SHA256 request signing, and a SQLite dead-letter queue for audit/replay.

Usage:
    python 029_webhook_notifications.py --event provisioning_complete \
        --device-id sw-access-01 --endpoints https://hooks.example.com/ztp \
        --secret mysecret

    # Replay failed deliveries from the dead-letter queue
    python 029_webhook_notifications.py --replay-dlq --db /var/lib/ztp/webhooks.db

Prerequisites:
    - Python 3.9+
    - stdlib only (urllib, hmac, hashlib, sqlite3, json, logging, argparse)
    - Network access to webhook endpoints
"""

import argparse
import hashlib
import hmac
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("ztp.webhooks")

ZTP_EVENTS = {
    "device_discovered", "device_registered", "provisioning_started",
    "provisioning_complete", "provisioning_failed", "config_pushed", "health_check",
}

RETRY_DELAYS = [5, 15, 60, 300]  # seconds; 4 attempts total


def _db_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_dlq (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            device_id   TEXT NOT NULL,
            endpoint    TEXT NOT NULL,
            payload     TEXT NOT NULL,
            last_error  TEXT,
            attempt     INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL,
            next_retry  TEXT,
            delivered   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _sign_payload(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(endpoint: str, payload: dict, secret: str, timeout: int = 10) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "X-ZTP-Signature": _sign_payload(secret, body),
        "X-ZTP-Event": payload.get("event_type", "unknown"),
        "User-Agent": "ztp-stack/1.0",
    }
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 400:
            raise urllib.error.HTTPError(endpoint, resp.status, resp.reason, {}, None)


def _build_payload(event_type: str, device_id: str, extra: dict) -> dict:
    return {
        "event_type": event_type,
        "device_id": device_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "ztp-stack",
        **extra,
    }


def dispatch(event_type: str, device_id: str, endpoints: list[str],
             secret: str, extra: dict, db_path: str) -> dict[str, bool]:
    payload = _build_payload(event_type, device_id, extra)
    conn = _db_connect(db_path)
    results: dict[str, bool] = {}

    for endpoint in endpoints:
        success = False
        last_error = None
        for attempt, delay in enumerate(RETRY_DELAYS):
            try:
                _post(endpoint, payload, secret)
                LOG.info("delivered event=%s device=%s endpoint=%s attempt=%d",
                         event_type, device_id, endpoint, attempt + 1)
                success = True
                break
            except Exception as exc:
                last_error = str(exc)
                LOG.warning("attempt %d failed endpoint=%s error=%s",
                            attempt + 1, endpoint, exc)
                if attempt < len(RETRY_DELAYS) - 1:
                    time.sleep(delay)

        results[endpoint] = success
        if not success:
            conn.execute(
                """INSERT INTO webhook_dlq
                   (event_type, device_id, endpoint, payload, last_error, attempt, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (event_type, device_id, endpoint, json.dumps(payload),
                 last_error, len(RETRY_DELAYS),
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            LOG.error("all attempts failed; queued in DLQ endpoint=%s", endpoint)

    conn.close()
    return results


def replay_dlq(db_path: str, secret: str) -> None:
    conn = _db_connect(db_path)
    rows = conn.execute(
        "SELECT id, endpoint, payload FROM webhook_dlq WHERE delivered=0"
    ).fetchall()
    LOG.info("DLQ replay: %d pending entries", len(rows))

    for row_id, endpoint, raw_payload in rows:
        try:
            payload = json.loads(raw_payload)
            _post(endpoint, payload, secret)
            conn.execute("UPDATE webhook_dlq SET delivered=1 WHERE id=?", (row_id,))
            conn.commit()
            LOG.info("replayed dlq_id=%d endpoint=%s", row_id, endpoint)
        except Exception as exc:
            LOG.warning("replay failed dlq_id=%d error=%s", row_id, exc)

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ZTP webhook dispatcher")
    parser.add_argument("--event", choices=sorted(ZTP_EVENTS),
                        help="ZTP lifecycle event type")
    parser.add_argument("--device-id", help="Device hostname or MAC")
    parser.add_argument("--endpoints", nargs="+", metavar="URL",
                        help="Webhook receiver URLs")
    parser.add_argument("--secret", required=True, help="HMAC signing secret")
    parser.add_argument("--extra", default="{}", metavar="JSON",
                        help="Extra JSON fields merged into payload")
    parser.add_argument("--db", default="/tmp/ztp_webhooks.db",
                        help="SQLite path for DLQ (default: /tmp/ztp_webhooks.db)")
    parser.add_argument("--replay-dlq", action="store_true",
                        help="Replay undelivered DLQ entries and exit")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.replay_dlq:
        replay_dlq(args.db, args.secret)
        return

    if not args.event:
        parser.error("--event is required unless --replay-dlq is set")
    if not args.device_id:
        parser.error("--device-id is required unless --replay-dlq is set")
    if not args.endpoints:
        parser.error("--endpoints is required unless --replay-dlq is set")

    try:
        extra = json.loads(args.extra)
    except json.JSONDecodeError as exc:
        parser.error(f"--extra is not valid JSON: {exc}")

    results = dispatch(args.event, args.device_id, args.endpoints,
                       args.secret, extra, args.db)
    failed = [ep for ep, ok in results.items() if not ok]
    if failed:
        LOG.error("delivery failed for %d endpoint(s); see DLQ at %s", len(failed), args.db)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
```