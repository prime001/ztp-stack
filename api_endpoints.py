The user wants the script content output directly. Here it is:

"""
RESTCONF Client for ZTP Post-Provisioning Verification
=======================================================
Queries and configures network devices via RESTCONF (RFC 8040) using
IOS-XE / NX-OS compatible YANG paths. Designed for use after ZTP
bootstraps a device to verify provisioning state and push day-1 config.

Usage:
    python 002_restconf_client.py --host 192.168.1.1 --user admin --password secret get-hostname
    python 002_restconf_client.py --host 192.168.1.1 --user admin --password secret get-interfaces
    python 002_restconf_client.py --host 192.168.1.1 --user admin --password secret \
        put-config --xpath "Cisco-IOS-XE-native:native/hostname" --data '{"hostname": "edge-01"}'
    python 002_restconf_client.py --host 192.168.1.1 --user admin --password secret verify

Prerequisites:
    pip install requests
    RESTCONF must be enabled on the device:
        conf t
         restconf
         ip http secure-server
         ip http authentication local
"""

import argparse
import json
import logging
import sys
from typing import Any

import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import ConnectionError, HTTPError, Timeout

RESTCONF_ROOT = "/restconf/data"
YANG_HOSTNAME = "Cisco-IOS-XE-native:native/hostname"
YANG_INTERFACES = "ietf-interfaces:interfaces"
YANG_PLATFORM = "Cisco-IOS-XE-device-hardware-oper:device-hardware-data"

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def make_session(user: str, password: str, verify_ssl: bool) -> requests.Session:
    session = requests.Session()
    session.auth = HTTPBasicAuth(user, password)
    session.headers.update(
        {
            "Accept": "application/yang-data+json",
            "Content-Type": "application/yang-data+json",
        }
    )
    session.verify = verify_ssl
    return session


def restconf_get(session: requests.Session, base_url: str, xpath: str) -> dict[str, Any]:
    url = f"{base_url}{RESTCONF_ROOT}/{xpath}"
    log.debug("GET %s", url)
    try:
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Timeout:
        log.error("Timed out connecting to %s", base_url)
        sys.exit(1)
    except ConnectionError as exc:
        log.error("Connection failed: %s", exc)
        sys.exit(1)
    except HTTPError as exc:
        log.error("HTTP %s on GET %s: %s", resp.status_code, url, resp.text[:200])
        raise


def restconf_put(
    session: requests.Session, base_url: str, xpath: str, payload: dict[str, Any]
) -> int:
    url = f"{base_url}{RESTCONF_ROOT}/{xpath}"
    log.debug("PUT %s payload=%s", url, json.dumps(payload))
    try:
        resp = session.put(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("PUT %s -> HTTP %s", xpath, resp.status_code)
        return resp.status_code
    except Timeout:
        log.error("Timed out connecting to %s", base_url)
        sys.exit(1)
    except ConnectionError as exc:
        log.error("Connection failed: %s", exc)
        sys.exit(1)
    except HTTPError as exc:
        log.error("HTTP %s on PUT %s: %s", resp.status_code, url, resp.text[:200])
        raise


def cmd_get_hostname(session: requests.Session, base_url: str) -> None:
    data = restconf_get(session, base_url, YANG_HOSTNAME)
    hostname = data.get("Cisco-IOS-XE-native:hostname", "<unknown>")
    print(f"hostname: {hostname}")


def cmd_get_interfaces(session: requests.Session, base_url: str) -> None:
    data = restconf_get(session, base_url, YANG_INTERFACES)
    interfaces = data.get("ietf-interfaces:interfaces", {}).get("interface", [])
    print(f"{'Name':<30} {'Type':<35} {'Admin':<8} {'Oper'}")
    print("-" * 85)
    for iface in interfaces:
        name = iface.get("name", "")
        itype = iface.get("type", "").split(":")[-1]
        admin = iface.get("enabled", True) and "up" or "down"
        oper = iface.get("oper-status", "unknown")
        print(f"{name:<30} {itype:<35} {admin:<8} {oper}")


def cmd_put_config(
    session: requests.Session, base_url: str, xpath: str, data_json: str
) -> None:
    try:
        payload = json.loads(data_json)
    except json.JSONDecodeError as exc:
        log.error("Invalid JSON for --data: %s", exc)
        sys.exit(1)
    restconf_put(session, base_url, xpath, payload)


def cmd_verify(session: requests.Session, base_url: str) -> None:
    checks = [
        ("hostname reachable", YANG_HOSTNAME),
        ("interfaces model present", YANG_INTERFACES),
    ]
    all_ok = True
    for label, xpath in checks:
        try:
            restconf_get(session, base_url, xpath)
            print(f"  [PASS] {label}")
        except HTTPError:
            print(f"  [FAIL] {label}")
            all_ok = False
    sys.exit(0 if all_ok else 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RESTCONF client for ZTP post-provisioning verification"
    )
    parser.add_argument("--host", required=True, help="Device IP or hostname")
    parser.add_argument("--port", type=int, default=443, help="HTTPS port (default: 443)")
    parser.add_argument("--user", required=True, help="RESTCONF username")
    parser.add_argument("--password", required=True, help="RESTCONF password")
    parser.add_argument(
        "--no-verify-ssl",
        dest="verify_ssl",
        action="store_false",
        default=True,
        help="Disable TLS certificate verification (lab use only)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging"
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("get-hostname", help="Print device hostname")
    sub.add_parser("get-interfaces", help="List interfaces with admin/oper state")
    sub.add_parser("verify", help="Run provisioning readiness checks")

    put_p = sub.add_parser("put-config", help="PUT arbitrary YANG data")
    put_p.add_argument("--xpath", required=True, help="YANG path below /restconf/data/")
    put_p.add_argument("--data", required=True, help="JSON payload string")

    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    base_url = f"https://{args.host}:{args.port}"
    session = make_session(args.user, args.password, args.verify_ssl)

    if args.command == "get-hostname":
        cmd_get_hostname(session, base_url)
    elif args.command == "get-interfaces":
        cmd_get_interfaces(session, base_url)
    elif args.command == "verify":
        cmd_verify(session, base_url)
    elif args.command == "put-config":
        cmd_put_config(session, base_url, args.xpath, args.data)