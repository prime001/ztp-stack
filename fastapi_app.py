The existing scripts are defined in the prompt. Writing `007_device_inventory.py` — a FastAPI device registry that ties MAC addresses to provisioning intent, filling the gap between KEA DHCP and the config template renderer.

```python
"""
ZTP Device Inventory Service  (007_device_inventory.py)

Lightweight FastAPI service that manages the device registry for a Zero Touch
Provisioning pipeline.  Devices are keyed by MAC address; the service exposes
REST endpoints consumed by KEA DHCP hooks, the config-template renderer, and
the deployment tracker.

Purpose
-------
- Persist discovered devices (MAC, hostname, model, target config template).
- Track per-device provisioning state: pending → provisioning → complete | failed.
- Provide a query interface so other ZTP services look up device intent without
  duplicating the source of truth.

Usage
-----
    python 007_device_inventory.py
    python 007_device_inventory.py --host 0.0.0.0 --port 8007
    python 007_device_inventory.py --db /var/lib/ztp/inventory.db

    Register a device:
        curl -X POST http://localhost:8007/devices \
             -H 'Content-Type: application/json' \
             -d '{"mac":"aa:bb:cc:dd:ee:ff","hostname":"leaf-01","model":"N9K-C93180YC-EX","template":"leaf_base"}'

    Advance its state:
        curl -X PATCH http://localhost:8007/devices/aa:bb:cc:dd:ee:ff \
             -H 'Content-Type: application/json' \
             -d '{"state":"complete","assigned_ip":"10.0.1.11"}'

Prerequisites
-------------
    pip install fastapi uvicorn

The SQLite database is created automatically on first run.  No external
services required — the inventory is intentionally self-contained so it can
bootstrap before DHCP or DNS are fully operational.
"""

import argparse
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator

LOG = logging.getLogger("ztp.inventory")

# ---------------------------------------------------------------------------
# Enums / Pydantic models
# ---------------------------------------------------------------------------

class ProvisionState(str, Enum):
    pending = "pending"
    provisioning = "provisioning"
    complete = "complete"
    failed = "failed"


class DeviceIn(BaseModel):
    mac: str
    hostname: Optional[str] = None
    model: Optional[str] = None
    template: Optional[str] = None

    @field_validator("mac")
    @classmethod
    def normalise_mac(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v.replace(":", "").replace("-", "")) != 12:
            raise ValueError("mac must be a 48-bit address")
        return v.replace("-", ":")


class DeviceUpdate(BaseModel):
    hostname: Optional[str] = None
    model: Optional[str] = None
    template: Optional[str] = None
    state: Optional[ProvisionState] = None
    assigned_ip: Optional[str] = None


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

_DB_PATH: str = "inventory.db"


def init_db(path: str) -> None:
    global _DB_PATH
    _DB_PATH = path
    with _conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                mac         TEXT PRIMARY KEY,
                hostname    TEXT,
                model       TEXT,
                template    TEXT,
                state       TEXT NOT NULL DEFAULT 'pending',
                assigned_ip TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        db.commit()
    LOG.debug("Database ready at %s", path)


@contextmanager
def _conn():
    db = sqlite3.connect(_DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="ZTP Device Inventory", version="1.0.0",
              description="Per-device provisioning state and intent registry.")


@app.post("/devices", status_code=201)
def register_device(device: DeviceIn):
    """Register a new device by MAC address (idempotent key)."""
    ts = _now()
    try:
        with _conn() as db:
            db.execute(
                "INSERT INTO devices (mac,hostname,model,template,state,created_at,updated_at)"
                " VALUES (?,?,?,?,'pending',?,?)",
                (device.mac, device.hostname, device.model, device.template, ts, ts),
            )
            db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"Device {device.mac} already registered")
    LOG.info("Registered %s (template=%s)", device.mac, device.template)
    return {"mac": device.mac, "state": "pending", "created_at": ts}


@app.get("/devices")
def list_devices(state: Optional[ProvisionState] = Query(None)):
    """List all devices, optionally filtered by provisioning state."""
    with _conn() as db:
        if state:
            rows = db.execute(
                "SELECT * FROM devices WHERE state=? ORDER BY created_at DESC",
                (state.value,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM devices ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/devices/{mac}")
def get_device(mac: str):
    """Fetch a single device record by MAC address."""
    with _conn() as db:
        row = db.execute("SELECT * FROM devices WHERE mac=?", (mac.lower(),)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Device {mac} not found")
    return dict(row)


@app.patch("/devices/{mac}")
def update_device(mac: str, update: DeviceUpdate):
    """Update device fields; used to advance provisioning state or record assigned IP."""
    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided")
    fields["updated_at"] = _now()
    clause = ", ".join(f"{k}=?" for k in fields)
    with _conn() as db:
        cur = db.execute(
            f"UPDATE devices SET {clause} WHERE mac=?",
            (*fields.values(), mac.lower()),
        )
        db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Device {mac} not found")
    LOG.info("Updated %s: %s", mac, fields)
    return {"mac": mac, **fields}


@app.delete("/devices/{mac}", status_code=204)
def delete_device(mac: str):
    """Remove a device from inventory (decommission or reset)."""
    with _conn() as db:
        cur = db.execute("DELETE FROM devices WHERE mac=?", (mac.lower(),))
        db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Device {mac} not found")
    LOG.info("Deleted %s", mac)


@app.get("/summary")
def summary():
    """Return device counts grouped by provisioning state."""
    with _conn() as db:
        rows = db.execute(
            "SELECT state, COUNT(*) AS n FROM devices GROUP BY state"
        ).fetchall()
    return {r["state"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ZTP Device Inventory — REST API backed by SQLite"
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8007,
                   help="TCP port (default: 8007)")
    p.add_argument("--db", default="inventory.db", metavar="PATH",
                   help="SQLite database path (default: inventory.db)")
    p.add_argument("--log-level", default="info",
                   choices=["debug", "info", "warning", "error"],
                   help="Log verbosity (default: info)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )
    init_db(args.db)
    LOG.info("ZTP inventory service starting on %s:%d  db=%s", args.host, args.port, args.db)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
```