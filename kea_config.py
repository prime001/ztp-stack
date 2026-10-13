The repo context is a portfolio description — the script output goes directly to the user. Let me write the Kea DHCP configuration script.

```python
"""
004_kea_config.py — ISC Kea DHCP Host-Reservation Manager

Manages per-host reservations in an ISC Kea DHCP server via the Kea Control
Agent REST API. In a ZTP pipeline, every device needs a predictable IP at
boot time; this script is the authoritative tool for seeding and auditing
those MAC→IP mappings without editing kea-dhcp4.conf by hand.

Usage:
    # List all reservations in subnet 0
    python 004_kea_config.py list --host 192.168.1.1 --port 8080

    # Add a reservation (subnet-id auto-detected from --subnet)
    python 004_kea_config.py add \\
        --host 192.168.1.1 --port 8080 \\
        --mac aa:bb:cc:dd:ee:ff --ip 192.168.1.50 \\
        --hostname sw-access-01 --subnet 0

    # Remove a reservation by MAC
    python 004_kea_config.py remove \\
        --host 192.168.1.1 --port 8080 \\
        --mac aa:bb:cc:dd:ee:ff --subnet 0

    # Bulk-load reservations from a CSV (mac,ip,hostname columns)
    python 004_kea_config.py sync \\
        --host 192.168.1.1 --port 8080 \\
        --csv devices.csv --subnet 0

Prerequisites:
    pip install requests
    Kea Control Agent must be running and reachable on --host:--port.
    The agent must have the 'subnet_cmds' and 'host_cmds' hook libraries
    loaded (standard in Kea >= 1.8).
"""

import argparse
import csv
import json
import logging
import sys

import requests

log = logging.getLogger("kea_config")


class KeaClient:
    def __init__(self, host: str, port: int, timeout: int = 10):
        self.url = f"http://{host}:{port}"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"

    def _command(self, command: str, service: list[str], arguments: dict | None = None) -> dict:
        payload = {"command": command, "service": service}
        if arguments is not None:
            payload["arguments"] = arguments
        try:
            resp = self.session.post(self.url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.ConnectionError as exc:
            log.error("Cannot reach Kea agent at %s: %s", self.url, exc)
            sys.exit(1)
        except requests.HTTPError as exc:
            log.error("HTTP error from Kea agent: %s", exc)
            sys.exit(1)

        body = resp.json()
        if not body:
            log.error("Empty response from Kea agent")
            sys.exit(1)

        result = body[0]
        if result.get("result") != 0:
            log.error("Kea command failed: %s", result.get("text", "unknown error"))
            sys.exit(1)

        return result.get("arguments", {})

    def list_reservations(self, subnet_id: int) -> list[dict]:
        args = self._command(
            "reservation-get-all",
            service=["dhcp4"],
            arguments={"subnet-id": subnet_id},
        )
        return args.get("hosts", [])

    def add_reservation(self, subnet_id: int, mac: str, ip: str, hostname: str) -> None:
        self._command(
            "reservation-add",
            service=["dhcp4"],
            arguments={
                "reservation": {
                    "subnet-id": subnet_id,
                    "hw-address": mac.lower(),
                    "ip-address": ip,
                    "hostname": hostname,
                }
            },
        )
        log.info("Added reservation: %s → %s (%s)", mac, ip, hostname)

    def remove_reservation(self, subnet_id: int, mac: str) -> None:
        self._command(
            "reservation-del",
            service=["dhcp4"],
            arguments={"subnet-id": subnet_id, "identifier-type": "hw-address", "identifier": mac.lower()},
        )
        log.info("Removed reservation for %s", mac)


def cmd_list(client: KeaClient, args: argparse.Namespace) -> None:
    hosts = client.list_reservations(args.subnet)
    if not hosts:
        print(f"No reservations in subnet-id {args.subnet}")
        return
    print(f"{'MAC':<20} {'IP':<18} {'Hostname'}")
    print("-" * 60)
    for h in hosts:
        print(f"{h.get('hw-address', ''):<20} {h.get('ip-address', ''):<18} {h.get('hostname', '')}")


def cmd_add(client: KeaClient, args: argparse.Namespace) -> None:
    client.add_reservation(args.subnet, args.mac, args.ip, args.hostname or "")


def cmd_remove(client: KeaClient, args: argparse.Namespace) -> None:
    client.remove_reservation(args.subnet, args.mac)


def cmd_sync(client: KeaClient, args: argparse.Namespace) -> None:
    existing = {h["hw-address"]: h for h in client.list_reservations(args.subnet)}
    added = skipped = errors = 0

    try:
        f = open(args.csv, newline="")
    except OSError as exc:
        log.error("Cannot open CSV: %s", exc)
        sys.exit(1)

    with f:
        reader = csv.DictReader(f)
        required = {"mac", "ip", "hostname"}
        if not required.issubset(set(reader.fieldnames or [])):
            log.error("CSV must have columns: mac, ip, hostname")
            sys.exit(1)

        for row in reader:
            mac = row["mac"].strip().lower()
            ip = row["ip"].strip()
            hostname = row["hostname"].strip()

            if mac in existing:
                log.debug("Skipping existing reservation: %s", mac)
                skipped += 1
                continue
            try:
                client.add_reservation(args.subnet, mac, ip, hostname)
                added += 1
            except SystemExit:
                errors += 1

    print(f"Sync complete — added: {added}, skipped: {skipped}, errors: {errors}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Manage ISC Kea DHCP host reservations for ZTP",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1", help="Kea Control Agent address")
    p.add_argument("--port", type=int, default=8080, help="Kea Control Agent port")
    p.add_argument("--subnet", type=int, default=0, help="Kea subnet-id")
    p.add_argument("--timeout", type=int, default=10, help="Request timeout (seconds)")
    p.add_argument("--verbose", "-v", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all reservations in a subnet")

    add_p = sub.add_parser("add", help="Add a single host reservation")
    add_p.add_argument("--mac", required=True, help="Hardware address (aa:bb:cc:dd:ee:ff)")
    add_p.add_argument("--ip", required=True, help="Reserved IP address")
    add_p.add_argument("--hostname", default="", help="Client hostname")

    rm_p = sub.add_parser("remove", help="Remove a reservation by MAC")
    rm_p.add_argument("--mac", required=True, help="Hardware address to remove")

    sync_p = sub.add_parser("sync", help="Bulk-load reservations from CSV")
    sync_p.add_argument("--csv", required=True, help="Path to CSV file (mac,ip,hostname)")

    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    client = KeaClient(args.host, args.port, args.timeout)

    dispatch = {"list": cmd_list, "add": cmd_add, "remove": cmd_remove, "sync": cmd_sync}
    dispatch[args.command](client, args)
```