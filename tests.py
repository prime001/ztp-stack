The script should live in the `ztp-stack` portfolio repo (not this one). Here is the script:

```python
"""
006_ssh_validation.py — Post-provisioning SSH reachability and health checker.

After ZTP completes (DHCP lease assigned, config template deployed), this script
validates that each provisioned device is reachable via SSH, authenticates
successfully, and returns expected output from a set of diagnostic commands.

Designed to run as a CI gate in the ZTP pipeline or interactively against a
device inventory file.

Usage:
    # Single device
    python 006_ssh_validation.py --host 192.168.1.10 --user admin --password-env DEVICE_PASS

    # Inventory file (JSON list of {"host","user","password"} objects)
    python 006_ssh_validation.py --inventory devices.json

    # With a custom command set and strict exit code
    python 006_ssh_validation.py --inventory devices.json --commands "show version,show interfaces" --strict

Prerequisites:
    pip install paramiko
    DEVICE_PASS env var (or --password-env pointing to one) for single-device mode
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import paramiko

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger(__name__)

DEFAULT_COMMANDS = [
    "show version",
    "show interfaces status",
    "show running-config | include hostname",
]
DEFAULT_TIMEOUT = 15
DEFAULT_PORT = 22


@dataclass
class DeviceResult:
    host: str
    reachable: bool = False
    auth_ok: bool = False
    command_results: dict = field(default_factory=dict)
    error: Optional[str] = None
    elapsed_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.reachable and self.auth_ok and self.error is None


def validate_device(host: str, user: str, password: str, commands: list[str],
                    port: int = DEFAULT_PORT, timeout: int = DEFAULT_TIMEOUT) -> DeviceResult:
    result = DeviceResult(host=host)
    start = time.monotonic()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        result.reachable = True
        result.auth_ok = True
        log.info("%s: SSH authenticated OK", host)

        for cmd in commands:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            output = stdout.read().decode(errors="replace").strip()
            err = stderr.read().decode(errors="replace").strip()
            result.command_results[cmd] = output if output else err
            log.debug("%s | %r -> %d chars", host, cmd, len(output))

    except paramiko.AuthenticationException:
        result.reachable = True
        result.error = "authentication failed"
        log.warning("%s: authentication failed", host)
    except (paramiko.SSHException, OSError) as exc:
        result.error = str(exc)
        log.warning("%s: connection error: %s", host, exc)
    finally:
        client.close()
        result.elapsed_s = round(time.monotonic() - start, 2)

    return result


def load_inventory(path: str) -> list[dict]:
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("Inventory JSON must be a top-level list")
    for entry in data:
        for key in ("host", "user", "password"):
            if key not in entry:
                raise ValueError(f"Inventory entry missing field '{key}': {entry}")
    return data


def print_summary(results: list[DeviceResult]) -> None:
    passed = sum(1 for r in results if r.passed)
    print(f"\n{'='*60}")
    print(f"SSH Validation Summary: {passed}/{len(results)} passed")
    print(f"{'='*60}")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        detail = r.error or f"{len(r.command_results)} commands executed"
        print(f"  [{status}] {r.host:<20} {detail}  ({r.elapsed_s}s)")
    print()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Validate SSH reachability of ZTP-provisioned devices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--host", help="Single device IP or hostname")
    group.add_argument("--inventory", metavar="FILE", help="JSON inventory file")

    p.add_argument("--user", help="SSH username (single-device mode)")
    p.add_argument("--password-env", default="DEVICE_PASS",
                   metavar="ENV_VAR", help="Env var holding the SSH password")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="SSH port")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Connection timeout (s)")
    p.add_argument("--commands", help="Comma-separated commands to run (overrides defaults)")
    p.add_argument("--strict", action="store_true",
                   help="Exit non-zero if any device fails (useful for CI)")
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("paramiko").setLevel(logging.DEBUG)
    else:
        logging.getLogger("paramiko").setLevel(logging.WARNING)

    commands = [c.strip() for c in args.commands.split(",")] if args.commands else DEFAULT_COMMANDS

    if args.inventory:
        try:
            inventory = load_inventory(args.inventory)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.error("Failed to load inventory: %s", exc)
            sys.exit(2)
    else:
        if not args.user:
            parser.error("--user is required with --host")
        password = os.environ.get(args.password_env, "")
        if not password:
            parser.error(f"Env var {args.password_env!r} is not set")
        inventory = [{"host": args.host, "user": args.user, "password": password}]

    results = []
    for entry in inventory:
        log.info("Checking %s ...", entry["host"])
        result = validate_device(
            host=entry["host"],
            user=entry["user"],
            password=entry["password"],
            commands=commands,
            port=args.port,
            timeout=args.timeout,
        )
        results.append(result)

    print_summary(results)

    if args.strict and not all(r.passed for r in results):
        sys.exit(1)
```