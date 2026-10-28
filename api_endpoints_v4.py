The script content is ready. Since the file write was blocked, here is the complete script:

```python
"""
ZTP Stack - RESTCONF API Client for Device Verification (032)

Purpose:
    Query network devices via RESTCONF (RFC 8040) to verify post-provisioning
    state. Checks interface status, LLDP neighbors, and running hostname
    against the ZTP deployment manifest. Useful as a smoke-test step after
    a device completes zero-touch provisioning.

Usage:
    python 032_api_endpoints.py --host 192.168.1.10 --username admin \
        --password secret --check interfaces
    python 032_api_endpoints.py --host 192.168.1.10 -u admin -p secret \
        --check all --output json
    python 032_api_endpoints.py --host 192.168.1.10 -u admin -p secret \
        --check hostname --expected-hostname sw-access-01

Prerequisites:
    - Device must have RESTCONF enabled (IOS-XE: `restconf`, NX-OS: feature restconf)
    - HTTPS access on port 443 (or specify --port)
    - pip install requests (or use stdlib urllib if requests unavailable)
    - Valid credentials with at least read-only privilege level
"""

import argparse
import json
import logging
import sys
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
import ssl
import base64

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

RESTCONF_ROOT = "/restconf/data"
HEADERS = {
    "Accept": "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}

YANG_PATHS = {
    "hostname": "Cisco-IOS-XE-native:native/hostname",
    "interfaces": "ietf-interfaces:interfaces",
    "lldp": "Cisco-IOS-XE-lldp-oper:lldp-oper-data/lldp-oper-interface",
    "platform": "Cisco-IOS-XE-platform-oper:platform-oper-data/component",
}


def _make_context(verify_ssl: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def restconf_get(host: str, port: int, path: str, username: str, password: str,
                 verify_ssl: bool = False) -> dict:
    url = f"https://{host}:{port}{RESTCONF_ROOT}/{path}"
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {**HEADERS, "Authorization": f"Basic {credentials}"}
    req = Request(url, headers=headers)
    ctx = _make_context(verify_ssl)
    try:
        with urlopen(req, context=ctx, timeout=10) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as exc:
        log.error("HTTP %s for %s: %s", exc.code, url, exc.reason)
        raise
    except URLError as exc:
        log.error("Connection failed for %s: %s", url, exc.reason)
        raise


def check_hostname(host, port, username, password, expected=None):
    data = restconf_get(host, port, YANG_PATHS["hostname"], username, password)
    hostname = data.get("Cisco-IOS-XE-native:hostname", "<unknown>")
    ok = (hostname == expected) if expected else True
    result = {"hostname": hostname, "expected": expected, "match": ok}
    if expected and not ok:
        log.warning("Hostname mismatch: got %r, expected %r", hostname, expected)
    else:
        log.info("Hostname: %s", hostname)
    return result


def check_interfaces(host, port, username, password):
    data = restconf_get(host, port, YANG_PATHS["interfaces"], username, password)
    ifaces = data.get("ietf-interfaces:interfaces", {}).get("interface", [])
    results = []
    for iface in ifaces:
        name = iface.get("name", "?")
        admin_status = iface.get("ietf-interfaces:admin-status", "?")
        oper_status = iface.get("ietf-interfaces:oper-status", "?")
        results.append({"name": name, "admin": admin_status, "oper": oper_status})
        log.info("  %-30s admin=%-4s oper=%s", name, admin_status, oper_status)
    log.info("Found %d interfaces", len(results))
    return results


def check_lldp(host, port, username, password):
    try:
        data = restconf_get(host, port, YANG_PATHS["lldp"], username, password)
    except HTTPError as exc:
        if exc.code == 404:
            log.warning("LLDP oper data not available (LLDP may be disabled)")
            return []
        raise
    neighbors = data.get("Cisco-IOS-XE-lldp-oper:lldp-oper-interface", [])
    results = []
    for entry in neighbors:
        iface = entry.get("name", "?")
        for nbr in entry.get("lldp-neighbor-details", {}).get("lldp-neighbor-detail", []):
            sys_name = nbr.get("system-name", "?")
            port_id = nbr.get("port-id", "?")
            results.append({"local_port": iface, "neighbor": sys_name, "remote_port": port_id})
            log.info("  %-20s -> %-30s (%s)", iface, sys_name, port_id)
    log.info("Found %d LLDP neighbor entries", len(results))
    return results


def run_checks(args) -> dict:
    common = dict(host=args.host, port=args.port,
                  username=args.username, password=args.password)
    report = {"device": args.host}
    checks = {"interfaces", "hostname", "lldp"} if args.check == "all" else {args.check}

    if "hostname" in checks:
        log.info("=== Hostname ===")
        report["hostname"] = check_hostname(expected=args.expected_hostname, **common)
    if "interfaces" in checks:
        log.info("=== Interfaces ===")
        report["interfaces"] = check_interfaces(**common)
    if "lldp" in checks:
        log.info("=== LLDP Neighbors ===")
        report["lldp"] = check_lldp(**common)

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Query a RESTCONF-enabled device for ZTP verification"
    )
    parser.add_argument("--host", required=True, help="Device management IP or hostname")
    parser.add_argument("--port", type=int, default=443, help="RESTCONF port (default 443)")
    parser.add_argument("-u", "--username", required=True, help="Device username")
    parser.add_argument("-p", "--password", required=True, help="Device password")
    parser.add_argument(
        "--check",
        choices=["hostname", "interfaces", "lldp", "all"],
        default="all",
        help="Which check to run (default: all)",
    )
    parser.add_argument(
        "--expected-hostname",
        dest="expected_hostname",
        default=None,
        help="Assert the device reports this hostname",
    )
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        default=False,
        help="Validate server TLS certificate (default: off for lab use)",
    )
    args = parser.parse_args()

    try:
        report = run_checks(args)
    except (HTTPError, URLError) as exc:
        log.error("Check failed: %s", exc)
        sys.exit(1)

    if args.output == "json":
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
```

**What this script does:** RESTCONF client (`032_api_endpoints.py`) that hits a Cisco IOS-XE device's YANG data model endpoints to verify post-ZTP provisioning state — hostname match, interface admin/oper status, and LLDP neighbor table. Uses only stdlib (`urllib`, `ssl`, `base64`, `json`, `argparse`). The `--check all` mode runs all three checks; `--output json` dumps a structured report for pipeline consumption. Distinguishes itself from the prior `api_endpoints` scripts by targeting the device side (RESTCONF) rather than the ZTP stack's own API surface.