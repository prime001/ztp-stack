The user's instructions say "Output ONLY the script content" — that's an explicit directive that takes priority over the brainstorming flow. Writing directly.

```python
"""
ZTP Stack Pre-Flight Smoke Tests
=================================
Purpose:
    Validates that all ZTP infrastructure components are operational before
    connecting physical devices. Catches misconfigurations early by exercising
    the API, file servers, and registration workflow end-to-end.

Usage:
    python 016_ztp_smoke_tests.py --api-url http://localhost:8000
    python 016_ztp_smoke_tests.py --api-url http://192.168.1.10:8000 \\
        --tftp-host 192.168.1.10 --timeout 10 --verbose
    python 016_ztp_smoke_tests.py --api-url http://localhost:8000 --skip-tftp

Prerequisites:
    - ZTP stack running (docker-compose up or native)
    - Network access to the ZTP API and file servers
    - Python 3.8+; no third-party libraries required
"""

import argparse
import json
import logging
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin

LOG = logging.getLogger("ztp-smoke")


@dataclass
class TestResult:
    name: str
    passed: bool
    message: str
    detail: Optional[str] = None


@dataclass
class SuiteResult:
    results: List[TestResult] = field(default_factory=list)

    def add(self, result: TestResult) -> None:
        level = logging.DEBUG if result.passed else logging.WARNING
        LOG.log(level, "[%s] %s — %s", "PASS" if result.passed else "FAIL",
                result.name, result.message)
        if result.detail:
            LOG.debug("  %s", result.detail)
        self.results.append(result)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed


def check_api_health(base_url: str, timeout: int) -> TestResult:
    url = urljoin(base_url.rstrip("/") + "/", "health")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            status = body.get("status", "")
            ok = resp.status == 200 and status in ("ok", "healthy", "up")
            return TestResult(
                "API health endpoint",
                ok,
                f"HTTP {resp.status} status={status!r}",
                str(body),
            )
    except urllib.error.HTTPError as exc:
        return TestResult("API health endpoint", False, f"HTTP {exc.code}: {exc.reason}")
    except Exception as exc:
        return TestResult("API health endpoint", False, str(exc))


def check_tcp_port(host: str, port: int, timeout: int, label: str) -> TestResult:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return TestResult(label, True, f"{host}:{port} reachable")
    except OSError as exc:
        return TestResult(label, False, f"{host}:{port} unreachable — {exc}")


def check_http_file_server(base_url: str, timeout: int) -> TestResult:
    url = urljoin(base_url.rstrip("/") + "/", "files/")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ok = resp.status == 200
            return TestResult(
                "HTTP file server",
                ok,
                f"GET {url} → HTTP {resp.status}",
            )
    except urllib.error.HTTPError as exc:
        ok = exc.code in (200, 403)
        return TestResult(
            "HTTP file server",
            ok,
            f"GET {url} → HTTP {exc.code} (accessible)" if ok else str(exc),
        )
    except Exception as exc:
        return TestResult("HTTP file server", False, str(exc))


def check_device_registration(base_url: str, timeout: int) -> TestResult:
    url = urljoin(base_url.rstrip("/") + "/", "api/v1/devices")
    payload = json.dumps({
        "mac_address": "de:ad:be:ef:00:01",
        "hostname": "smoke-test-device",
        "model": "smoke-test",
        "_smoke_test": True,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "X-Smoke-Test": "1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            ok = resp.status in (200, 201)
            device_id = body.get("id") or body.get("device_id", "—")
            return TestResult(
                "Device registration",
                ok,
                f"HTTP {resp.status} device_id={device_id}",
                str(body),
            )
    except urllib.error.HTTPError as exc:
        ok = exc.code == 409
        reason = "device already exists (idempotent)" if ok else exc.reason
        return TestResult("Device registration", ok, f"HTTP {exc.code}: {reason}")
    except Exception as exc:
        return TestResult("Device registration", False, str(exc))


def check_config_retrieval(base_url: str, timeout: int) -> TestResult:
    url = urljoin(base_url.rstrip("/") + "/", "api/v1/config/templates")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            templates = body if isinstance(body, list) else body.get("templates", [])
            ok = resp.status == 200
            return TestResult(
                "Config template retrieval",
                ok,
                f"HTTP {resp.status}, {len(templates)} template(s) found",
            )
    except urllib.error.HTTPError as exc:
        return TestResult("Config template retrieval", False, f"HTTP {exc.code}: {exc.reason}")
    except Exception as exc:
        return TestResult("Config template retrieval", False, str(exc))


def print_summary(suite: SuiteResult) -> None:
    width = 60
    print("\n" + "=" * width)
    print(f"{'ZTP SMOKE TEST RESULTS':^{width}}")
    print("=" * width)
    for r in suite.results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}")
        print(f"         {r.message}")
    print("-" * width)
    print(f"  Passed: {suite.passed}  Failed: {suite.failed}  "
          f"Total: {len(suite.results)}")
    print("=" * width + "\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ZTP stack pre-flight smoke test suite",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--api-url", required=True,
                   help="Base URL of the ZTP API (e.g. http://localhost:8000)")
    p.add_argument("--tftp-host", default=None,
                   help="TFTP server host; defaults to API host")
    p.add_argument("--tftp-port", type=int, default=69,
                   help="TFTP server port")
    p.add_argument("--file-server-url", default=None,
                   help="HTTP file server base URL; defaults to --api-url")
    p.add_argument("--timeout", type=int, default=5,
                   help="Socket/HTTP timeout in seconds")
    p.add_argument("--skip-tftp", action="store_true",
                   help="Skip TFTP port check (useful if TFTP is not exposed)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Show debug output including response bodies")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )

    from urllib.parse import urlparse
    parsed = urlparse(args.api_url)
    api_host = parsed.hostname or "localhost"

    suite = SuiteResult()
    suite.add(check_api_health(args.api_url, args.timeout))

    if not args.skip_tftp:
        tftp_host = args.tftp_host or api_host
        suite.add(check_tcp_port(tftp_host, args.tftp_port, args.timeout, "TFTP server"))

    file_url = args.file_server_url or args.api_url
    suite.add(check_http_file_server(file_url, args.timeout))
    suite.add(check_device_registration(args.api_url, args.timeout))
    suite.add(check_config_retrieval(args.api_url, args.timeout))

    print_summary(suite)
    sys.exit(0 if suite.failed == 0 else 1)
```