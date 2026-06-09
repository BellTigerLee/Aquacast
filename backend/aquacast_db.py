"""SQLite storage for Aquacast backend Kafka-shaped messages.

The backend emits one Kafka payload per sensor. This module stores those payloads
as one wide row per ``tank_id`` and ``event_time_ms`` so measurements produced at
the same backend tick can be queried together.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

import kafka_payload


BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_DOCKER_DB_PATH = Path("/data/aquacast.db")
DEFAULT_LOCAL_DB_PATH = BACKEND_ROOT / "aquacast.db"
WIDE_TABLE = "aquacast_wide"
SENSOR_COLUMNS = tuple(kafka_payload.SENSOR_MEASUREMENT_KEYS.keys())


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def default_db_path() -> Path:
    if DEFAULT_DOCKER_DB_PATH.parent.exists():
        return DEFAULT_DOCKER_DB_PATH
    return DEFAULT_LOCAL_DB_PATH


def db_path_from_env(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    return Path(env.get("AQUACAST_DB_PATH", str(default_db_path()))).expanduser()


def init_db(db_path: Path | str, *, drop: bool = False) -> None:
    """Create the Aquacast SQLite schema.

    ``drop=True`` is intentionally explicit because it destroys existing rows.
    """

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if drop:
            conn.execute(f"DROP TABLE IF EXISTS {WIDE_TABLE}")
            conn.execute("DROP TABLE IF EXISTS water_quality_kafka_wide")
        conn.executescript(_create_schema_sql())
        conn.commit()


class WideMessageStore:
    """Thread-safe SQLite writer for timestamp-wide Kafka payload rows."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        init_db(self.db_path, drop=False)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def insert_kafka_message(
        self,
        message: dict[str, Any],
        *,
        topic: str | None = None,
        partition: int | None = None,
        offset: int | None = None,
        message_key: str | bytes | None = None,
    ) -> None:
        del message_key
        with self._lock:
            insert_kafka_message(
                self._conn,
                message,
                topic=topic,
                partition=partition,
                offset=offset,
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def insert_kafka_message(
    conn: sqlite3.Connection,
    message: dict[str, Any],
    *,
    topic: str | None = None,
    partition: int | None = None,
    offset: int | None = None,
    message_key: str | bytes | None = None,
) -> None:
    """Merge one Aquacast Kafka payload into the wide table.

    Messages sharing the same ``tank_id`` and ``event_time_ms`` update the same
    row. Only the measurement keys present in a message overwrite columns.
    """

    del message_key
    measurements = message.get("measurements") or {}
    if not isinstance(measurements, dict):
        raise ValueError("message.measurements must be an object")

    tank_id = str(message.get("tank_id") or "")
    event_time_ms = message.get("event_time_ms")
    if not tank_id or event_time_ms is None:
        raise ValueError("message.tank_id and message.event_time_ms are required")

    sensor_name = str(message.get("sensor_name") or "").strip()
    existing = conn.execute(
        f"""
        SELECT id, payload_json, seq
        FROM {WIDE_TABLE}
        WHERE tank_id = ? AND event_time_ms = ?
        ORDER BY id
        LIMIT 1
        """,
        (tank_id, event_time_ms),
    ).fetchone()

    if existing is None:
        _insert_new_wide_row(conn, message, measurements, topic, partition, offset, sensor_name)
        return

    row_id, existing_payload_json, existing_seq = existing
    payload_json = _append_payload_json(existing_payload_json, message)
    update_values: dict[str, Any] = {
        "topic": topic,
        "partition": partition,
        "offset": offset,
        "schema_version": message.get("schema_version"),
        "source": message.get("source"),
        "event_time": message.get("event_time"),
        "seq": max(_int_or_zero(existing_seq), _int_or_zero(message.get("seq"))),
        "sim_time_h": message.get("sim_time_h"),
        "payload_json": payload_json,
    }
    if sensor_name in SENSOR_COLUMNS:
        update_values[sensor_name] = 1
    for key in kafka_payload.MEASUREMENT_KEYS:
        if key in measurements:
            update_values[key] = measurements[key]

    assignments = ",".join(f"{key} = ?" for key in update_values)
    conn.execute(
        f"UPDATE {WIDE_TABLE} SET {assignments} WHERE id = ?",
        (*update_values.values(), row_id),
    )


def _insert_new_wide_row(
    conn: sqlite3.Connection,
    message: dict[str, Any],
    measurements: dict[str, Any],
    topic: str | None,
    partition: int | None,
    offset: int | None,
    sensor_name: str,
) -> None:
    measurement_values = {key: measurements.get(key) for key in kafka_payload.MEASUREMENT_KEYS}
    sensor_values = {key: 1 if key == sensor_name else 0 for key in SENSOR_COLUMNS}
    row = {
        "topic": topic,
        "partition": partition,
        "offset": offset,
        "schema_version": message.get("schema_version"),
        "source": message.get("source"),
        "tank_id": message.get("tank_id"),
        **sensor_values,
        "event_time": message.get("event_time"),
        "event_time_ms": message.get("event_time_ms"),
        "seq": message.get("seq"),
        "sim_time_h": message.get("sim_time_h"),
        **measurement_values,
        "payload_json": json.dumps([message], separators=(",", ":"), sort_keys=True),
        "created_at": _utc_now_iso(),
    }
    columns = tuple(row.keys())
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {WIDE_TABLE} ({','.join(columns)}) VALUES ({placeholders})",
        tuple(row[column] for column in columns),
    )


def _append_payload_json(existing: str | None, message: dict[str, Any]) -> str:
    try:
        payloads = json.loads(existing or "[]")
    except json.JSONDecodeError:
        payloads = []
    if isinstance(payloads, dict):
        payloads = [payloads]
    if not isinstance(payloads, list):
        payloads = []
    payloads.append(message)
    return json.dumps(payloads, separators=(",", ":"), sort_keys=True)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _utc_now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _create_schema_sql() -> str:
    sensor_columns = "\n".join(f"    {key} BOOLEAN NOT NULL DEFAULT FALSE," for key in SENSOR_COLUMNS)
    measurement_columns = "\n".join(f"    {key} REAL," for key in kafka_payload.MEASUREMENT_KEYS)
    return f"""
CREATE TABLE IF NOT EXISTS {WIDE_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT,
    partition INTEGER,
    offset INTEGER,
    schema_version INTEGER,
    source TEXT NOT NULL,
    tank_id TEXT NOT NULL,
{sensor_columns}
    event_time TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    sim_time_h REAL,
{measurement_columns}
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(tank_id, event_time_ms)
);

CREATE INDEX IF NOT EXISTS idx_{WIDE_TABLE}_event_time_ms
    ON {WIDE_TABLE}(event_time_ms);

CREATE INDEX IF NOT EXISTS idx_{WIDE_TABLE}_tank_time
    ON {WIDE_TABLE}(tank_id, event_time_ms);
"""

def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize Aquacast SQLite schema")
    parser.add_argument("--db-path", default=str(db_path_from_env()))
    parser.add_argument("--drop", action="store_true", help=f"DROP TABLE IF EXISTS {WIDE_TABLE} before create")
    args = parser.parse_args()

    drop = args.drop or _truthy(os.environ.get("AQUACAST_DB_DROP"))
    init_db(args.db_path, drop=drop)
    action = "dropped and created" if drop else "created if missing"
    print(f"[Aquacast DB] {action}: {Path(args.db_path)} table={WIDE_TABLE}", flush=True)


if __name__ == "__main__":
    main()
