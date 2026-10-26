Now I understand the context. NetAutoCommitter is designed to generate network automation scripts for a portfolio, and the user is asking about writing a script for the `ztp-stack` repository (Week 10 of the rollout). The existing 26 scripts cover Docker setup, APIs, deployment tracking, DHCP (KEA), templating, tests, and device registration.

**Clarifying Question #1:**

For a Zero-Touch Provisioning (ZTP) system, which of these would be most valuable to showcase as real-world network engineering skills?

1. **Device Inventory & Discovery** — poll devices for model/version/serial via SNMP or REST, track first-seen/last-seen
2. **Configuration Audit** — validate device configs against compliance rules, report drift
3. **Health Check Monitor** — periodically check device reachability, interface status, CPU/memory via SNMP
4. **Configuration Backup/Restore** — pull running-config from devices, store in Git, restore on demand
5. **Performance Metrics Collector** — gather interface stats (throughput, errors, discards), push to metrics backend

Which resonates most with what you want to highlight?