```
"""
Kea DHCP Host Reservation Manager for ZTP Stack

Manages per-device DHCP host reservations via the Kea Control Agent REST API.
In a ZTP pipeline, reservations bind a device's MAC address to a fixed IP,
next-server (TFTP), and boot-file so every PXE boot is deterministic.

Usage:
    python 014_kea_reservation_manager.py list --subnet-id 1
    python 014_kea_reservation_manager.py add \
        --mac de:ad:be:ef:00:01 --ip 192.168.1.50 --subnet-id 1 \
        --hostname spine-01 --next-server 192.168.1.1 --boot-file grub.cfg
    python 014_kea_reservation_manager.py delete \
        --mac de:ad:be:ef:00:01 --subnet-id 1
    python 014_kea_reservation_manager.py get \
        --mac de:ad:be:ef:00:01 --subnet-id 1

Prerequisites:
    - Kea Control Agent running and reachable (default: http://localhost:8000)
    - kea-dhcp4 service active with host-cmds hook library loaded
    - Python 3.8+; no third-party packages required
"""

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def kea_command(url: str, command: str, arguments: dict[str, Any]) -> dict:
    payload = json.dumps(
        {"command": command, "service": ["dhcp4"], "arguments": arguments}
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code} from Kea Control Agent: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach Kea Control Agent at {url}: {exc.reason}") from exc

    if isinstance(body, list):
        body = body[0]

    result = body.get("result", -1)
    if result not in (0, 3):
        raise SystemExit(f"Kea error {result}: {body.get('text', 'unknown error')}")

    return body


def cmd_list(args: argparse.Namespace) -> None:
    body = kea_command(
        args.url,
        "reservation-get-all",
        {"subnet-id": args.subnet_id},
    )
    hosts = body.get("arguments", {}).get("hosts", [])
    if not hosts:
        log.info("No reservations found in subnet %d.", args.subnet_id)
        return
    if args.json:
        print(json.dumps(hosts, indent=2))
        return
    print(f"{'MAC':<20} {'IP':<18} {'Hostname':<24} Boot File")
    print("-" * 80)
    for h in hosts:
        mac = h.get("hw-address", "")
        ip = h.get("ip-address", "")
        hostname = h.get("hostname", "")
        boot = h.get("boot-file-name", "")
        print(f"{mac:<20} {ip:<18} {hostname:<24} {boot}")


def cmd_get(args: argparse.Namespace) -> None:
    body = kea_command(
        args.url,
        "reservation-get",
        {"subnet-id": args.subnet_id, "identifier-type": "hw-address", "identifier": args.mac},
    )
    if body.get("result") == 3:
        log.warning("No reservation found for %s in subnet %d.", args.mac, args.subnet_id)
        sys.exit(1)
    host = body.get("arguments", {})
    if args.json:
        print(json.dumps(host, indent=2))
        return
    for key, val in host.items():
        print(f"  {key}: {val}")


def cmd_add(args: argparse.Namespace) -> None:
    reservation: dict[str, Any] = {
        "subnet-id": args.subnet_id,
        "hw-address": args.mac,
        "ip-address": args.ip,
    }
    if args.hostname:
        reservation["hostname"] = args.hostname
    if args.next_server:
        reservation["next-server"] = args.next_server
    if args.boot_file:
        reservation["boot-file-name"] = args.boot_file
    if args.option_data:
        try:
            reservation["option-data"] = json.loads(args.option_data)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--option-data is not valid JSON: {exc}") from exc

    body = kea_command(args.url, "reservation-add", {"reservation": reservation})
    log.info("Reservation added: %s -> %s (subnet %d).", args.mac, args.ip, args.subnet_id)
    if args.json:
        print(json.dumps(body, indent=2))


def cmd_delete(args: argparse.Namespace) -> None:
    body = kea_command(
        args.url,
        "reservation-del",
        {
            "subnet-id": args.subnet_id,
            "identifier-type": "hw-address",
            "identifier": args.mac,
        },
    )
    if body.get("result") == 3:
        log.warning("No reservation found for %s; nothing deleted.", args.mac)
        sys.exit(1)
    log.info("Reservation deleted: %s from subnet %d.", args.mac, args.subnet_id)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Manage Kea DHCP host reservations for ZTP workflows."
    )
    p.add_argument("--url", default="http://localhost:8000", help="Kea Control Agent URL")
    p.add_argument("--json", action="store_true", help="Output raw JSON")

    sub = p.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("list", help="List all reservations in a subnet")
    ls.add_argument("--subnet-id", type=int, required=True)
    ls.set_defaults(func=cmd_list)

    get = sub.add_parser("get", help="Get one reservation by MAC")
    get.add_argument("--subnet-id", type=int, required=True)
    get.add_argument("--mac", required=True, help="Hardware address (de:ad:be:ef:00:01)")
    get.set_defaults(func=cmd_get)

    add = sub.add_parser("add", help="Add or replace a host reservation")
    add.add_argument("--subnet-id", type=int, required=True)
    add.add_argument("--mac", required=True)
    add.add_argument("--ip", required=True, help="Reserved IPv4 address")
    add.add_argument("--hostname", default="")
    add.add_argument("--next-server", default="", help="TFTP server IP for PXE")
    add.add_argument("--boot-file", default="", help="PXE boot filename")
    add.add_argument(
        "--option-data",
        default="",
        help='JSON array of Kea option-data objects, e.g. \'[{"name":"domain-name","data":"lab.local"}]\'',
    )
    add.set_defaults(func=cmd_add)

    rm = sub.add_parser("delete", help="Delete a host reservation by MAC")
    rm.add_argument("--subnet-id", type=int, required=True)
    rm.add_argument("--mac", required=True)
    rm.set_defaults(func=cmd_delete)

    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
```