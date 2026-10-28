```python
#!/usr/bin/env python3
"""
030_device_registration.py — Batch ZTP Device Registration with Pre-flight Validation

Purpose:
    Reads a CSV of candidate devices, performs SSH reachability checks, collects
    serial numbers and platform info via Paramiko, then POSTs each device to the
    ZTP stack registration API.  Writes a results CSV summarising pass/fail per
    device so the operator can spot failures at a glance.

Usage:
    python 030_device_registration.py --input devices.csv --api http://ztp-server:8000 \
        --username admin --password secret [--dry-run] [--timeout 10]

Prerequisites:
    pip install paramiko requests
    ZTP FastAPI stack running and reachable at --api base URL.

Input CSV columns (header required):
    hostname, mgmt_ip, platform   (platform: ios | eos | nxos | junos)
"""

import argparse
import csv
import logging
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import paramiko
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SHOW_SERIAL = {
    "ios":   "show version | include Processor board ID",
    "eos":   "show version | grep Serial",
    "nxos":  "show version | grep -i serial",
    "junos": "show chassis hardware | grep Chassis",
}


def parse_args():
    p = argparse.ArgumentParser(description="Batch ZTP device registration")
    p.add_argument("--input", required=True, help="Path to devices CSV")
    p.add_argument("--api", required=True, help="ZTP API base URL")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--timeout", type=int, default=10, help="SSH/HTTP timeout (s)")
    p.add_argument("--dry-run", action="store_true", help="Validate only; skip API POST")
    p.add_argument("--output", default="registration_results.csv")
    return p.parse_args()


def is_tcp_reachable(host: str, port: int = 22, timeout: int = 5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def collect_serial(host: str, username: str, password: str,
                   platform: str, timeout: int) -> str | None:
    cmd = SHOW_SERIAL.get(platform.lower())
    if not cmd:
        log.warning("%s: unknown platform '%s'; skipping serial collection", host, platform)
        return None

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=username, password=password,
                       timeout=timeout, look_for_keys=False, allow_agent=False)
        _, stdout, _ = client.exec_command(cmd, timeout=timeout)
        output = stdout.read().decode().strip()
        # Pull the last whitespace-delimited token as the serial number
        token = output.split()[-1] if output.split() else None
        return token
    except paramiko.AuthenticationException:
        log.error("%s: SSH authentication failed", host)
    except (paramiko.SSHException, OSError) as exc:
        log.error("%s: SSH error — %s", host, exc)
    finally:
        client.close()
    return None


def register_device(api_base: str, payload: dict, timeout: int) -> tuple[bool, str]:
    url = f"{api_base.rstrip('/')}/api/v1/devices"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code in (200, 201):
            return True, resp.json().get("id", "ok")
        return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
    except requests.RequestException as exc:
        return False, str(exc)


def load_devices(path: str) -> list[dict]:
    devices = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip(): v.strip() for k, v in row.items()}
            if not all(k in row for k in ("hostname", "mgmt_ip", "platform")):
                log.warning("Skipping malformed row: %s", row)
                continue
            devices.append(row)
    return devices


def main():
    args = parse_args()

    if not Path(args.input).is_file():
        log.error("Input file not found: %s", args.input)
        sys.exit(1)

    devices = load_devices(args.input)
    log.info("Loaded %d device(s) from %s", len(devices), args.input)

    results = []
    registered = skipped = failed = 0

    for dev in devices:
        hostname = dev["hostname"]
        ip = dev["mgmt_ip"]
        platform = dev["platform"]
        result = {"hostname": hostname, "mgmt_ip": ip, "platform": platform,
                  "serial": "", "status": "", "detail": ""}

        if not is_tcp_reachable(ip, timeout=args.timeout):
            log.warning("%s (%s): TCP:22 unreachable — skipping", hostname, ip)
            result.update(status="skipped", detail="TCP unreachable")
            skipped += 1
            results.append(result)
            continue

        serial = collect_serial(ip, args.username, args.password, platform, args.timeout)
        result["serial"] = serial or ""

        if args.dry_run:
            log.info("%s: dry-run — would register serial=%s", hostname, serial)
            result.update(status="dry-run", detail="skipped by --dry-run")
            results.append(result)
            continue

        payload = {
            "hostname": hostname,
            "mgmt_ip": ip,
            "platform": platform,
            "serial_number": serial,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        ok, detail = register_device(args.api, payload, args.timeout)
        if ok:
            log.info("%s: registered (id=%s)", hostname, detail)
            result.update(status="registered", detail=detail)
            registered += 1
        else:
            log.error("%s: registration failed — %s", hostname, detail)
            result.update(status="failed", detail=detail)
            failed += 1

        results.append(result)

    with open(args.output, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["hostname", "mgmt_ip", "platform",
                                                 "serial", "status", "detail"])
        writer.writeheader()
        writer.writerows(results)

    log.info("Done — registered=%d skipped=%d failed=%d → %s",
             registered, skipped, failed, args.output)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
```