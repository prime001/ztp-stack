#!/usr/bin/env python3
"""
010_device_registration.py — ZTP Device Registration Client

Connects to a newly provisioned network device via SSH, collects hardware
and software inventory (hostname, serial number, platform, OS version),
and registers the device against the ZTP stack REST API.

Usage:
    python 010_device_registration.py --host 192.168.1.10 --user admin \\
        --password secret --api-url http://ztp-server:8000

    python 010_device_registration.py --host 192.168.1.10 --user admin \\
        --key ~/.ssh/id_rsa --api-url http://ztp-server:8000 --dry-run

Prerequisites:
    pip install paramiko requests
    ZTP stack API (007_fastapi_app.py) must be reachable at --api-url
    SSH access enabled on the target device
"""

import argparse
import json
import logging
import re
import sys
import time
from getpass import getpass
from typing import Optional

import paramiko
import requests

LOG = logging.getLogger(__name__)


def build_ssh_client(
    host: str,
    port: int,
    username: str,
    password: Optional[str],
    key_path: Optional[str],
    timeout: int,
) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(
        hostname=host,
        port=port,
        username=username,
        timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    if key_path:
        kwargs["key_filename"] = key_path
    else:
        kwargs["password"] = password
    client.connect(**kwargs)
    return client


def run_cmd(client: paramiko.SSHClient, command: str, timeout: int = 20) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    stdout.channel.recv_exit_status()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if err:
        LOG.debug("stderr [%s]: %s", command, err)
    return stdout.read().decode("utf-8", errors="replace").strip()


def collect_facts(client: paramiko.SSHClient) -> dict:
    """Probe common vendor commands and return a normalized inventory dict."""
    facts: dict = {}

    facts["hostname"] = (run_cmd(client, "hostname") or "unknown").splitlines()[0].strip()

    version_raw = run_cmd(client, "show version 2>/dev/null || cat /proc/version 2>/dev/null || uname -a")
    facts["raw_version"] = version_raw[:1500]

    # Serial — Cisco IOS/NX-OS, IOS-XE, Linux dmidecode
    serial = "unknown"
    for cmd in (
        "show version | include [Ss]erial",
        "show inventory | include SN:",
        "dmidecode -s system-serial-number 2>/dev/null",
    ):
        out = run_cmd(client, cmd)
        m = re.search(r"(?:SN:\s*|[Ss]erial\s+[Nn]umber\s*:?\s*)(\S+)", out)
        if m:
            serial = m.group(1)
            break
    facts["serial"] = serial

    # Model
    model = "unknown"
    for cmd in (
        "show version | include [Mm]odel [Nn]umber",
        "show inventory | include DESCR",
        "dmidecode -s system-product-name 2>/dev/null",
    ):
        out = run_cmd(client, cmd)
        m = re.search(r"(?:[Mm]odel [Nn]umber\s*:\s*|DESCR:\s*\")([^\",\n]+)", out)
        if m:
            model = m.group(1).strip()
            break
    facts["model"] = model

    m = re.search(r"[Vv]ersion\s+([\d\w\.\(\)]+)", version_raw)
    facts["os_version"] = m.group(1) if m else "unknown"

    # Derive management IP from routing table
    ip_out = run_cmd(client, "ip route get 1.1.1.1 2>/dev/null | head -1")
    src_m = re.search(r"src\s+(\S+)", ip_out)
    facts["mgmt_ip"] = src_m.group(1) if src_m else ""

    LOG.debug("Facts collected:\n%s", json.dumps(facts, indent=2))
    return facts


def register_device(api_url: str, host: str, facts: dict, dry_run: bool) -> dict:
    payload = {
        "hostname": facts["hostname"],
        "mgmt_ip": facts.get("mgmt_ip") or host,
        "serial": facts["serial"],
        "model": facts["model"],
        "os_version": facts["os_version"],
    }

    if dry_run:
        LOG.info("[dry-run] POST %s/devices\n%s", api_url, json.dumps(payload, indent=2))
        return {"status": "dry_run", "payload": payload}

    resp = requests.post(
        f"{api_url.rstrip('/')}/devices",
        json=payload,
        timeout=15,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    result = resp.json()
    LOG.info("API response: %s", json.dumps(result, indent=2))
    return result


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect device inventory and register with the ZTP API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="Device management IP or hostname")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--user", required=True, help="SSH username")
    parser.add_argument("--password", help="SSH password (prompted if omitted and --key not set)")
    parser.add_argument("--key", metavar="PATH", help="SSH private key file")
    parser.add_argument("--timeout", type=int, default=30, help="SSH connect timeout seconds (default: 30)")
    parser.add_argument("--api-url", default="http://localhost:8000",
                        help="ZTP API base URL (default: http://localhost:8000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Collect facts but skip API submission")
    parser.add_argument("--retries", type=int, default=3,
                        help="SSH connection attempts before giving up (default: 3)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    if not args.key and not args.password:
        args.password = getpass(f"SSH password for {args.user}@{args.host}: ")

    client = None
    for attempt in range(1, args.retries + 1):
        try:
            LOG.info("Connecting to %s:%d (attempt %d/%d)", args.host, args.port, attempt, args.retries)
            client = build_ssh_client(
                args.host, args.port, args.user, args.password, args.key, args.timeout
            )
            LOG.info("Connected.")
            break
        except paramiko.AuthenticationException as exc:
            LOG.error("Authentication failed: %s", exc)
            return 1
        except (paramiko.SSHException, OSError) as exc:
            LOG.warning("Attempt %d failed: %s", attempt, exc)
            if attempt < args.retries:
                time.sleep(5 * attempt)
            else:
                LOG.error("Exhausted %d attempts. Aborting.", args.retries)
                return 1

    try:
        facts = collect_facts(client)
        result = register_device(args.api_url, args.host, facts, args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response is not None else ""
        LOG.error("API error %s: %s", exc, body)
        return 2
    except Exception as exc:
        LOG.error("Unexpected error: %s", exc)
        return 3
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())