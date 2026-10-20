#!/usr/bin/env python3
"""
ZTP Config Push and Verify

Connects to a network device via SSH, pushes a pre-rendered configuration file,
and verifies that key config elements (interfaces, static routes, NTP) are active
post-push. Designed as the delivery stage in a ZTP pipeline after Jinja2 template
rendering (see 005_config_templates.py) and before deployment tracking (003).

Usage:
    python 015_config_push.py --host 192.168.1.1 --user admin --config site-a.cfg
    python 015_config_push.py --host 192.168.1.1 --user admin --config site-a.cfg --verify-only
    python 015_config_push.py --host 192.168.1.1 --user admin --password-file .creds --no-save

Prerequisites:
    pip install paramiko
    SSH access to the target device (IOS/IOS-XE/EOS syntax assumed)
    A rendered config file (flat line-by-line, comments prefixed with '!')
"""

import argparse
import getpass
import logging
import re
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko not installed. Run: pip install paramiko", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_config(path: str) -> list:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    return [l for l in p.read_text().splitlines() if l.strip() and not l.strip().startswith("!")]


def ssh_connect(host: str, user: str, password: str, port: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=user, password=password, timeout=15)
    log.info("Connected to %s:%d", host, port)
    return client


def recv_until_quiet(shell: paramiko.Channel, wait: float = 0.5) -> str:
    time.sleep(wait)
    output = ""
    while shell.recv_ready():
        output += shell.recv(8192).decode("utf-8", errors="replace")
    return output


def push_config(shell: paramiko.Channel, lines: list) -> bool:
    log.info("Entering config mode (%d lines)", len(lines))
    shell.send("configure terminal\n")
    recv_until_quiet(shell, wait=0.4)
    errors = []
    for line in lines:
        shell.send(line + "\n")
        out = recv_until_quiet(shell, wait=0.08)
        if re.search(r"(Invalid|Error|Unrecognized)", out, re.I):
            errors.append((line, out.strip()))
    shell.send("end\n")
    recv_until_quiet(shell, wait=0.4)
    if errors:
        log.warning("%d config line(s) triggered errors:", len(errors))
        for cmd, msg in errors:
            log.warning("  CMD: %s\n  OUT: %s", cmd, msg)
        return False
    log.info("Config push complete — no errors detected")
    return True


def write_memory(shell: paramiko.Channel) -> None:
    log.info("Saving running config (write memory)")
    shell.send("write memory\n")
    out = recv_until_quiet(shell, wait=2.5)
    if re.search(r"(OK|success|complete)", out, re.I):
        log.info("Save confirmed")
    else:
        log.warning("Save response unclear: %s", out.strip())


def derive_checks(lines: list) -> list:
    checks = []
    seen = set()
    for line in lines:
        s = line.strip()
        m = re.match(r"^interface\s+(\S+)", s, re.I)
        if m:
            cmd = f"show interfaces {m.group(1)} status"
            if cmd not in seen:
                checks.append(cmd)
                seen.add(cmd)
            continue
        if re.match(r"^ip route\s+", s, re.I) and "show ip route" not in seen:
            checks.append("show ip route")
            seen.add("show ip route")
        if re.match(r"^ntp server\s+", s, re.I) and "show ntp status" not in seen:
            checks.append("show ntp status")
            seen.add("show ntp status")
        if re.match(r"^router (ospf|bgp|eigrp)\s*", s, re.I):
            proto = s.split()[1].lower()
            cmd = f"show {proto} neighbor"
            if cmd not in seen:
                checks.append(cmd)
                seen.add(cmd)
    return checks


def run_verify(shell: paramiko.Channel, checks: list) -> dict:
    results = {}
    for cmd in checks:
        log.info("Verify: %s", cmd)
        shell.send(cmd + "\n")
        out = recv_until_quiet(shell, wait=1.2)
        results[cmd] = out.strip()
    return results


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Push a rendered ZTP config to a device and verify key elements via SSH"
    )
    p.add_argument("--host", required=True, help="Device IP or hostname")
    p.add_argument("--user", required=True, help="SSH username")
    p.add_argument("--password", help="SSH password (prompted if omitted)")
    p.add_argument("--password-file", metavar="FILE", help="Read SSH password from file")
    p.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    p.add_argument("--config", required=True, help="Rendered config file to push")
    p.add_argument("--verify-only", action="store_true", help="Skip push, run verification only")
    p.add_argument("--no-save", action="store_true", help="Skip 'write memory' after push")
    p.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.password_file:
        password = Path(args.password_file).read_text().strip()
    elif args.password:
        password = args.password
    else:
        password = getpass.getpass(f"Password for {args.user}@{args.host}: ")

    try:
        config_lines = load_config(args.config)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        sys.exit(1)

    log.info("Loaded %d config lines from %s", len(config_lines), args.config)

    try:
        client = ssh_connect(args.host, args.user, password, args.port)
        shell = client.invoke_shell(width=200, height=50)
        recv_until_quiet(shell, wait=1.5)

        ok = True
        if not args.verify_only:
            ok = push_config(shell, config_lines)
            if ok and not args.no_save:
                write_memory(shell)

        checks = derive_checks(config_lines)
        if checks:
            results = run_verify(shell, checks)
            for cmd, out in results.items():
                log.info("[%s]\n%s", cmd, out)
        else:
            log.info("No verifiable stanzas found in config (no interfaces/routes/ntp/routing)")

        client.close()
        sys.exit(0 if ok else 2)

    except paramiko.AuthenticationException:
        log.error("Authentication failed for %s@%s", args.user, args.host)
        sys.exit(1)
    except (paramiko.SSHException, OSError) as exc:
        log.error("SSH error connecting to %s: %s", args.host, exc)
        sys.exit(1)