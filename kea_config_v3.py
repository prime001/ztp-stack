kea_lease_audit.py — Kea DHCP Lease Utilization Auditor

Queries the Kea Control Agent REST API to report on subnet utilization,
identify exhausted or underutilized subnets, and surface leases nearing
expiry. Useful for ZTP environments where DHCP availability is critical
for device bootstrapping.

Usage:
    python 024_kea_lease_audit.py --url http://kea-host:8000 [options]

Prerequisites:
    - Kea Control Agent running and reachable
    - requests library (pip install requests)
    - Optional: Kea configured with lease cmds hook library for full lease data

Options:
    --url           Kea Control Agent base URL (default: http://localhost:8000)
    --warn-pct      Subnet utilization % to flag as warning (default: 80)
    --crit-pct      Subnet utilization % to flag as critical (default: 95)
    --expiry-hours  Flag leases expiring within N hours (default: 24)
    --output        Output format: text or json (default: text)
    --timeout       HTTP request timeout in seconds (default: 10)
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("requests library required: pip install requests")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def kea_command(url, command, service, arguments=None, timeout=10):
    payload = {"command": command, "service": service}
    if arguments:
        payload["arguments"] = arguments
    try:
        resp = requests.post(url, json=payload, timeout=timeout,
                             headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            data = data[0]
        if data.get("result", 0) != 0:
            raise RuntimeError(f"Kea error [{command}]: {data.get('text', 'unknown')}")
        return data.get("arguments", {})
    except requests.RequestException as exc:
        log.error("HTTP error calling %s: %s", command, exc)
        raise


def fetch_subnet_stats(url, timeout):
    raw = kea_command(url, "stat-lease4-get", ["dhcp4"], timeout=timeout)
    return raw.get("result-set", {}).get("rows", [])


def fetch_leases(url, timeout):
    try:
        raw = kea_command(url, "lease4-get-all", ["dhcp4"], timeout=timeout)
        return raw.get("leases", [])
    except RuntimeError as exc:
        log.warning("lease4-get-all unavailable (hook not loaded?): %s", exc)
        return []


def analyze_subnets(rows, warn_pct, crit_pct):
    results = []
    for row in rows:
        subnet = row.get("subnet-id") or row.get("subnet")
        total = row.get("total-addresses", 0) or row.get("total-nas", 0)
        assigned = row.get("assigned-addresses", 0) or row.get("assigned-nas", 0)
        if total == 0:
            continue
        pct = round(assigned / total * 100, 1)
        if pct >= crit_pct:
            status = "CRITICAL"
        elif pct >= warn_pct:
            status = "WARNING"
        else:
            status = "OK"
        results.append({
            "subnet_id": subnet,
            "total": total,
            "assigned": assigned,
            "free": total - assigned,
            "utilization_pct": pct,
            "status": status,
        })
    results.sort(key=lambda r: r["utilization_pct"], reverse=True)
    return results


def analyze_expiring_leases(leases, expiry_hours):
    now = time.time()
    cutoff = now + expiry_hours * 3600
    expiring = []
    for lease in leases:
        cltt = lease.get("cltt", 0)
        valid_lft = lease.get("valid-lft", 0)
        expire_ts = cltt + valid_lft
        if expire_ts <= cutoff:
            expiring.append({
                "ip": lease.get("ip-address"),
                "hw-address": lease.get("hw-address"),
                "hostname": lease.get("hostname", ""),
                "subnet_id": lease.get("subnet-id"),
                "expires_at": datetime.fromtimestamp(expire_ts, tz=timezone.utc).isoformat(),
                "state": lease.get("state", 0),
            })
    expiring.sort(key=lambda x: x["expires_at"])
    return expiring


def render_text(subnets, expiring, expiry_hours):
    print("\n=== Kea DHCP Subnet Utilization ===")
    print(f"{'Subnet ID':<12} {'Total':>8} {'Assigned':>10} {'Free':>8} {'Pct':>7}  Status")
    print("-" * 60)
    for s in subnets:
        marker = "(!)" if s["status"] != "OK" else "   "
        print(f"{str(s['subnet_id']):<12} {s['total']:>8} {s['assigned']:>10} "
              f"{s['free']:>8} {s['utilization_pct']:>6}%  {s['status']}{marker}")

    if expiring:
        print(f"\n=== Leases Expiring Within {expiry_hours}h ===")
        for lse in expiring:
            state = "EXPIRED" if lse["state"] == 1 else "active"
            print(f"  {lse['ip']:<16}  hw={lse['hw-address']}  "
                  f"host={lse['hostname'] or '—'}  expires={lse['expires_at']}  [{state}]")
    else:
        print(f"\nNo leases expiring within {expiry_hours}h.")


def parse_args():
    p = argparse.ArgumentParser(
        description="Audit Kea DHCP lease utilization and expiry.")
    p.add_argument("--url", default="http://localhost:8000",
                   help="Kea Control Agent URL")
    p.add_argument("--warn-pct", type=int, default=80,
                   help="Utilization %% to flag WARNING")
    p.add_argument("--crit-pct", type=int, default=95,
                   help="Utilization %% to flag CRITICAL")
    p.add_argument("--expiry-hours", type=int, default=24,
                   help="Flag leases expiring within N hours")
    p.add_argument("--output", choices=["text", "json"], default="text",
                   help="Output format")
    p.add_argument("--timeout", type=int, default=10,
                   help="HTTP timeout in seconds")
    p.add_argument("--debug", action="store_true",
                   help="Enable debug logging")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    log.info("Connecting to Kea Control Agent at %s", args.url)

    try:
        subnet_rows = fetch_subnet_stats(args.url, args.timeout)
        leases = fetch_leases(args.url, args.timeout)
    except Exception as exc:
        log.error("Failed to collect data from Kea: %s", exc)
        sys.exit(1)

    subnets = analyze_subnets(subnet_rows, args.warn_pct, args.crit_pct)
    expiring = analyze_expiring_leases(leases, args.expiry_hours)

    if args.output == "json":
        print(json.dumps({"subnets": subnets, "expiring_leases": expiring}, indent=2))
    else:
        render_text(subnets, expiring, args.expiry_hours)

    critical = [s for s in subnets if s["status"] == "CRITICAL"]
    if critical:
        log.warning("%d subnet(s) at critical utilization", len(critical))
        sys.exit(2)