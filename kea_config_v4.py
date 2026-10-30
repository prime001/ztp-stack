```python
"""
034_kea_lease_audit.py — Kea DHCP Lease Auditor

Queries the Kea Control Agent REST API to audit active leases, identify
stale or expired entries, and report per-subnet utilization. Useful for
validating ZTP environments where devices must receive consistent addresses.

Usage:
    python 034_kea_lease_audit.py --host 192.168.1.1 --port 8000
    python 034_kea_lease_audit.py --host 192.168.1.1 --subnet 10.0.1.0/24 --export leases.csv
    python 034_kea_lease_audit.py --host 192.168.1.1 --stale-hours 48 --verbose

Prerequisites:
    - Kea DHCP server with Control Agent enabled (kea-ctrl-agent)
    - Control Agent accessible on the specified host/port (default: 8000)
    - Python 3.8+ (stdlib only: urllib, json, csv, argparse, logging)
"""

import argparse
import csv
import json
import logging
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

LOG = logging.getLogger("kea-lease-audit")


def kea_command(base_url: str, service: str, command: str, args: dict | None = None) -> dict:
    payload = {"command": command, "service": [service]}
    if args:
        payload["arguments"] = args
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as exc:
        LOG.error("Control Agent unreachable: %s", exc)
        sys.exit(1)
    if not isinstance(result, list) or result[0].get("result", 1) != 0:
        msg = result[0].get("text", "unknown error") if result else "empty response"
        raise RuntimeError(f"Kea command failed: {msg}")
    return result[0].get("arguments", {})


def fetch_leases(base_url: str) -> list[dict]:
    LOG.debug("Fetching all DHCPv4 leases")
    args = kea_command(base_url, "dhcp4", "lease4-get-all")
    return args.get("leases", [])


def fetch_subnets(base_url: str) -> list[dict]:
    LOG.debug("Fetching subnet configuration")
    args = kea_command(base_url, "dhcp4", "subnet4-list")
    return args.get("subnets", [])


def classify_leases(leases: list[dict], stale_threshold_sec: int) -> dict:
    now = datetime.now(tz=timezone.utc).timestamp()
    buckets = {"active": [], "expired": [], "stale": []}
    for lease in leases:
        expire_ts = lease.get("expire", 0)
        if expire_ts < now:
            buckets["expired"].append(lease)
        elif (expire_ts - now) < stale_threshold_sec:
            buckets["stale"].append(lease)
        else:
            buckets["active"].append(lease)
    return buckets


def subnet_utilization(leases: list[dict], subnets: list[dict]) -> list[dict]:
    subnet_map = {s["id"]: s for s in subnets}
    counts: dict[int, int] = {}
    for lease in leases:
        sid = lease.get("subnet-id", 0)
        counts[sid] = counts.get(sid, 0) + 1
    rows = []
    for sid, subnet in subnet_map.items():
        total = subnet.get("pool-count", None)
        used = counts.get(sid, 0)
        pct = f"{used / total * 100:.1f}%" if total else "n/a"
        rows.append({
            "subnet_id": sid,
            "prefix": subnet.get("subnet", "unknown"),
            "used": used,
            "pool_total": total or "unknown",
            "utilization": pct,
        })
    return sorted(rows, key=lambda r: r["subnet_id"])


def print_report(buckets: dict, utilization: list[dict], target_subnet: str | None) -> None:
    print("\n=== Kea DHCP Lease Audit ===")
    print(f"  Active  : {len(buckets['active'])}")
    print(f"  Stale   : {len(buckets['stale'])}")
    print(f"  Expired : {len(buckets['expired'])}")

    if utilization:
        print("\n--- Subnet Utilization ---")
        header = f"{'ID':>6}  {'Prefix':<20}  {'Used':>6}  {'Total':>8}  {'Util':>7}"
        print(header)
        print("-" * len(header))
        for row in utilization:
            if target_subnet and target_subnet not in row["prefix"]:
                continue
            print(f"{row['subnet_id']:>6}  {row['prefix']:<20}  {row['used']:>6}  "
                  f"{str(row['pool_total']):>8}  {row['utilization']:>7}")

    if buckets["expired"]:
        print(f"\n[!] {len(buckets['expired'])} expired lease(s) found — consider running lease4-del")


def export_csv(leases: list[dict], path: str) -> None:
    if not leases:
        LOG.warning("No leases to export")
        return
    fields = ["ip-address", "hw-address", "hostname", "subnet-id", "expire", "state"]
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leases)
    LOG.info("Exported %d leases to %s", len(leases), path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Audit Kea DHCP leases via Control Agent API")
    p.add_argument("--host", default="127.0.0.1", help="Kea Control Agent host")
    p.add_argument("--port", type=int, default=8000, help="Kea Control Agent port")
    p.add_argument("--subnet", metavar="PREFIX", help="Filter utilization report to this subnet prefix")
    p.add_argument("--stale-hours", type=float, default=24.0,
                   help="Leases expiring within this many hours are flagged stale (default: 24)")
    p.add_argument("--export", metavar="FILE", help="Export all leases to CSV")
    p.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    base_url = f"http://{args.host}:{args.port}"
    LOG.info("Connecting to Kea Control Agent at %s", base_url)

    leases = fetch_leases(base_url)
    LOG.info("Retrieved %d leases", len(leases))

    subnets = fetch_subnets(base_url)
    buckets = classify_leases(leases, int(args.stale_hours * 3600))
    utilization = subnet_utilization(leases, subnets)

    print_report(buckets, utilization, args.subnet)

    if args.export:
        export_csv(leases, args.export)

    sys.exit(1 if buckets["expired"] else 0)
```