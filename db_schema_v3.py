ZTP Stack - Database Schema Migration Manager (028)

Purpose:
    Manages incremental schema migrations for the ZTP device database.
    Tracks applied migrations, validates schema integrity, and supports
    rollback to a prior version. Complements the initial schema creation
    scripts (008, 018) by providing an audit-trailed upgrade path for
    production ZTP deployments.

Usage:
    python 028_db_schema.py --db ztp.db --action status
    python 028_db_schema.py --db ztp.db --action migrate
    python 028_db_schema.py --db ztp.db --action validate
    python 028_db_schema.py --db ztp.db --action rollback --target 3

Prerequisites:
    - Python 3.8+
    - An existing ZTP SQLite database (or an empty file for fresh init)
    - Write permissions on the database file
"""

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# Each migration is (version, description, up_sql, down_sql).
# up_sql and down_sql may each be a list of statements.
MIGRATIONS = [
    (
        1,
        "Create schema_version tracking table",
        [
            """CREATE TABLE IF NOT EXISTS schema_version (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at  TEXT NOT NULL
            )"""
        ],
        ["DROP TABLE IF EXISTS schema_version"],
    ),
    (
        2,
        "Add provisioning_stage column to devices",
        [
            "ALTER TABLE devices ADD COLUMN provisioning_stage TEXT DEFAULT 'discovered'",
        ],
        # SQLite cannot drop columns before 3.35; use a no-op as the rollback sentinel
        [
            "SELECT 1"
        ],
    ),
    (
        3,
        "Create device_events audit table",
        [
            """CREATE TABLE IF NOT EXISTS device_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                detail      TEXT,
                occurred_at TEXT NOT NULL DEFAULT (datetime('now','utc'))
            )""",
            "CREATE INDEX IF NOT EXISTS idx_device_events_device ON device_events(device_id)",
        ],
        [
            "DROP INDEX IF EXISTS idx_device_events_device",
            "DROP TABLE IF EXISTS device_events",
        ],
    ),
    (
        4,
        "Add retry_count and last_error columns to devices",
        [
            "ALTER TABLE devices ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE devices ADD COLUMN last_error TEXT",
        ],
        ["SELECT 1"],
    ),
]


def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_version"
        ).fetchone()
        return row["v"]
    except sqlite3.OperationalError:
        return 0


def apply_statements(conn: sqlite3.Connection, stmts: list) -> None:
    for stmt in stmts:
        conn.execute(stmt)


def cmd_status(conn: sqlite3.Connection) -> None:
    ver = current_version(conn)
    log.info("Current schema version: %d", ver)
    pending = [m for m in MIGRATIONS if m[0] > ver]
    if pending:
        log.info("%d pending migration(s):", len(pending))
        for m in pending:
            log.info("  v%d — %s", m[0], m[1])
    else:
        log.info("Schema is up to date.")


def cmd_migrate(conn: sqlite3.Connection) -> None:
    ver = current_version(conn)
    pending = [m for m in MIGRATIONS if m[0] > ver]
    if not pending:
        log.info("Nothing to migrate — already at version %d.", ver)
        return
    for version, desc, up_sql, _ in pending:
        log.info("Applying v%d: %s", version, desc)
        try:
            with conn:
                apply_statements(conn, up_sql)
                conn.execute(
                    "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                    (version, desc, datetime.now(timezone.utc).isoformat()),
                )
        except sqlite3.OperationalError as exc:
            log.error("Migration v%d failed: %s", version, exc)
            sys.exit(1)
        log.info("  v%d applied.", version)
    log.info("Schema now at version %d.", current_version(conn))


def cmd_validate(conn: sqlite3.Connection) -> None:
    log.info("Running schema integrity check...")
    result = conn.execute("PRAGMA integrity_check").fetchall()
    issues = [r[0] for r in result if r[0] != "ok"]
    if issues:
        log.error("Integrity problems found:")
        for issue in issues:
            log.error("  %s", issue)
        sys.exit(1)
    log.info("Integrity check passed.")

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {"schema_version", "device_events"}
    missing = required - tables
    if missing:
        log.warning("Expected tables not yet created: %s", ", ".join(sorted(missing)))
    else:
        log.info("All expected tables present: %s", ", ".join(sorted(required)))


def cmd_rollback(conn: sqlite3.Connection, target: int) -> None:
    ver = current_version(conn)
    if target >= ver:
        log.error("Target version %d >= current %d — nothing to roll back.", target, ver)
        sys.exit(1)
    to_rollback = sorted(
        [m for m in MIGRATIONS if m[0] > target and m[0] <= ver],
        key=lambda m: m[0],
        reverse=True,
    )
    for version, desc, _, down_sql in to_rollback:
        log.info("Rolling back v%d: %s", version, desc)
        try:
            with conn:
                apply_statements(conn, down_sql)
                conn.execute(
                    "DELETE FROM schema_version WHERE version = ?", (version,)
                )
        except sqlite3.OperationalError as exc:
            log.error("Rollback of v%d failed: %s", version, exc)
            sys.exit(1)
        log.info("  v%d rolled back.", version)
    log.info("Schema now at version %d.", current_version(conn))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ZTP stack database schema migration manager"
    )
    parser.add_argument(
        "--db",
        required=True,
        metavar="PATH",
        help="Path to the SQLite database file",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["status", "migrate", "validate", "rollback"],
        help="Action to perform",
    )
    parser.add_argument(
        "--target",
        type=int,
        metavar="VERSION",
        help="Target schema version for rollback",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable DEBUG logging"
    )
    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("Database not found: %s", db_path)
        sys.exit(1)

    conn = open_db(str(db_path))

    if args.action == "status":
        cmd_status(conn)
    elif args.action == "migrate":
        cmd_migrate(conn)
    elif args.action == "validate":
        cmd_validate(conn)
    elif args.action == "rollback":
        if args.target is None:
            parser.error("--target VERSION is required for rollback")
        cmd_rollback(conn, args.target)

    conn.close()