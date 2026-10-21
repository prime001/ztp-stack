```python
#!/usr/bin/env python3
"""
Network Device Webhook Notification Handler

Receives webhook events from network devices in a Zero Touch Provisioning stack,
validates payload signatures, logs events, and forwards critical notifications
to configured endpoints (Slack, etc.).

Prerequisites:
  - HTTP server with public or internal endpoint for webhooks
  - Webhook secret configured on devices for HMAC-SHA256 signature validation
  - Slack incoming webhook URL (optional, for notifications)

Usage:
  python3 webhook_handler.py --port 8080 --webhook-secret mysecret
  python3 webhook_handler.py --listen-addr 127.0.0.1 --slack-webhook https://hooks.slack.com/...
  python3 webhook_handler.py --log-level DEBUG

Environment Variables:
  WEBHOOK_SECRET: Shared secret for signature validation
  NOTIFY_SLACK_WEBHOOK: Slack incoming webhook URL for critical alerts
"""

import argparse
import json
import logging
import hmac
import hashlib
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any
import urllib.request
import urllib.error


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for network device webhook events."""

    WEBHOOK_SECRET = None
    SLACK_WEBHOOK = None

    def do_POST(self) -> None:
        """Handle POST requests to /webhook/device-event."""
        if self.path != "/webhook/device-event":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            logging.error(f"JSON decode error: {e}")
            self.send_error(400, "Invalid JSON")
            return

        # Validate HMAC-SHA256 signature if secret configured
        if self.WEBHOOK_SECRET:
            signature = self.headers.get("X-Webhook-Signature", "")
            expected = hmac.new(
                self.WEBHOOK_SECRET.encode(),
                body,
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(signature, expected):
                logging.warning(
                    f"Signature validation failed for device {payload.get('device_id')}"
                )
                self.send_error(401, "Signature mismatch")
                return

        try:
            self._process_webhook(payload)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        except Exception as e:
            logging.error(f"Webhook processing error: {e}", exc_info=True)
            self.send_error(500, "Processing failed")

    def _process_webhook(self, payload: Dict[str, Any]) -> None:
        """Process and log device webhook event."""
        device_id = payload.get("device_id", "unknown")
        event_type = payload.get("event_type", "unknown")
        status = payload.get("status", "unknown")
        details = payload.get("details", "")

        logging.info(f"Device {device_id}: {event_type} -> {status}")

        # Determine severity based on status
        if status in ("failed", "error"):
            severity = "critical"
        elif event_type == "provisioning_failed":
            severity = "critical"
        elif event_type == "config_mismatch":
            severity = "warning"
        else:
            severity = "info"

        # Send Slack notification for critical/warning events
        if self.SLACK_WEBHOOK and severity in ("critical", "warning"):
            self._send_slack_notification(
                device_id, event_type, status, severity, details
            )

    def _send_slack_notification(
        self,
        device_id: str,
        event_type: str,
        status: str,
        severity: str,
        details: str,
    ) -> None:
        """Send notification to Slack webhook."""
        colors = {
            "critical": "#FF0000",
            "warning": "#FFAA00",
            "info": "#0099FF",
        }

        text = f"Device: {device_id}\nEvent: {event_type}\nStatus: {status}"
        if details:
            text += f"\nDetails: {details}"

        payload = {
            "attachments": [
                {
                    "color": colors.get(severity, "#0099FF"),
                    "title": f"Network Device Alert [{severity.upper()}]",
                    "text": text,
                    "footer": "ZTP Stack Webhook Handler",
                }
            ]
        }

        try:
            req = urllib.request.Request(
                self.SLACK_WEBHOOK,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status != 200:
                    logging.warning(
                        f"Slack API returned status {response.status}"
                    )
        except urllib.error.URLError as e:
            logging.error(f"Slack notification failed: {e}")

    def log_message(self, format_str: str, *args) -> None:
        """Suppress default HTTP server logging."""
        pass


def main() -> None:
    """Start the webhook server."""
    parser = argparse.ArgumentParser(
        description="ZTP Network Device Webhook Notification Handler"
    )
    parser.add_argument(
        "--listen-addr",
        default="0.0.0.0",
        help="Listen address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Listen port (default: 8080)",
    )
    parser.add_argument(
        "--webhook-secret",
        help="Secret for HMAC-SHA256 signature validation",
    )
    parser.add_argument(
        "--slack-webhook",
        help="Slack incoming webhook URL for critical alerts",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    WebhookHandler.WEBHOOK_SECRET = args.webhook_secret
    WebhookHandler.SLACK_WEBHOOK = args.slack_webhook

    server_address = (args.listen_addr, args.port)
    httpd = HTTPServer(server_address, WebhookHandler)

    logging.info(f"Webhook server starting on {args.listen_addr}:{args.port}")
    logging.info("POST /webhook/device-event to send device events")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down webhook server")
        httpd.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()
```