ZTP Stack API Endpoint Health Monitor

Purpose:
    Validates health and schema correctness of ZTP stack API endpoints.
    Useful for post-deployment verification, CI gate checks, and scheduled
    monitoring to catch regressions before they affect provisioning workflows.

Usage:
    python 012_api_health_monitor.py --base-url http://localhost:8000
    python 012_api_health_monitor.py --base-url http://ztp-api:8000 --timeout 10 --format json
    python 012_api_health_monitor.py --base-url http://ztp-api:8000 --endpoint /devices --verbose

Prerequisites:
    pip install requests
    A running ZTP FastAPI stack (see 007_fastapi_app.py)
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import ConnectionError, Timeout

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


ENDPOINT_SPECS: List[Dict[str, Any]] = [
    {
        "method": "GET",
        "path": "/health",
        "expected_status": 200,
        "required_keys": ["status"],
        "expect_list": False,
    },
    {
        "method": "GET",
        "path": "/devices",
        "expected_status": 200,
        "required_keys": [],
        "expect_list": True,
    },
    {
        "method": "GET",
        "path": "/deployments",
        "expected_status": 200,
        "required_keys": [],
        "expect_list": True,
    },
    {
        "method": "GET",
        "path": "/config-templates",
        "expected_status": 200,
        "required_keys": [],
        "expect_list": True,
    },
    {
        "method": "GET",
        "path": "/webhooks",
        "expected_status": 200,
        "required_keys": [],
        "expect_list": True,
    },
]


@dataclass
class EndpointResult:
    endpoint: str
    method: str
    status_code: Optional[int]
    latency_ms: float
    passed: bool
    error: str = ""
    schema_errors: List[str] = field(default_factory=list)


def check_endpoint(
    session: requests.Session,
    base_url: str,
    spec: Dict[str, Any],
    timeout: int,
) -> EndpointResult:
    url = f"{base_url.rstrip('/')}{spec['path']}"
    start = time.monotonic()
    status_code: Optional[int] = None
    schema_errors: List[str] = []

    try:
        resp = session.request(spec["method"], url, timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        status_code = resp.status_code

        if status_code != spec["expected_status"]:
            return EndpointResult(
                endpoint=spec["path"],
                method=spec["method"],
                status_code=status_code,
                latency_ms=latency_ms,
                passed=False,
                error=f"expected HTTP {spec['expected_status']}, got {status_code}",
            )

        try:
            body = resp.json()
        except ValueError:
            return EndpointResult(
                endpoint=spec["path"],
                method=spec["method"],
                status_code=status_code,
                latency_ms=latency_ms,
                passed=False,
                error="response body is not valid JSON",
            )

        if spec.get("expect_list") and not isinstance(body, list):
            schema_errors.append(f"expected list, got {type(body).__name__}")

        for key in spec.get("required_keys", []):
            if isinstance(body, dict) and key not in body:
                schema_errors.append(f"missing required key '{key}'")

        return EndpointResult(
            endpoint=spec["path"],
            method=spec["method"],
            status_code=status_code,
            latency_ms=latency_ms,
            passed=not schema_errors,
            schema_errors=schema_errors,
        )

    except (ConnectionError, Timeout) as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return EndpointResult(
            endpoint=spec["path"],
            method=spec["method"],
            status_code=None,
            latency_ms=latency_ms,
            passed=False,
            error=str(exc),
        )
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return EndpointResult(
            endpoint=spec["path"],
            method=spec["method"],
            status_code=status_code,
            latency_ms=latency_ms,
            passed=False,
            error=f"unexpected: {exc}",
        )


def run_checks(
    base_url: str,
    timeout: int,
    endpoint_filter: Optional[str],
    verbose: bool,
) -> List[EndpointResult]:
    specs = ENDPOINT_SPECS
    if endpoint_filter:
        specs = [s for s in specs if s["path"] == endpoint_filter]
        if not specs:
            log.error("No spec registered for path: %s", endpoint_filter)
            return []

    results: List[EndpointResult] = []
    with requests.Session() as session:
        session.headers["Accept"] = "application/json"
        for spec in specs:
            result = check_endpoint(session, base_url, spec, timeout)
            results.append(result)
            if verbose:
                label = "PASS" if result.passed else "FAIL"
                notes = result.error or "; ".join(result.schema_errors)
                log.info(
                    "[%s] %s %s  %s  %dms  %s",
                    label,
                    result.method,
                    result.endpoint,
                    result.status_code or "N/A",
                    int(result.latency_ms),
                    notes,
                )

    return results


def render_table(results: List[EndpointResult]) -> None:
    header = f"{'STATUS':<6}  {'METHOD':<6}  {'ENDPOINT':<25}  {'HTTP':<4}  {'MS':>6}  NOTES"
    print(header)
    print("-" * len(header))
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        http_col = str(r.status_code) if r.status_code is not None else "N/A"
        notes = r.error or "; ".join(r.schema_errors)
        print(
            f"{status:<6}  {r.method:<6}  {r.endpoint:<25}  "
            f"{http_col:<4}  {int(r.latency_ms):>6}  {notes}"
        )


def render_json(results: List[EndpointResult]) -> None:
    payload = [
        {
            "endpoint": r.endpoint,
            "method": r.method,
            "status_code": r.status_code,
            "latency_ms": round(r.latency_ms, 1),
            "passed": r.passed,
            "error": r.error,
            "schema_errors": r.schema_errors,
        }
        for r in results
    ]
    print(json.dumps(payload, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Health-check ZTP stack API endpoints and validate response schemas",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the ZTP FastAPI service (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        metavar="SECONDS",
        help="Per-request timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--endpoint",
        metavar="PATH",
        help="Check only this endpoint path, e.g. /devices",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Log each check as it runs",
    )
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()

    results = run_checks(
        base_url=args.base_url,
        timeout=args.timeout,
        endpoint_filter=args.endpoint,
        verbose=args.verbose,
    )

    if not results:
        sys.exit(2)

    if args.format == "json":
        render_json(results)
    else:
        render_table(results)

    failed = sum(1 for r in results if not r.passed)
    total = len(results)
    print(f"\n{total - failed}/{total} checks passed")
    sys.exit(0 if failed == 0 else 1)