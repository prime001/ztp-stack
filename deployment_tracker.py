ZTP Deployment Tracker — tracks per-device provisioning state via SSH.

Purpose:
    After a ZTP stack stages devices, this script SSHes into each target,
    collects software version, hostname, and uptime, then writes a JSON
    state file so CI pipelines and operators can track progress without
    logging into devices manually.

Usage:
    python 003_deployment_tracker.py --inventory devices.txt \
        --username admin --password-env NET_PASS \
        [--state-file deployment_state.json] [--timeout 10] [--verbose]

Prerequisites:
    pip install paramiko
    Export credentials via environment variable (see --password-env).
    Device inventory file: one IP or hostname per line, blank lines ignored.

Output:
    JSON state file with per-device records:
        { "ip": ..., "status": "ok|unreachable|auth_error|error",
          "hostname": ..., "version": ..., "uptime": ..., "checked_at": ... }
    Exit code 0 when all devices are "ok", 1 otherwise.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import paramiko

LOG = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_UNREACHABLE = "unreachable"
STATUS_AUTH_ERROR = "auth_error"
STATUS_ERROR = "error"

COMMANDS = {
    "hostname": "show version | include uptime",
    "version": "show version | include Software",
}


def load_inventory(path: str) -> list[str]:
    with open(path) as fh:
        return [line.strip() for line in fh if line.strip() and not line.startswith("#")]


def ssh_collect(host: str, username: str, password: str, timeout: int) -> dict:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    result = {"ip": host, "hostname": None, "version": None, "uptime": None}
    try:
        client.connect(host, username=username, password=password,
                       timeout=timeout, look_for_keys=False, allow_agent=False)
        for key, cmd in COMMANDS.items():
            _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            output = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            if err:
                LOG.debug("%s stderr for '%s': %s", host, cmd, err)
            result[key] = output or None
        result["status"] = STATUS_OK
    except paramiko.AuthenticationException:
        LOG.warning("%s: authentication failed", host)
        result["status"] = STATUS_AUTH_ERROR
    except (paramiko.SSHException, OSError) as exc:
        LOG.warning("%s: unreachable — %s", host, exc)
        result["status"] = STATUS_UNREACHABLE
    except Exception as exc:
        LOG.error("%s: unexpected error — %s", host, exc)
        result["status"] = STATUS_ERROR
    finally:
        client.close()
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    return result


def load_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as fh:
                data = json.load(fh)
                return {entry["ip"]: entry for entry in data if "ip" in entry}
        except (json.JSONDecodeError, KeyError) as exc:
            LOG.warning("Could not parse existing state file: %s", exc)
    return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as fh:
        json.dump(list(state.values()), fh, indent=2)
    LOG.debug("State written to %s", path)


def print_summary(state: dict) -> None:
    counts: dict[str, int] = {}
    for entry in state.values():
        s = entry.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    total = len(state)
    print(f"\nDeployment summary — {total} device(s)")
    print("-" * 36)
    for status, n in sorted(counts.items()):
        marker = "+" if status == STATUS_OK else "x"
        print(f"  {marker}  {status:<14} {n:>4}")
    print()
    failures = [ip for ip, e in state.items() if e.get("status") != STATUS_OK]
    if failures:
        print("Failed devices:")
        for ip in failures:
            print(f"  {ip}  ({state[ip].get('status')})")
        print()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track ZTP device deployment status via SSH."
    )
    parser.add_argument("--inventory", required=True,
                        help="Path to device inventory file (one host per line)")
    parser.add_argument("--username", required=True, help="SSH username")
    parser.add_argument("--password-env", default="NET_PASS",
                        help="Env var holding the SSH password (default: NET_PASS)")
    parser.add_argument("--state-file", default="deployment_state.json",
                        help="JSON file to persist device state (default: deployment_state.json)")
    parser.add_argument("--timeout", type=int, default=10,
                        help="SSH connect/read timeout in seconds (default: 10)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    password = os.environ.get(args.password_env)
    if not password:
        LOG.error("Password env var '%s' is not set or empty.", args.password_env)
        sys.exit(2)

    try:
        hosts = load_inventory(args.inventory)
    except FileNotFoundError:
        LOG.error("Inventory file not found: %s", args.inventory)
        sys.exit(2)

    if not hosts:
        LOG.error("Inventory file is empty: %s", args.inventory)
        sys.exit(2)

    LOG.info("Checking %d device(s) from %s", len(hosts), args.inventory)
    state = load_state(args.state_file)

    for host in hosts:
        LOG.info("Polling %s ...", host)
        result = ssh_collect(host, args.username, password, args.timeout)
        state[host] = result
        LOG.info("%s -> %s", host, result["status"])

    save_state(args.state_file, state)
    print_summary(state)

    all_ok = all(e.get("status") == STATUS_OK for e in state.values())
    sys.exit(0 if all_ok else 1)