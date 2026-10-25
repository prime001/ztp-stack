ZTP Config Template Hierarchy Renderer

Renders network device configurations from a layered template system:
base template + device-type overlay + per-device variable substitution.
Supports diff output against a previously rendered config to audit drift.

Usage:
    python 025_config_templates.py \
        --base templates/base.tmpl \
        --overlay templates/cisco_ios.tmpl \
        --vars vars/switch-01.json \
        --output rendered/switch-01.cfg

    python 025_config_templates.py \
        --base templates/base.tmpl \
        --vars vars/switch-01.json \
        --diff rendered/switch-01.cfg.prev

Prerequisites:
    - Template files using $VAR or ${VAR} syntax (stdlib string.Template)
    - Variables file in JSON format with required/optional field declarations
    - Python 3.8+
"""

import argparse
import difflib
import json
import logging
import sys
from pathlib import Path
from string import Template

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

REQUIRED_VARS = {
    "hostname",
    "mgmt_ip",
    "mgmt_prefix",
    "mgmt_gateway",
    "domain_name",
    "ntp_server",
    "snmp_community",
}


def load_template(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        log.error("Cannot read template %s: %s", path, exc)
        sys.exit(1)


def load_vars(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Cannot load variables %s: %s", path, exc)
        sys.exit(1)
    if not isinstance(data, dict):
        log.error("Variables file must be a JSON object")
        sys.exit(1)
    return data


def validate_vars(variables: dict) -> list[str]:
    missing = sorted(REQUIRED_VARS - variables.keys())
    return missing


def merge_templates(base: str, overlay: str | None) -> str:
    if overlay is None:
        return base
    return base.rstrip("\n") + "\n\n" + overlay.lstrip("\n")


def render(template_text: str, variables: dict) -> str:
    try:
        return Template(template_text).substitute(variables)
    except KeyError as exc:
        log.error("Template references undefined variable: %s", exc)
        sys.exit(1)
    except ValueError as exc:
        log.error("Malformed placeholder in template: %s", exc)
        sys.exit(1)


def show_diff(current: str, previous_path: Path) -> None:
    if not previous_path.exists():
        log.warning("Diff target not found: %s — skipping diff", previous_path)
        return
    previous = previous_path.read_text().splitlines(keepends=True)
    current_lines = current.splitlines(keepends=True)
    delta = list(
        difflib.unified_diff(
            previous,
            current_lines,
            fromfile=str(previous_path),
            tofile="rendered",
            lineterm="",
        )
    )
    if not delta:
        log.info("No diff — rendered config matches %s", previous_path)
        return
    log.info("Config diff vs %s:", previous_path)
    for line in delta:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")


def write_output(content: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    log.info("Rendered config written to %s (%d lines)", output_path, content.count("\n"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render ZTP device config from layered templates and a JSON variable file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", required=True, type=Path, help="Base template file")
    parser.add_argument("--overlay", type=Path, default=None, help="Device-type overlay template (optional)")
    parser.add_argument("--vars", required=True, type=Path, help="JSON variable file for this device")
    parser.add_argument("--output", type=Path, default=None, help="Write rendered config to this path")
    parser.add_argument("--diff", type=Path, default=None, metavar="PREV", help="Diff rendered output against this file")
    parser.add_argument("--print", dest="print_output", action="store_true", help="Print rendered config to stdout")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any required vars are missing")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    base_text = load_template(args.base)
    overlay_text = load_template(args.overlay) if args.overlay else None
    variables = load_vars(args.vars)

    missing = validate_vars(variables)
    if missing:
        msg = "Missing required variables: %s", ", ".join(missing)
        if args.strict:
            log.error(*msg)
            sys.exit(2)
        else:
            log.warning(*msg)

    log.debug("Loaded %d variables from %s", len(variables), args.vars)

    merged = merge_templates(base_text, overlay_text)
    rendered = render(merged, variables)

    if args.diff:
        show_diff(rendered, args.diff)

    if args.output:
        write_output(rendered, args.output)

    if args.print_output:
        sys.stdout.write(rendered)

    if not args.output and not args.print_output and not args.diff:
        log.warning("No --output, --print, or --diff specified — nothing to do")
        sys.exit(0)

    log.info("Done")