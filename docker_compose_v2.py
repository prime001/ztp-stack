#!/usr/bin/env python3
"""
011_provision_verifier.py — Post-ZTP Device Provisioning Verifier

Connects to a newly provisioned network device via SSH and runs a suite of
health checks to confirm successful zero-touch provisioning. Reports pass/fail
for each check and exits non-zero if any critical check fails.

Typical ZTP workflow: device boots, pulls config via DHCP/TFTP, then this
script is called (e.g. from a webhook or polling loop) to confirm the device
is operational before marking the deployment as complete.

Usage:
    python 011_provision_verifier.py --host 192.168.1.10 --user admin --password secret
    python 011_provision_verifier.py --host 192.168.1.10 --user admin --key ~/.ssh/id_rsa \
        --platform eos --expect-version "4.29" --expect-hostname "sw-access-01"

Prerequisites:
    pip install paramiko
    Device must be reachable on port 22 with SSH configured.
    For key auth, the public key must be pre-seeded in the ZTP initial config.

Exit codes:
    0 — all critical checks passed
    1 — one or more critical checks failed
    2 — bad arguments or connection failure
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

import paramiko

log = logging.getLogger(__name__)

PLATFORM_COMMANDS = {
    "ios": {
        "version": "show version",
        "interfaces": "show ip interface brief",
        "ntp": "show ntp status",
        "errdisable": "show interfaces status err-disabled",
    },
    "eos": {
        "version": "show version",
        "interfaces": "show ip interface brief",
        "ntp": "show ntp status",
        "errdisable": "show interfaces status errdisabled",
    },
    "nxos": {
        "version": "show version",
        "interfaces": "show ip interface brief",
        "ntp": "show ntp status",
        "errdisable": "show interface status err-disabled",
    },
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    critical: bool = True


@dataclass
class VerificationReport:
    host: str
    results: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results if r.critical)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    def print_summary(self) -> None:
        width = 62
        print(f"\n{'=' * width}")
        print(f"  Provisioning Verification: {self.host}")
        print(f"{'=' * width}")
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            tag = " [warn]" if not r.critical else ""
            print(f"  [{status}]{tag:8s} {r.name}")
            if not r.passed or log.isEnabledFor(logging.DEBUG):
                for line in r.detail.splitlines()[:3]:
                    print(f"             {line}")
        print(f"{'=' * width}")
        print(f"  Overall: {'PASSED' if self.passed else 'FAILED'}\n")


def ssh_run(client: paramiko.SSHClient, command: str, timeout: int = 15) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if err:
        log.debug("stderr for %r: %s", command, err)
    return out


def connect(
    host: str,
    port: int,
    username: str,
    password: Optional[str],
    key_path: Optional[str],
    timeout: int,
) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = dict(
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


def run_checks(
    client: paramiko.SSHClient,
    host: str,
    platform: str,
    expect_version: Optional[str],
    expect_hostname: Optional[str],
) -> VerificationReport:
    cmds = PLATFORM_COMMANDS[platform]
    report = VerificationReport(host=host)

    report.add(CheckResult(
        name="SSH reachable",
        passed=True,
        detail="Connection established",
    ))

    version_out = ssh_run(client, cmds["version"])

    if expect_hostname:
        matched = expect_hostname.lower() in version_out.lower()
        report.add(CheckResult(
            name="Hostname matches expected",
            passed=matched,
            detail=f"expected={expect_hostname!r}",
        ))

    if expect_version:
        matched = expect_version in version_out
        snippet = next(
            (ln for ln in version_out.splitlines() if "version" in ln.lower()),
            version_out[:120],
        )
        report.add(CheckResult(
            name="Software version matches",
            passed=matched,
            detail=f"expected={expect_version!r}  found: {snippet}",
        ))

    iface_out = ssh_run(client, cmds["interfaces"])
    any_up = any(
        "up" in ln.lower() for ln in iface_out.splitlines() if ln.strip()
    )
    report.add(CheckResult(
        name="At least one interface is up",
        passed=any_up,
        detail=iface_out[:300] if not any_up else "OK",
    ))

    ntp_out = ssh_run(client, cmds["ntp"])
    ntp_synced = "synchronized" in ntp_out.lower() or "synced" in ntp_out.lower()
    report.add(CheckResult(
        name="NTP synchronized",
        passed=ntp_synced,
        detail=ntp_out.splitlines()[0] if ntp_out else "(no output)",
        critical=False,
    ))

    errdis_out = ssh_run(client, cmds["errdisable"])
    has_errdis = bool(errdis_out) and any(
        ln.strip() and not ln.startswith("Port") for ln in errdis_out.splitlines()
    )
    report.add(CheckResult(
        name="No err-disabled interfaces",
        passed=not has_errdis,
        detail=errdis_out if has_errdis else "None found",
        critical=False,
    ))

    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Verify a network device was provisioned correctly via ZTP",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", required=True, help="Device IP or hostname")
    p.add_argument("--port", type=int, default=22, help="SSH port")
    p.add_argument("--user", required=True, help="SSH username")
    p.add_argument("--password", default=None, help="SSH password")
    p.add_argument("--key", default=None, metavar="PATH", help="Path to SSH private key")
    p.add_argument("--timeout", type=int, default=30, help="SSH connect timeout (seconds)")
    p.add_argument(
        "--platform",
        default="ios",
        choices=list(PLATFORM_COMMANDS),
        help="Device NOS platform",
    )
    p.add_argument("--expect-version", default=None, help="Expected software version substring")
    p.add_argument("--expect-hostname", default=None, help="Expected hostname substring")
    p.add_argument("--retry", type=int, default=3, help="Connection retry attempts")
    p.add_argument("--retry-delay", type=int, default=30, help="Seconds between retries")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.password and not args.key:
        log.error("Provide --password or --key for authentication")
        sys.exit(2)

    client = None
    last_exc: Optional[Exception] = None

    for attempt in range(1, args.retry + 1):
        try:
            log.info("Connecting to %s (attempt %d/%d)", args.host, attempt, args.retry)
            client = connect(
                args.host, args.port, args.user, args.password, args.key, args.timeout
            )
            break
        except Exception as exc:
            last_exc = exc
            log.warning("Connection failed: %s", exc)
            if attempt < args.retry:
                log.info("Retrying in %d s...", args.retry_delay)
                time.sleep(args.retry_delay)

    if client is None:
        log.error("Could not connect after %d attempt(s): %s", args.retry, last_exc)
        sys.exit(2)

    try:
        report = run_checks(
            client,
            host=args.host,
            platform=args.platform,
            expect_version=args.expect_version,
            expect_hostname=args.expect_hostname,
        )
    finally:
        client.close()

    report.print_summary()
    sys.exit(0 if report.passed else 1)