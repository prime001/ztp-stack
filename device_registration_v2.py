The request is to generate a standalone script - I'll write it directly.

```python
"""
020_device_registration.py - Bulk Device Pre-Registration for ZTP Stack

Purpose:
    Pre-populate the ZTP database with device inventory before physical deployment.
    Reads a CSV or YAML file containing device records (MAC address, hostname,
    device type, site, and optional role metadata) and registers each device via
    the ZTP API, enabling zero-touch provisioning when devices first boot.

    This complements 010_device_registration.py (which handles the API endpoint
    and individual registration logic) by providing an operator-facing bulk import
    tool for pre-staging inventory before a rack-and-stack deployment window.

Usage:
    python 020_device_registration.py --file inventory.csv --api http://localhost:8000
    python 020_device_registration.py --file inventory.yaml --api http://ztp-server:8000 --dry-run
    python 020_device_registration.py --file inventory.csv --api http://localhost:8000 --update

Prerequisites:
    - ZTP API server running and reachable (see 007_fastapi_app.py)
    - requests library:  pip install requests
    - PyYAML (YAML only): pip install pyyaml

CSV format (header row required):
    mac_address,hostname,device_type,site,role
    aa:bb:cc:dd:ee:ff,sw-access-01,cisco_ios,dc1,access

YAML format:
    devices:
      - mac_address: aa:bb:cc:dd:ee:ff
        hostname: sw-access-01
        device_type: cisco_ios
        site: dc1
        role: access
"""

import argparse
import csv
import logging
import re
import sys
from pathlib import Path

import requests

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

REQUIRED_FIELDS = {"mac_address", "hostname", "device_type", "site"}


def normalize_mac(mac: str) -> str:
    """Return MAC as lowercase colon-separated hex; raise ValueError if malformed."""
    digits = re.sub(r"[:\-\.]", "", mac).lower()
    if len(digits) != 12 or not re.fullmatch(r"[0-9a-f]+", digits):
        raise ValueError(f"Invalid MAC address: '{mac}'")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def validate_device(record: dict) -> list:
    errors = []
    missing = REQUIRED_FIELDS - set(record.keys())
    if missing:
        errors.append(f"Missing fields: {', '.join(sorted(missing))}")
        return errors

    try:
        normalize_mac(record["mac_address"])
    except ValueError as exc:
        errors.append(str(exc))

    hostname = record["hostname"].strip()
    if not hostname or len(hostname) > 63:
        errors.append(f"Hostname length out of range: '{hostname}'")
    elif not re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?", hostname):
        errors.append(f"Hostname has invalid characters: '{hostname}'")

    return errors


def load_csv(path: Path) -> list:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [{k.strip(): v.strip() for k, v in row.items() if k} for row in reader]


def load_yaml(path: Path) -> list:
    try:
        import yaml
    except ImportError:
        log.error("PyYAML required for YAML inventory: pip install pyyaml")
        sys.exit(1)
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "devices" in data:
        return data["devices"]
    raise ValueError("YAML must be a list or a dict with a 'devices' key")


def load_inventory(path: Path) -> list:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv(path)
    if suffix in {".yaml", ".yml"}:
        return load_yaml(path)
    raise ValueError(f"Unsupported format '{suffix}' — use .csv or .yaml")


def register_device(api_url: str, device: dict, update: bool, timeout: int) -> bool:
    mac = normalize_mac(device["mac_address"])
    payload = {**device, "mac_address": mac}
    base = f"{api_url.rstrip('/')}/api/v1/devices"
    try:
        if update:
            resp = requests.put(f"{base}/{mac}", json=payload, timeout=timeout)
        else:
            resp = requests.post(base, json=payload, timeout=timeout)

        if resp.status_code in (200, 201):
            log.info("  OK   %s  ->  %s", mac, device["hostname"])
            return True
        if resp.status_code == 409 and not update:
            log.warning("  DUP  %s already registered (use --update to overwrite)", mac)
            return False
        log.error("  ERR  %s HTTP %d: %s", mac, resp.status_code, resp.text[:200])
        return False
    except requests.Timeout:
        log.error("  ERR  %s timed out after %ds", mac, timeout)
        return False
    except requests.ConnectionError as exc:
        log.error("  ERR  %s connection failed: %s", mac, exc)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Bulk pre-register devices into the ZTP database from CSV or YAML inventory.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--file", "-f", required=True, metavar="PATH",
                        help="Inventory file (.csv or .yaml)")
    parser.add_argument("--api", default="http://localhost:8000", metavar="URL",
                        help="ZTP API base URL")
    parser.add_argument("--update", action="store_true",
                        help="Overwrite existing registrations via PUT")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate records and print without registering")
    parser.add_argument("--timeout", type=int, default=10, metavar="SEC",
                        help="HTTP request timeout in seconds")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    path = Path(args.file)
    if not path.exists():
        log.error("Inventory file not found: %s", path)
        sys.exit(1)

    try:
        devices = load_inventory(path)
    except (ValueError, OSError) as exc:
        log.error("Failed to load inventory: %s", exc)
        sys.exit(1)

    log.info("Loaded %d record(s) from %s", len(devices), path)

    valid, skipped = [], 0
    for i, device in enumerate(devices, start=1):
        errors = validate_device(device)
        if errors:
            log.error("Record %d skipped — validation errors:", i)
            for err in errors:
                log.error("    %s", err)
            skipped += 1
        else:
            valid.append(device)

    if skipped:
        log.warning("%d invalid record(s) skipped", skipped)
    if not valid:
        log.error("No valid records to process")
        sys.exit(1)

    if args.dry_run:
        log.info("Dry run — %d device(s) would be registered:", len(valid))
        for d in valid:
            mac = normalize_mac(d["mac_address"])
            log.info("    %-20s  %-30s  %s @ %s", mac, d["hostname"],
                     d.get("role", "—"), d["site"])
        sys.exit(0)

    log.info("Registering %d device(s) against %s", len(valid), args.api)
    ok_count = sum(
        register_device(args.api, d, args.update, args.timeout) for d in valid
    )
    fail_count = len(valid) - ok_count

    log.info(
        "Complete: %d registered, %d failed/skipped, %d invalid (total input: %d)",
        ok_count, fail_count, skipped, len(devices),
    )
    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
```