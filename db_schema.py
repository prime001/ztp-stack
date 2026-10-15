```python
"""
008_db_schema.py — ZTP Stack Database Schema Manager

Initializes, inspects, migrates, and resets the SQLite database used by the
ZTP stack. Manages tables for device inventory, provisioning events, DHCP
leases, and config template assignments.

Usage:
    python 008_db_schema.py --init
    python 008_db_schema.py --inspect
    python 008_db_schema.py --migrate
    python 008_db_schema.py --reset --confirm
    python 008_db_schema.py --db /path/to/custom.db --inspect

Prerequisites:
    Python 3.8+, no third-party packages required.
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_DB = Path(__file__).parent / "ztp_stack.db"

SCHEMA_VERSION = 3

MIGRATIONS = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac TEXT UNIQUE NOT NULL,
            hostname TEXT,
            platform TEXT,
            serial TEXT,
            ip TEXT,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','provisioning','complete','failed')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS provisioning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_mac TEXT NOT NULL REFERENCES devices(mac),
            event TEXT NOT NULL,
            detail TEXT,
            occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
    ],
    2: [
        """
        CREATE TABLE IF NOT EXISTS dhcp_leases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac TEXT UNIQUE NOT NULL,
            ip TEXT NOT NULL,
            hostname TEXT,
            lease_start TEXT NOT NULL DEFAULT (datetime('now')),
            lease_end TEXT,
            pool TEXT
        )
        """,
    ],
    3: [
        """
        CREATE TABLE IF NOT EXISTS template_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_mac TEXT NOT NULL REFERENCES devices(mac),
            template_name TEXT NOT NULL,
            rendered_at TEXT,
            deployed_at TEXT,
            checksum TEXT,
            UNIQUE(device_mac, template_name)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_events_mac ON provisioning_events(device_mac)",
        "CREATE INDEX IF NOT EXISTS idx_devices_state ON devices(state)",
    ],
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] or 0
    except sqlite3.OperationalError:
        return 0


def apply_migrations(conn: sqlite3.Connection, target: int = SCHEMA_VERSION) -> int:
    applied = 0
    ver = current_version(conn)
    for v in sorted(k for k in MIGRATIONS if k > ver and k <= target):
        log.info("Applying migration v%d (%d statements)", v, len(MIGRATIONS[v]))
        with conn:
            for stmt in MIGRATIONS[v]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (v,)
            )
        applied += 1
        log.info("Migration v%d applied", v)
    return applied


def cmd_init(conn: sqlite3.Connection) -> None:
    ver = current_version(conn)
    if ver >= SCHEMA_VERSION:
        log.info("Schema already at v%d — nothing to do", ver)
        return
    count = apply_migrations(conn)
    log.info("Initialized: %d migration(s) applied, schema now at v%d", count, SCHEMA_VERSION)


def cmd_migrate(conn: sqlite3.Connection) -> None:
    ver = current_version(conn)
    if ver >= SCHEMA_VERSION:
        log.info("Schema up to date at v%d", ver)
        return
    count = apply_migrations(conn)
    log.info("Migrated: %d migration(s) applied, now at v%d", count, SCHEMA_VERSION)


def cmd_inspect(conn: sqlite3.Connection) -> None:
    ver = current_version(conn)
    print(f"Schema version : {ver}")
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    if not tables:
        print("No tables found.")
        return
    for (tbl,) in tables:
        cols = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        count = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"\n[{tbl}]  ({count} rows)")
        for c in cols:
            pk = " PK" if c["pk"] else ""
            nn = " NOT NULL" if c["notnull"] else ""
            default = f" DEFAULT {c['dflt_value']}" if c["dflt_value"] else ""
            print(f"  {c['name']:30s} {c['type']}{pk}{nn}{default}")


def cmd_reset(conn: sqlite3.Connection) -> None:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    with conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for (tbl,) in tables:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.execute("PRAGMA foreign_keys = ON")
    log.warning("All tables dropped — database is empty")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ZTP Stack database schema manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB, metavar="PATH",
                   help=f"SQLite database path (default: {DEFAULT_DB})")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true",
                       help="Create schema from scratch (idempotent)")
    group.add_argument("--migrate", action="store_true",
                       help="Apply pending migrations")
    group.add_argument("--inspect", action="store_true",
                       help="Print schema version, tables, and row counts")
    group.add_argument("--reset", action="store_true",
                       help="Drop all tables (requires --confirm)")
    p.add_argument("--confirm", action="store_true",
                   help="Required safety flag for --reset")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.reset and not args.confirm:
        log.error("--reset requires --confirm to prevent accidental data loss")
        sys.exit(1)

    try:
        conn = connect(args.db)
        log.info("Connected to %s", args.db)
    except sqlite3.Error as exc:
        log.error("Cannot open database: %s", exc)
        sys.exit(1)

    try:
        if args.init:
            cmd_init(conn)
        elif args.migrate:
            cmd_migrate(conn)
        elif args.inspect:
            cmd_inspect(conn)
        elif args.reset:
            cmd_reset(conn)
    except sqlite3.Error as exc:
        log.error("Database error: %s", exc)
        sys.exit(1)
    finally:
        conn.close()
```