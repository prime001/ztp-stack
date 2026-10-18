Writing the script now based on the ZTP stack context and the gap in existing coverage.

```python
"""
ZTP Post-Provisioning Verification
====================================
SSH-connects to devices that the ZTP API reports as "provisioned" and validates
key configuration checkpoints (hostname, NTP, management interface reachability).
Results are optionally written back to the API and/or a local JSON report file.

Fills the gap between device_registration (010) and the deployment API (002/012):
those scripts register and track devices; this one confirms they actually came up
correctly after the ZTP boot cycle.

Usage
-----
    # Verify all provisioned devices known to the API:
    python 013_provision_verify.py --api-url http://ztp.local:8000 \\
        --ssh-user admin --ssh-password changeme

    # Verify a single device by IP (API still used to patch status):
    python 013_provision_verify.py --api-url http://ztp.local:8000 \\
        --ssh-user admin --ssh-key ~/.ssh/ztp_key --device 192.0.2.10

    # Dry-run — verify but do not update API:
    python 013_provision_verify.py --api-url http://ztp.local:8000 \\
        --ssh-user admin --ssh-password changeme --no-patch --report out.json

Prerequisites
-------------
    pip install paramiko requests
    ZTP API (007_fastapi_app.py) running and reachable.
    SSH enabled on target devices with valid credentials.
    Devices must be in EOS/IOS-style CLI; adjust PLATFORM_CHECKS if needed.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

import paramiko
import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-platform CLI commands used for verification
# ---------------------------------------------------------------------------
PLATFORM_CHECKS: Dict[str, Dict[str, str]] = {
    "eos": {
        "hostname_set": "show hostname",
        "ntp_sync": "show ntp status | include Clock",
        "mgmt_up": "show interface Management1 | include line protocol",
    },
    "ios": {
        "hostname_set": "show version | include hostname",
        "ntp_sync": "show ntp status | include Clock is",
        "mgmt_up": "show interface GigabitEthernet0 | include line protocol",
    },
    "linux": {
        "hostname_set": "hostname",
        "ntp_sync": "timedatectl | grep 'NTP synchronized'",
        "mgmt_up": "ip link show eth0 | grep 'state UP'",
    },
}


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _ssh_client(hostname: str, port: int, username: str,
                password: Optional[str], key_path: Optional[str],
                timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = dict(
        hostname=hostname, port=port, username=username,
        timeout=timeout, allow_agent=False, look_for_keys=False,
    )
    if key_path:
        kwargs["key_filename"] = key_path
    else:
        kwargs["password"] = password
    client.connect(**kwargs)
    return client


def ssh_run(hostname: str, port: int, username: str,
            password: Optional[str], key_path: Optional[str],
            command: str, timeout: int = 10) -> str:
    client = _ssh_client(hostname, port, username, password, key_path, timeout)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace").strip()
        if err:
            log.debug("stderr [%s]: %s", hostname, err)
        return out
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Device verification
# ---------------------------------------------------------------------------

def verify_device(ip: str, platform: str, port: int, username: str,
                  password: Optional[str], key_path: Optional[str],
                  timeout: int) -> dict:
    checks = PLATFORM_CHECKS.get(platform, PLATFORM_CHECKS["eos"])
    result: dict = {
        "ip": ip,
        "platform": platform,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": {},
    }
    for name, cmd in checks.items():
        try:
            output = ssh_run(ip, port, username, password, key_path, cmd, timeout)
            passed = bool(output.strip())
            result["checks"][name] = {"passed": passed, "output": output.strip()[:200]}
        except paramiko.AuthenticationException:
            result["checks"][name] = {"passed": False, "output": "authentication failed"}
        except Exception as exc:
            result["checks"][name] = {"passed": False, "output": str(exc)[:200]}

    result["overall"] = all(c["passed"] for c in result["checks"].values())
    return result


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def fetch_provisioned(api_url: str) -> List[dict]:
    resp = requests.get(f"{api_url}/devices", params={"status": "provisioned"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def patch_status(api_url: str, device_id: str, verified: bool) -> None:
    status = "verified" if verified else "verification_failed"
    resp = requests.patch(f"{api_url}/devices/{device_id}", json={"status": status}, timeout=10)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Verify ZTP-provisioned devices via SSH.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--api-url", default="http://localhost:8000")
    p.add_argument("--device", metavar="IP",
                   help="Verify a single IP instead of querying the API")
    p.add_argument("--platform", choices=list(PLATFORM_CHECKS), default="eos")
    p.add_argument("--ssh-user", default="admin")
    p.add_argument("--ssh-password", default=None)
    p.add_argument("--ssh-key", metavar="PATH", default=None)
    p.add_argument("--ssh-port", type=int, default=22)
    p.add_argument("--timeout", type=int, default=10, metavar="SECS")
    p.add_argument("--report", metavar="FILE", help="Write JSON results to FILE")
    p.add_argument("--no-patch", action="store_true",
                   help="Skip patching device status back to the API")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    if not args.ssh_password and not args.ssh_key:
        log.error("Provide --ssh-password or --ssh-key.")
        return 1

    if args.device:
        devices = [{"id": None, "ip_address": args.device}]
    else:
        log.info("Fetching provisioned devices from %s", args.api_url)
        try:
            devices = fetch_provisioned(args.api_url)
        except requests.RequestException as exc:
            log.error("API error: %s", exc)
            return 1

    if not devices:
        log.info("No provisioned devices found — nothing to verify.")
        return 0

    log.info("Verifying %d device(s) [platform=%s]", len(devices), args.platform)
    report = []
    failed = 0

    for dev in devices:
        ip = dev.get("ip_address") or dev.get("ip", "")
        device_id = dev.get("id")
        log.info("  checking %s …", ip)

        result = verify_device(
            ip=ip, platform=args.platform, port=args.ssh_port,
            username=args.ssh_user, password=args.ssh_password,
            key_path=args.ssh_key, timeout=args.timeout,
        )
        report.append(result)

        label = "PASS" if result["overall"] else "FAIL"
        log.info("  %-16s %s", ip, label)
        if not result["overall"]:
            failed += 1
            for name, chk in result["checks"].items():
                if not chk["passed"]:
                    log.warning("    [%s] %s: %s", ip, name, chk["output"])

        if not args.no_patch and device_id:
            try:
                patch_status(args.api_url, device_id, result["overall"])
                log.debug("  patched %s -> %s", device_id, label.lower())
            except requests.RequestException as exc:
                log.warning("  could not update device %s: %s", device_id, exc)

    if args.report:
        with open(args.report, "w") as fh:
            json.dump(report, fh, indent=2)
        log.info("Report written: %s", args.report)

    log.info("Result: %d/%d passed.", len(report) - failed, len(report))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
```