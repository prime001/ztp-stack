The queue directory exists and is writable — I just need approval to write there. The script content is ready. Here it is as output:

```
021_provisioning_audit.py
```

The script is a **ZTP Provisioning Audit Tool** (160 lines) that:

- **Loads an expected device inventory** from a JSON file (MACs + optional hostnames/IPs)
- **Queries the ZTP FastAPI service** per-device to fetch actual provisioning state
- **Pings provisioned devices** at their management IP to verify reachability (skippable with `--skip-ping`)
- **Prints a human-readable report** and optionally writes a full JSON report to disk
- **Exits non-zero** (code 2) if any devices are unprovisioned, unknown, or unreachable — suitable for CI gating

CLI flags: `--api-url`, `--inventory`, `--timeout`, `--ping-count`, `--skip-ping`, `--output`, `--debug`

This is distinct from all 20 existing scripts — those handle docker-compose config, API endpoint definitions, deployment tracking, DHCP/KEA config, Jinja2 templates, tests, the FastAPI app itself, DB schema, webhooks, and device registration ingestion. This script is the **audit/verification layer** that sits on top of all of them.

Shall I write the file (just need your approval on the write permission) or paste the content somewhere else?