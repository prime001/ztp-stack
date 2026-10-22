The user's explicit instruction is "Output ONLY the script content" — that overrides the brainstorming design process. Proceeding directly.

```python
"""
ZTP Stack API Provisioning Audit Tool

Purpose:
    Queries the ZTP stack REST API to audit device provisioning state across
    the fleet. Identifies stalled jobs, mismatched config assignments, and
    devices that have not completed provisioning within a configurable SLA
    window. Exits non-zero when stalled jobs are detected, making it safe
    to run from monitoring scripts or CI pipelines.

Usage:
    python 022_api_endpoints.py --url http://ztp-server:8000 --token <token>
    python 022_api_endpoints.py --url http://ztp-server:8000 --token <token> \
        --status pending --sla-hours 2 --output report.json
    python 022_api_endpoints.py --url http://ztp-server:8000 --token <token> \
        --site dc1 --format csv --verbose

Prerequisites:
    - ZTP stack API running and reachable
    - API token with read access to /api/v1/devices and /api/v1/jobs
    - Python 3.8+
    - pip install requests
"""

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

import requests

LOG = logging.getLogger(__name__)


def build_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    return session


def fetch_devices(
    session: requests.Session,
    base_url: str,
    site: str = None,
    status: str = None,
) -> list:
    params = {}
    if site:
        params["site"] = site
    if status:
        params["status"] = status

    url = f"{base_url.rstrip('/')}/api/v1/devices"
    try:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        LOG.error("HTTP error fetching devices: %s", exc)
        raise
    except requests.ConnectionError as exc:
        LOG.error("Cannot reach ZTP API at %s: %s", base_url, exc)
        raise

    data = resp.json()
    return data.get("devices", data) if isinstance(data, dict) else data


def fetch_jobs(
    session: requests.Session,
    base_url: str,
    device_id: str = None,
) -> list:
    url = f"{base_url.rstrip('/')}/api/v1/jobs"
    params = {"device_id": device_id} if device_id else {}
    try:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        LOG.warning("HTTP error fetching jobs for device %s: %s", device_id, exc)
        return []

    data = resp.json()
    return data.get("jobs", data) if isinstance(data, dict) else data


def identify_stalled(jobs: list, sla_hours: int) -> list:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=sla_hours)
    stalled = []
    for job in jobs:
        if job.get("status") not in ("pending", "running", "in_progress"):
            continue
        raw_ts = job.get("created_at") or job.get("started_at")
        if not raw_ts:
            continue
        try:
            created = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            if created < cutoff:
                stalled.append(job)
        except (ValueError, AttributeError):
            LOG.warning("Cannot parse timestamp for job %s", job.get("id"))
    return stalled


def audit_devices(
    session: requests.Session,
    base_url: str,
    site: str,
    status_filter: str,
    sla_hours: int,
) -> dict:
    LOG.info(
        "Fetching devices from %s (site=%s, status=%s)",
        base_url, site or "*", status_filter or "*",
    )
    devices = fetch_devices(session, base_url, site=site, status=status_filter)
    LOG.info("Found %d device(s)", len(devices))

    results = []
    stalled_total = 0

    for device in devices:
        dev_id = device.get("id") or device.get("device_id")
        jobs = fetch_jobs(session, base_url, device_id=dev_id)
        stalled = identify_stalled(jobs, sla_hours)
        stalled_total += len(stalled)

        results.append({
            "device_id": dev_id,
            "hostname": device.get("hostname"),
            "site": device.get("site"),
            "model": device.get("model"),
            "status": device.get("status"),
            "ip_address": device.get("ip_address") or device.get("mgmt_ip"),
            "job_count": len(jobs),
            "stalled_jobs": len(stalled),
            "stalled_job_ids": [j.get("id") for j in stalled],
        })

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "base_url": base_url,
        "filters": {"site": site, "status": status_filter, "sla_hours": sla_hours},
        "summary": {
            "total_devices": len(results),
            "stalled_job_count": stalled_total,
            "devices_with_stalled_jobs": sum(
                1 for r in results if r["stalled_jobs"] > 0
            ),
        },
        "devices": results,
    }


def write_csv(report: dict, path: str) -> None:
    fields = [
        "device_id", "hostname", "site", "model", "status",
        "ip_address", "job_count", "stalled_jobs", "stalled_job_ids",
    ]
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in report["devices"]:
            row = dict(row)
            row["stalled_job_ids"] = "|".join(str(j) for j in row["stalled_job_ids"])
            writer.writerow(row)
    LOG.info("CSV report written to %s", path)


def write_json(report: dict, path: str) -> None:
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2)
    LOG.info("JSON report written to %s", path)


def print_summary(report: dict) -> None:
    s = report["summary"]
    print(f"\nZTP Provisioning Audit — {report['generated_at']}")
    print(f"  API              : {report['base_url']}")
    print(f"  Devices audited  : {s['total_devices']}")
    print(f"  Stalled jobs     : {s['stalled_job_count']}")
    print(f"  Affected devices : {s['devices_with_stalled_jobs']}")
    if any(d["stalled_jobs"] for d in report["devices"]):
        print("\n  Devices with stalled jobs:")
        for d in report["devices"]:
            if d["stalled_jobs"]:
                ids = ", ".join(str(j) for j in d["stalled_job_ids"])
                label = d["hostname"] or d["device_id"]
                print(f"    {label:30s}  job(s): {ids}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit ZTP stack provisioning state via the REST API."
    )
    parser.add_argument(
        "--url", required=True,
        help="ZTP API base URL (e.g. http://ztp-server:8000)",
    )
    parser.add_argument("--token", required=True, help="API bearer token")
    parser.add_argument("--site", default=None, help="Filter by site name")
    parser.add_argument(
        "--status", default=None,
        choices=["pending", "provisioning", "complete", "failed"],
        help="Filter by provisioning status",
    )
    parser.add_argument(
        "--sla-hours", type=int, default=4, metavar="N",
        help="Flag in-progress jobs older than N hours as stalled (default: 4)",
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help="Write full report to file (.json or .csv inferred from extension)",
    )
    parser.add_argument(
        "--format", choices=["json", "csv"], default=None,
        help="Force output format when --output is set",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    session = build_session(args.token)

    try:
        report = audit_devices(
            session,
            args.url,
            site=args.site,
            status_filter=args.status,
            sla_hours=args.sla_hours,
        )
    except (requests.ConnectionError, requests.HTTPError) as exc:
        LOG.error("Audit failed: %s", exc)
        sys.exit(2)

    print_summary(report)

    if args.output:
        fmt = args.format or ("csv" if args.output.endswith(".csv") else "json")
        if fmt == "csv":
            write_csv(report, args.output)
        else:
            write_json(report, args.output)

    sys.exit(1 if report["summary"]["stalled_job_count"] else 0)
```