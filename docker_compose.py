ZTP Stack Manager — docker-compose orchestration and health monitoring for a
Zero Touch Provisioning (ZTP) environment.

Purpose:
    Spin up, tear down, and inspect the ZTP service stack (DHCP, TFTP, HTTP
    file server) via docker compose, then optionally poll a newly-provisioned
    device's progress through the ZTP HTTP status endpoint.

Usage:
    python ztp_manager.py up
    python ztp_manager.py down [--volumes]
    python ztp_manager.py status
    python ztp_manager.py poll --host 192.168.1.10 --device spine-01 [--timeout 600]

Prerequisites:
    - Docker Engine and the Compose v2 plugin installed and in PATH.
    - A docker-compose.yml in the working directory (override with --compose-file).
    - The ZTP HTTP server exposes:
          GET /api/provision/<hostname>
      returning JSON: {"status": "pending|complete|failed", "stage": "...", "ts": "..."}
"""

import argparse
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
logging.basicConfig(format=LOG_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("ztp_manager")


# ---------------------------------------------------------------------------
# docker compose helpers
# ---------------------------------------------------------------------------

def _compose(args: list[str], compose_file: str) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", compose_file] + args
    log.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def stack_up(compose_file: str) -> int:
    result = _compose(["up", "--build", "-d"], compose_file)
    if result.returncode != 0:
        log.error("stack up failed:\n%s", result.stderr.strip())
    else:
        log.info("ZTP stack started.")
    return result.returncode


def stack_down(compose_file: str, volumes: bool = False) -> int:
    args = ["down", "-v"] if volumes else ["down"]
    result = _compose(args, compose_file)
    if result.returncode != 0:
        log.error("stack down failed:\n%s", result.stderr.strip())
    else:
        log.info("ZTP stack stopped.")
    return result.returncode


def stack_status(compose_file: str) -> int:
    result = _compose(["ps", "--format", "json"], compose_file)
    if result.returncode != 0:
        log.error("status check failed:\n%s", result.stderr.strip())
        return result.returncode

    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        log.warning("No containers found — is the stack running?")
        return 0

    try:
        containers = [json.loads(ln) for ln in lines]
    except json.JSONDecodeError:
        print(result.stdout)
        return 0

    fmt = "{:<32} {:<22} {:<18} {:<10}"
    print(fmt.format("NAME", "IMAGE", "STATUS", "HEALTH"))
    print("-" * 84)
    for c in containers:
        print(fmt.format(
            c.get("Name", c.get("name", "?")),
            c.get("Image", c.get("image", "?")),
            c.get("Status", c.get("status", "?")),
            c.get("Health", c.get("health", "-")),
        ))
    return 0


# ---------------------------------------------------------------------------
# Provision poll
# ---------------------------------------------------------------------------

def _get_provision_status(host: str, port: int, hostname: str) -> dict:
    url = f"http://{host}:{port}/api/provision/{hostname}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} querying {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc


def poll_device(host: str, port: int, hostname: str, timeout: int, interval: int) -> int:
    """
    Poll the ZTP HTTP endpoint until the device reports complete/failed or
    the wall-clock timeout elapses.  Returns 0 on success, 1 otherwise.
    """
    deadline = time.monotonic() + timeout
    log.info("Polling %s:%d for device %r (timeout=%ds, interval=%ds)",
             host, port, hostname, timeout, interval)

    while time.monotonic() < deadline:
        try:
            data = _get_provision_status(host, port, hostname)
        except RuntimeError as exc:
            log.warning("Poll error: %s - retrying in %ds", exc, interval)
            time.sleep(interval)
            continue

        status = data.get("status", "unknown")
        stage = data.get("stage", "")
        ts = data.get("ts", "")
        log.info("[%s] status=%-10s stage=%s", ts or "-", status, stage or "-")

        if status == "complete":
            log.info("Device %r provisioned successfully.", hostname)
            return 0
        if status == "failed":
            log.error("Device %r provisioning FAILED at stage: %s", hostname, stage)
            return 1

        time.sleep(interval)

    log.error("Timed out after %ds waiting for %r to provision.", timeout, hostname)
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n")[1].strip(),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--compose-file", default="docker-compose.yml",
                   help="Path to the docker-compose file")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable debug-level logging")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("up", help="Build images and start the ZTP stack")

    dn = sub.add_parser("down", help="Stop and remove containers")
    dn.add_argument("--volumes", action="store_true",
                    help="Also remove named volumes (destructive)")

    sub.add_parser("status", help="Print container status table")

    pl = sub.add_parser("poll", help="Poll device provisioning progress")
    pl.add_argument("--host", required=True, help="ZTP HTTP server address")
    pl.add_argument("--port", type=int, default=8080, help="ZTP HTTP server port")
    pl.add_argument("--device", required=True, help="Device hostname to poll")
    pl.add_argument("--timeout", type=int, default=600,
                    help="Max seconds to wait for provisioning")
    pl.add_argument("--interval", type=int, default=15,
                    help="Seconds between poll attempts")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    if not Path(args.compose_file).exists():
        log.error("Compose file not found: %s", args.compose_file)
        return 1

    if args.command == "up":
        return stack_up(args.compose_file)
    if args.command == "down":
        return stack_down(args.compose_file, volumes=args.volumes)
    if args.command == "status":
        return stack_status(args.compose_file)
    if args.command == "poll":
        return poll_device(args.host, args.port, args.device,
                           args.timeout, args.interval)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())