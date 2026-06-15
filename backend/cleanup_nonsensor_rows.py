#!/usr/bin/env python3
"""Remove stale Aquacast rows for synthetic or sensorless tanks."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3


BACKEND_ROOT = Path(__file__).resolve().parent
TABLES = ("aquacast_wide", "aquacast_threshold_alerts")
SENSORLESS_IDS = tuple(f"Fishtank_{index}" for index in range(7, 13))
BAD_TANK_IDS = ("tank-01", *SENSORLESS_IDS)


def default_db_path() -> Path:
    if os.environ.get("AQUACAST_DB_PATH"):
        return Path(os.environ["AQUACAST_DB_PATH"]).expanduser()
    candidates = [
        Path("/data/aquacast.db"),
        BACKEND_ROOT / "data" / "sqlite" / "aquacast.db",
        BACKEND_ROOT / "aquacast.db",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _where_clause(columns: set[str]) -> tuple[str, list[str]]:
    clauses = []
    params: list[str] = []
    for column in ("tank_id", "tank_path"):
        if column not in columns:
            continue
        placeholders = ",".join("?" for _ in BAD_TANK_IDS)
        clauses.append(f"{column} IN ({placeholders})")
        params.extend(BAD_TANK_IDS)
        for tank_id in SENSORLESS_IDS:
            clauses.append(f"{column} LIKE ?")
            params.append(f"%{tank_id}%")
    return " OR ".join(clauses), params


def cleanup(db_path: Path, *, dry_run: bool) -> None:
    if not db_path.exists():
        raise SystemExit(f"database does not exist: {db_path}")
    with sqlite3.connect(db_path) as conn:
        for table in TABLES:
            if not _table_exists(conn, table):
                print(f"{table}: skipped (table missing)")
                continue
            where_sql, params = _where_clause(_columns(conn, table))
            if not where_sql:
                print(f"{table}: skipped (tank_id/tank_path columns missing)")
                continue
            count = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where_sql}", params).fetchone()[0])
            if dry_run:
                print(f"{table}: would delete {count} rows")
                continue
            conn.execute(f"DELETE FROM {table} WHERE {where_sql}", params)
            print(f"{table}: deleted {count} rows")
        if dry_run:
            conn.rollback()
        else:
            conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="SQLite DB path")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without deleting")
    args = parser.parse_args()
    cleanup((args.db or default_db_path()).expanduser(), dry_run=bool(args.dry_run))


if __name__ == "__main__":
    main()
