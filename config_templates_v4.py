The user wants raw script output, not a file write. Here's the script:

```
"""
035_config_templates.py - ZTP Config Template Diff Auditor

Purpose:
    Renders Jinja2 configuration templates against device inventory variables
    and compares the rendered output to the running configuration retrieved
    via SSH. Produces a unified diff so operators can confirm ZTP-provisioned
    devices match expected state before sign-off.

Usage:
    python 035_config_templates.py \
        --template templates/access_switch.j2 \
        --inventory inventory.json \
        --device sw-access-01 \
        [--push] [--dry-run] [--output-dir rendered/]

    # Batch audit all devices in inventory:
    python 035_config_templates.py \
        --template templates/access_switch.j2 \
        --inventory inventory.json \
        --all [--output-dir rendered/]

Prerequisites:
    - Python 3.8+
    - jinja2 >= 3.0    (pip install jinja2)
    - paramiko >= 2.0  (pip install paramiko)
    - Inventory JSON: list of objects, each with at least "hostname" and
      "mgmt_ip" keys; remaining keys are exposed as template variables.
    - SSH key or password auth to target devices.
"""

import argparse
import difflib
import json
import logging
import os
import sys
from pathlib import Path

try:
    import jinja2
except ImportError:
    sys.exit("jinja2 is required: pip install jinja2")

try:
    import paramiko
except ImportError:
    paramiko = None  # optional; only needed for --push or running-config fetch


LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
log = logging.getLogger("config_diff")


def load_inventory(path: str) -> list[dict]:
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("Inventory JSON must be a top-level list of device objects.")
    return data


def render_template(template_path: str, variables: dict) -> str:
    loader = jinja2.FileSystemLoader(str(Path(template_path).parent))
    env = jinja2.Environment(loader=loader, undefined=jinja2.StrictUndefined,
                             trim_blocks=True, lstrip_blocks=True)
    tmpl = env.get_template(Path(template_path).name)
    return tmpl.render(**variables)


def fetch_running_config(host: str, username: str, password: str = None,
                         key_path: str = None, port: int = 22,
                         show_cmd: str = "show running-config") -> str:
    if paramiko is None:
        raise RuntimeError("paramiko is required to fetch running config: pip install paramiko")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(hostname=host, username=username, port=port, timeout=15)
    if key_path:
        connect_kwargs["key_filename"] = key_path
    else:
        connect_kwargs["password"] = password
    client.connect(**connect_kwargs)
    _, stdout, stderr = client.exec_command(show_cmd)
    output = stdout.read().decode()
    err = stderr.read().decode().strip()
    client.close()
    if err:
        log.warning("stderr from %s: %s", host, err)
    return output


def push_config(host: str, username: str, config_lines: list[str],
                password: str = None, key_path: str = None, port: int = 22) -> None:
    if paramiko is None:
        raise RuntimeError("paramiko is required to push config: pip install paramiko")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(hostname=host, username=username, port=port, timeout=15)
    if key_path:
        connect_kwargs["key_filename"] = key_path
    else:
        connect_kwargs["password"] = password
    client.connect(**connect_kwargs)
    shell = client.invoke_shell()
    for line in config_lines:
        shell.send(line + "\n")
    import time; time.sleep(0.5)
    shell.close()
    client.close()
    log.info("Config pushed to %s (%d lines)", host, len(config_lines))


def diff_configs(expected: str, actual: str, device: str) -> list[str]:
    expected_lines = expected.splitlines(keepends=True)
    actual_lines = actual.splitlines(keepends=True)
    return list(difflib.unified_diff(
        actual_lines, expected_lines,
        fromfile=f"{device}/running",
        tofile=f"{device}/expected",
    ))


def audit_device(device: dict, template_path: str, args: argparse.Namespace) -> bool:
    hostname = device.get("hostname", device.get("mgmt_ip", "unknown"))
    mgmt_ip = device.get("mgmt_ip", hostname)
    log.info("Auditing %s (%s)", hostname, mgmt_ip)

    try:
        rendered = render_template(template_path, device)
    except jinja2.UndefinedError as exc:
        log.error("%s: template variable missing — %s", hostname, exc)
        return False

    if args.output_dir:
        out_path = Path(args.output_dir) / f"{hostname}.cfg"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered)
        log.info("Rendered config written to %s", out_path)

    if args.dry_run:
        print(f"\n{'='*60}\nRendered config for {hostname}:\n{'='*60}")
        print(rendered)
        return True

    if args.fetch or args.push:
        try:
            running = fetch_running_config(
                mgmt_ip, args.username,
                password=args.password, key_path=args.key,
                port=args.port, show_cmd=args.show_cmd,
            )
        except Exception as exc:
            log.error("%s: failed to fetch running config — %s", hostname, exc)
            return False

        delta = diff_configs(rendered, running, hostname)
        if delta:
            log.warning("%s: %d line(s) differ from template", hostname, len(delta))
            print("".join(delta))
        else:
            log.info("%s: running config matches template exactly", hostname)

    if args.push and not args.dry_run:
        push_config(mgmt_ip, args.username, rendered.splitlines(),
                    password=args.password, key_path=args.key, port=args.port)

    return True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render and diff ZTP config templates against running device configs."
    )
    p.add_argument("--template", required=True, help="Path to Jinja2 template file")
    p.add_argument("--inventory", required=True, help="Path to JSON inventory file")
    p.add_argument("--device", help="Hostname of single device to process")
    p.add_argument("--all", dest="all_devices", action="store_true",
                   help="Process all devices in inventory")
    p.add_argument("--username", default=os.environ.get("NET_USER", "admin"),
                   help="SSH username (default: $NET_USER or 'admin')")
    p.add_argument("--password", default=os.environ.get("NET_PASS"),
                   help="SSH password (default: $NET_PASS)")
    p.add_argument("--key", default=os.environ.get("NET_KEY"),
                   help="Path to SSH private key (default: $NET_KEY)")
    p.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    p.add_argument("--show-cmd", default="show running-config",
                   help="Command to retrieve running config")
    p.add_argument("--fetch", action="store_true",
                   help="Fetch and diff running config (requires SSH)")
    p.add_argument("--push", action="store_true",
                   help="Push rendered config to device after diff")
    p.add_argument("--dry-run", action="store_true",
                   help="Print rendered config; do not connect to devices")
    p.add_argument("--output-dir", metavar="DIR",
                   help="Write rendered configs to this directory")
    p.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.device and not args.all_devices:
        sys.exit("Specify --device HOSTNAME or --all.")

    inventory = load_inventory(args.inventory)

    if args.device:
        devices = [d for d in inventory if d.get("hostname") == args.device]
        if not devices:
            sys.exit(f"Device '{args.device}' not found in inventory.")
    else:
        devices = inventory

    results = [audit_device(dev, args.template, args) for dev in devices]
    failed = results.count(False)
    log.info("Done: %d/%d devices succeeded.", len(results) - failed, len(results))
    sys.exit(1 if failed else 0)
```