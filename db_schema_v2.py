Here is the script:

```python
#!/usr/bin/env python3
"""
ZTP Stack Database Schema Migration Manager.

Manages incremental schema migrations for the ZTP stack SQLite database,
tracking applied migrations, applying pending ones in order, and supporting
rollback. Complements 008_db_schema.py (initial schema) by handling schema
evolution after first deployment.

Usage:
    python 018_db_schema.py --db ztp.db status
    python 018_db_schema.py --db ztp.db migrate
    python 018_db_schema.py --db ztp.db migrate --dry-run
    python 018_db_schema.py --db ztp.db rollback --steps 1

Prerequisites:
    - Python 3.8+
    - SQLite database created by 008_db_schema.py
    - Write access to the database file
"""

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT    NOT NULL UNIQUE,
    description TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    checksum    TEXT    NOT NULL
);
"""

# Ordered list of migrations; append only — never edit an existing entry.
MIGRATIONS = [
    (
        "0001",
        "Add last_seen index on device_registrations",
        "CREATE INDEX IF NOT EXISTS idx_device_reg_last_seen ON device_registrations(last_seen);",
        None,  # rollback SQL
    ),
    (
        "0002",
        "Add deployment_stage column to deployments",
        "ALTER TABLE deployments ADD COLUMN deployment_stage TEXT DEFAULT 'provision';",
        None,
    ),
    (
        "0003",
        "Add webhook_retries column to webhook_notifications",
        "ALTER TABLE webhook_notifications ADD COLUMN retries INTEGER DEFAULT 0;",
        None,
    ),
    (
        "0004",
        "Add unique constraint index on mac_address in device_registrations",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_reg_mac ON device_registrations(mac_address);",
        "DROP INDEX IF EXISTS idx_device_reg_mac;",
    ),
    (
        "0005",
        "Create config_audit_log table for change tracking",
        (
            "CREATE TABLE IF NOT EXISTS config_audit_log ("
            "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  device_id   TEXT NOT NULL,"
            "  changed_by  TEXT NOT NULL DEFAULT 'system',"
            "  change_type TEXT NOT NULL,"
            "  old_value   TEXT,"
            "  new_value   TEXT,"
            "  changed_at  TEXT NOT NULL"
            ");"
        ),
        "DROP TABLE IF EXISTS config_audit_log;",
    ),
]


class MigrationRow(NamedTuple):
    version: str
    description: str
    applied_at: str
    checksum: str


def _checksum(sql: str) -> str:
    import hashlib
    return hashlib.sha1(sql.encode()).hexdigest()[:12]


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.executescript(MIGRATIONS_TABLE)
    conn.commit()


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return {r["version"] for r in rows}


def run_status(conn: sqlite3.Connection) -> None:
    applied = applied_versions(conn)
    print(f"{'VER':<6}  {'STATUS':<9}  DESCRIPTION")
    print("-" * 60)
    for version, description, up_sql, _ in MIGRATIONS:
        status = "applied" if version in applied else "pending"
        print(f"{version:<6}  {status:<9}  {description}")
    pending = sum(1 for v, *_ in MIGRATIONS if v not in applied)
    print(f"\n{len(applied)} applied, {pending} pending")


def run_migrate(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    ensure_migrations_table(conn)
    applied = applied_versions(conn)
    pending = [(v, d, u, r) for v, d, u, r in MIGRATIONS if v not in applied]

    if not pending:
        log.info("No pending migrations.")
        return 0

    applied_count = 0
    for version, description, up_sql, _ in pending:
        log.info("Migration %s: %s%s", version, description, " [DRY RUN]" if dry_run else "")
        if dry_run:
            print(f"  -- {up_sql[:120]}")
            continue
        try:
            conn.executescript(up_sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, description, applied_at, checksum) VALUES (?, ?, ?, ?)",
                (version, description, datetime.now(timezone.utc).isoformat(), _checksum(up_sql)),
            )
            conn.commit()
            applied_count += 1
            log.info("Migration %s applied.", version)
        except sqlite3.Error as exc:
            conn.rollback()
            log.error("Migration %s failed: %s", version, exc)
            return 1

    if not dry_run:
        log.info("Applied %d migration(s).", applied_count)
    return 0


def run_rollback(conn: sqlite3.Connection, steps: int) -> int:
    applied = conn.execute(
        "SELECT version, description FROM schema_migrations ORDER BY version DESC LIMIT ?", (steps,)
    ).fetchall()

    if not applied:
        log.info("Nothing to roll back.")
        return 0

    migration_map = {v: (d, u, r) for v, d, u, r in MIGRATIONS}
    for row in applied:
        version = row["version"]
        _, _, rollback_sql = migration_map.get(version, (None, None, None))
        if not rollback_sql:
            log.error("Migration %s has no rollback SQL — aborting.", version)
            return 1
        log.info("Rolling back migration %s: %s", version, row["description"])
        try:
            conn.executescript(rollback_sql)
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
            conn.commit()
            log.info("Rolled back %s.", version)
        except sqlite3.Error as exc:
            conn.rollback()
            log.error("Rollback of %s failed: %s", version, exc)
            return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ZTP Stack DB schema migration manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="ztp.db", help="Path to SQLite database (default: ztp.db)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show migration status")

    migrate_p = sub.add_parser("migrate", help="Apply pending migrations")
    migrate_p.add_argument("--dry-run", action="store_true", help="Print SQL without executing")

    rollback_p = sub.add_parser("rollback", help="Reverse the last N migrations")
    rollback_p.add_argument("--steps", type=int, default=1, help="Number of migrations to reverse")

    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    db_path = Path(args.db)
    if args.command != "migrate" and not db_path.exists():
        log.error("Database not found: %s", db_path)
        sys.exit(1)

    conn = connect(str(db_path))
    ensure_migrations_table(conn)

    try:
        if args.command == "status":
            run_status(conn)
            rc = 0
        elif args.command == "migrate":
            rc = run_migrate(conn, dry_run=args.dry_run)
        elif args.command == "rollback":
            rc = run_rollback(conn, steps=args.steps)
        else:
            parser.print_help()
            rc = 1
    finally:
        conn.close()

    sys.exit(rc)
```