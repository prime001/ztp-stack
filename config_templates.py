#!/usr/bin/env python3
"""
005_config_templates.py — ZTP Stack: Network Device Config Template Renderer

Renders per-device startup configurations from parameterized templates for
Cisco IOS/IOS-XE, Arista EOS, and JunOS platforms.  Designed for use in
a ZTP pipeline: consume a CSV device inventory, produce ready-to-push
config files, or stream output for direct TFTP/SCP staging.

Usage:
    Single device (preview to stdout):
        python 005_config_templates.py \\
            --hostname sw-access-01 --platform ios \\
            --mgmt-ip 10.0.1.10/24 --mgmt-gateway 10.0.1.1 \\
            --loopback-ip 10.255.0.10/32 --preview

    Batch from inventory CSV:
        python 005_config_templates.py \\
            --inventory devices.csv --output-dir ./rendered

    CSV columns: hostname, platform, mgmt_ip, mgmt_gateway, loopback_ip
    Supported platforms: ios, iosxe, eos, junos, generic

Prerequisites:
    Python 3.8+  —  no third-party dependencies (uses stdlib string.Template)
"""

import argparse
import csv
import logging
import sys
from pathlib import Path
from string import Template
from typing import Optional

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform templates
# ---------------------------------------------------------------------------

_IOS = Template("""\
! Rendered by 005_config_templates.py  —  DO NOT EDIT MANUALLY
hostname $hostname
!
interface GigabitEthernet0/0
 description MGMT
 ip address $mgmt_addr $mgmt_mask
 no shutdown
!
ip default-gateway $mgmt_gateway
!
${loopback_stanza}ip ssh version 2
no ip http server
no ip http secure-server
!
line vty 0 4
 transport input ssh
 login local
!
end
""")

_EOS = Template("""\
! Rendered by 005_config_templates.py  —  DO NOT EDIT MANUALLY
hostname $hostname
!
interface Management1
   description MGMT
   ip address $mgmt_ip
!
ip route 0.0.0.0/0 $mgmt_gateway
!
${loopback_stanza}management ssh
   idle-timeout 10
!
end
""")

_JUNOS = Template("""\
# Rendered by 005_config_templates.py  —  DO NOT EDIT MANUALLY
set system host-name $hostname
set interfaces em0 description MGMT
set interfaces em0 unit 0 family inet address $mgmt_ip
set routing-options static route 0.0.0.0/0 next-hop $mgmt_gateway
${loopback_stanza}""")

PLATFORMS: dict[str, Template] = {
    "ios": _IOS,
    "iosxe": _IOS,
    "eos": _EOS,
    "junos": _JUNOS,
    "generic": _JUNOS,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cidr_to_addr_mask(cidr: str) -> tuple[str, str]:
    addr, prefix = cidr.split("/")
    bits = int(prefix)
    mask_int = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    mask = ".".join(str((mask_int >> (8 * i)) & 0xFF) for i in reversed(range(4)))
    return addr, mask


def _loopback_stanza(platform: str, loopback_ip: Optional[str]) -> str:
    if not loopback_ip:
        return ""
    addr = loopback_ip.split("/")[0]
    if platform in ("ios", "iosxe"):
        _, mask = _cidr_to_addr_mask(loopback_ip) if "/" in loopback_ip else (addr, "255.255.255.255")
        return f"interface Loopback0\n ip address {addr} {mask}\n!\n"
    if platform == "eos":
        return f"interface Loopback0\n   ip address {loopback_ip}\n!\n"
    return f"set interfaces lo0 unit 0 family inet address {addr}/32\n"


def render(device: dict) -> str:
    platform = device.get("platform", "generic").lower()
    tmpl = PLATFORMS.get(platform, PLATFORMS["generic"])
    mgmt_ip = device["mgmt_ip"]
    mgmt_addr, mgmt_mask = _cidr_to_addr_mask(mgmt_ip) if "/" in mgmt_ip else (mgmt_ip, "255.255.255.255")
    return tmpl.substitute(
        hostname=device["hostname"],
        platform=platform,
        mgmt_ip=mgmt_ip,
        mgmt_addr=mgmt_addr,
        mgmt_mask=mgmt_mask,
        mgmt_gateway=device["mgmt_gateway"],
        loopback_stanza=_loopback_stanza(platform, device.get("loopback_ip") or ""),
    )


def load_inventory(path: str) -> list[dict]:
    with open(path, newline="") as fh:
        return [{k.strip(): v.strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def write_config(hostname: str, content: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"{hostname}.cfg"
    dest.write_text(content)
    return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render ZTP device configs from built-in templates.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inventory", metavar="CSV", help="CSV inventory file (batch mode)")
    mode.add_argument("--hostname", help="Single device hostname")
    p.add_argument("--platform", default="generic", choices=list(PLATFORMS),
                   help="Device OS platform (default: generic)")
    p.add_argument("--mgmt-ip", dest="mgmt_ip", metavar="CIDR",
                   help="Management IP in CIDR notation, e.g. 10.0.1.10/24")
    p.add_argument("--mgmt-gateway", dest="mgmt_gateway", metavar="IP",
                   help="Management default gateway")
    p.add_argument("--loopback-ip", dest="loopback_ip", default="", metavar="CIDR",
                   help="Loopback0 IP in CIDR notation (optional)")
    p.add_argument("--output-dir", dest="output_dir", default="./rendered", metavar="DIR",
                   help="Directory for rendered configs (default: ./rendered)")
    p.add_argument("--preview", action="store_true",
                   help="Print rendered config(s) to stdout; do not write files")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.inventory:
        try:
            devices = load_inventory(args.inventory)
        except FileNotFoundError:
            LOG.error("Inventory file not found: %s", args.inventory)
            return 1
        except (KeyError, csv.Error) as exc:
            LOG.error("Failed to parse inventory: %s", exc)
            return 1
    else:
        if not args.mgmt_ip or not args.mgmt_gateway:
            build_parser().error("--mgmt-ip and --mgmt-gateway are required in single-device mode")
        devices = [{"hostname": args.hostname, "platform": args.platform,
                    "mgmt_ip": args.mgmt_ip, "mgmt_gateway": args.mgmt_gateway,
                    "loopback_ip": args.loopback_ip}]

    errors = 0
    for device in devices:
        hostname = device.get("hostname", "<unknown>")
        try:
            config = render(device)
        except KeyError as exc:
            LOG.error("[%s] Missing required field: %s", hostname, exc)
            errors += 1
            continue
        except ValueError as exc:
            LOG.error("[%s] Render error: %s", hostname, exc)
            errors += 1
            continue

        if args.preview:
            sep = "─" * 62
            print(f"\n{sep}\n  {hostname}  ({device.get('platform', 'generic')})\n{sep}")
            print(config)
        else:
            out = write_config(hostname, config, Path(args.output_dir))
            LOG.info("[%s] written → %s", hostname, out)

    if errors:
        LOG.warning("%d device(s) failed to render", errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())