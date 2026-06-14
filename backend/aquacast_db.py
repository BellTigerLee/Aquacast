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
import time
from typing import Any

import kafka_payload


BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_DOCKER_DB_PATH = Path("/data/aquacast.db")
DEFAULT_LOCAL_DB_PATH = BACKEND_ROOT / "aquacast.db"
WIDE_TABLE = "aquacast_wide"
THRESHOLD_ALERT_TABLE = "aquacast_threshold_alerts"
SENSOR_COLUMNS = tuple(kafka_payload.SENSOR_MEASUREMENT_KEYS.keys())
DASHBOARD_COLUMN_ALIASES = (
    ("temperature", "temperature_c"),
    ("DO", "dissolved_oxygen_mg_l"),
    ("CO2", "co2_mg_l"),
    ("pH", "ph"),
    ("TAN", "tan_mg_l"),
    ("ammonia", "nh3_mg_l"),
    ("nitrite", "nitrite_mg_l"),
    ("nitrate", "nitrate_mg_l"),
    ("salinity", "salinity_ppt"),
    ("turbidity", "turbidity_ntu"),
)


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
            conn.execute(f"DROP TABLE IF EXISTS {THRESHOLD_ALERT_TABLE}")
            conn.execute("DROP TABLE IF EXISTS water_quality_kafka_wide")
        conn.executescript(_create_schema_sql())
        _ensure_schema_columns(conn)
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

    def insert_threshold_alert(
        self,
        message: dict[str, Any],
        *,
        topic: str | None = None,
        partition: int | None = None,
        offset: int | None = None,
        message_key: str | bytes | None = None,
    ) -> None:
        with self._lock:
            insert_threshold_alert(
                self._conn,
                message,
                topic=topic,
                partition=partition,
                offset=offset,
                message_key=message_key,
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


def insert_threshold_alert(
    conn: sqlite3.Connection,
    message: dict[str, Any],
    *,
    topic: str | None = None,
    partition: int | None = None,
    offset: int | None = None,
    message_key: str | bytes | None = None,
) -> None:
    """Store one threshold alert payload for later incident/LLM analysis."""

    del message_key
    alert_id = str(message.get("alert_id") or "")
    tank_id = str(message.get("tank_id") or "")
    event_time_ms = message.get("event_time_ms")
    if not alert_id or not tank_id or event_time_ms is None:
        raise ValueError("threshold alert requires alert_id, tank_id, and event_time_ms")

    row = {
        "topic": topic,
        "partition": partition,
        "offset": offset,
        "schema_version": message.get("schema_version"),
        "source": message.get("source"),
        "alert_id": alert_id,
        "message_type": message.get("message_type"),
        "event_type": message.get("event_type"),
        "severity": message.get("severity"),
        "tank_id": tank_id,
        "tank_name": message.get("tank_name"),
        "tank_path": message.get("tank_path"),
        "event_time": message.get("event_time"),
        "event_time_ms": event_time_ms,
        "seq": message.get("seq"),
        "sim_time_h": message.get("sim_time_h"),
        "violated_parameter_names_json": _json_text(message.get("violated_parameter_names") or []),
        "violations_json": _json_text(message.get("violations") or []),
        "thresholds_json": _json_text(message.get("thresholds") or {}),
        "measurements_json": _json_text(message.get("measurements") or {}),
        "stock_json": _json_text(message.get("stock") or {}),
        "loads_json": _json_text(message.get("loads") or {}),
        "payload_json": _json_text(message),
        "created_at": _utc_now_iso(),
    }
    columns = tuple(row.keys())
    placeholders = ",".join("?" for _ in columns)
    assignments = ",".join(f"{column} = excluded.{column}" for column in columns if column != "alert_id")
    conn.execute(
        f"""
        INSERT INTO {THRESHOLD_ALERT_TABLE} ({','.join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(alert_id) DO UPDATE SET {assignments}
        """,
        tuple(row[column] for column in columns),
    )


def query_recent_wide(
    db_path: Path | str,
    *,
    hours: float = 4.0,
    limit: int = 7200,
    tank_id: str | None = None,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Return recent wide water-quality rows from SQLite."""

    cutoff_ms = _cutoff_ms(hours, now_ms=now_ms)
    limit = _safe_limit(limit, default=7200, maximum=100000)
    path = Path(db_path)
    if not path.exists():
        return []

    where = ["event_time_ms >= ?"]
    params: list[Any] = [cutoff_ms]
    if tank_id:
        where.append("tank_id = ?")
        params.append(str(tank_id))
    params.append(limit)

    conn = _read_connection(path)
    try:
        rows = conn.execute(
            f"""
            SELECT *
            FROM {WIDE_TABLE}
            WHERE {' AND '.join(where)}
            ORDER BY event_time_ms ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def query_recent_threshold_alerts(
    db_path: Path | str,
    *,
    hours: float = 4.0,
    limit: int = 200,
    tank_id: str | None = None,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Return recent decoded threshold alert rows from SQLite."""

    cutoff_ms = _cutoff_ms(hours, now_ms=now_ms)
    limit = _safe_limit(limit, default=200, maximum=10000)
    path = Path(db_path)
    if not path.exists():
        return []

    where = ["event_time_ms >= ?"]
    params: list[Any] = [cutoff_ms]
    if tank_id:
        where.append("tank_id = ?")
        params.append(str(tank_id))
    params.append(limit)

    conn = _read_connection(path)
    try:
        rows = conn.execute(
            f"""
            SELECT *
            FROM {THRESHOLD_ALERT_TABLE}
            WHERE {' AND '.join(where)}
            ORDER BY event_time_ms ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return [_decode_alert_row(dict(row)) for row in rows]


def dashboard_rows_from_wide(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map Aquacast canonical metric names to dashboard/LLM-friendly names."""

    mapped = []
    for row in rows:
        item: dict[str, Any] = {
            "timestamp": row.get("event_time"),
            "event_time_ms": row.get("event_time_ms"),
            "tank_id": row.get("tank_id"),
        }
        for alias, source in DASHBOARD_COLUMN_ALIASES:
            item[alias] = row.get(source)
        mapped.append(item)
    return mapped


def summarize_dashboard_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for alias, _source in DASHBOARD_COLUMN_ALIASES:
        values = [_float_or_none(row.get(alias)) for row in rows]
        values = [value for value in values if value is not None]
        if not values:
            continue
        first = values[0]
        latest = values[-1]
        summary[alias] = {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
            "first": first,
            "latest": latest,
            "delta": latest - first,
        }
    return summary


def build_llm_context_payload(
    db_path: Path | str,
    *,
    hours: float = 4.0,
    limit: int = 7200,
    alert_limit: int = 200,
    tank_id: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build a compact recent SQLite context payload for LLM prompts."""

    wide_rows = query_recent_wide(db_path, hours=hours, limit=limit, tank_id=tank_id, now_ms=now_ms)
    rows = dashboard_rows_from_wide(wide_rows)
    alerts = query_recent_threshold_alerts(db_path, hours=hours, limit=alert_limit, tank_id=tank_id, now_ms=now_ms)
    summary = summarize_dashboard_rows(rows)
    latest = rows[-1] if rows else None
    sample = rows[-min(120, len(rows)) :]
    payload = {
        "status": "ok",
        "db_path": str(Path(db_path)),
        "hours": float(hours),
        "tank_id": tank_id,
        "columns": ["timestamp"] + [alias for alias, _source in DASHBOARD_COLUMN_ALIASES],
        "row_count": len(rows),
        "latest": latest,
        "summary": summary,
        "recent_sample": sample,
        "threshold_alert_count": len(alerts),
        "threshold_alerts": alerts[-min(alert_limit, len(alerts)) :],
    }
    payload["context_text"] = render_llm_context_text(payload)
    return payload


def render_llm_context_text(payload: dict[str, Any]) -> str:
    lines = [
        "[Aquacast SQLite water-quality context]",
        f"Window: last {payload.get('hours')} hours",
        f"Rows: {payload.get('row_count', 0)}",
    ]
    latest = payload.get("latest")
    if latest:
        lines.append(f"Latest snapshot: {_json_text(latest)}")

    summary = payload.get("summary") or {}
    if summary:
        lines.append("Metric trends:")
        for alias, values in summary.items():
            lines.append(
                "- "
                f"{alias}: latest={_round_float(values.get('latest'))}, "
                f"avg={_round_float(values.get('avg'))}, "
                f"min={_round_float(values.get('min'))}, "
                f"max={_round_float(values.get('max'))}, "
                f"delta={_round_float(values.get('delta'))}"
            )

    alerts = payload.get("threshold_alerts") or []
    if alerts:
        lines.append("Recent threshold/anomaly alerts:")
        for alert in alerts[-20:]:
            names = alert.get("violated_parameter_names") or []
            lines.append(
                "- "
                f"{alert.get('event_time')} tank={alert.get('tank_id')} "
                f"severity={alert.get('severity')} violated={names}"
            )
    else:
        lines.append("Recent threshold/anomaly alerts: none recorded in this window")

    sample = payload.get("recent_sample") or []
    if sample:
        lines.append(f"Recent sample rows: {_json_text(sample[-20:])}")
    return "\n".join(lines)


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


def _json_text(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_float(value: Any) -> Any:
    numeric = _float_or_none(value)
    if numeric is None:
        return value
    return round(numeric, 5)


def _utc_now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _cutoff_ms(hours: float, *, now_ms: int | None = None) -> int:
    try:
        hours_value = max(0.0, float(hours))
    except (TypeError, ValueError):
        hours_value = 4.0
    current_ms = int(now_ms if now_ms is not None else _now_ms())
    return current_ms - int(hours_value * 3600.0 * 1000.0)


def _safe_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


def _read_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
    except Exception:
        pass
    return conn


def _decode_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _decode_alert_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["violated_parameter_names"] = _decode_json(row.get("violated_parameter_names_json"), [])
    decoded["violations"] = _decode_json(row.get("violations_json"), [])
    decoded["thresholds"] = _decode_json(row.get("thresholds_json"), {})
    decoded["measurements"] = _decode_json(row.get("measurements_json"), {})
    decoded["stock"] = _decode_json(row.get("stock_json"), {})
    decoded["loads"] = _decode_json(row.get("loads_json"), {})
    decoded["payload"] = _decode_json(row.get("payload_json"), {})
    return decoded


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

CREATE TABLE IF NOT EXISTS {THRESHOLD_ALERT_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT,
    partition INTEGER,
    offset INTEGER,
    schema_version INTEGER,
    source TEXT NOT NULL,
    alert_id TEXT NOT NULL UNIQUE,
    message_type TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT,
    tank_id TEXT NOT NULL,
    tank_name TEXT,
    tank_path TEXT,
    event_time TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    seq INTEGER,
    sim_time_h REAL,
    violated_parameter_names_json TEXT NOT NULL,
    violations_json TEXT NOT NULL,
    thresholds_json TEXT NOT NULL,
    measurements_json TEXT NOT NULL,
    stock_json TEXT NOT NULL,
    loads_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_{THRESHOLD_ALERT_TABLE}_event_time_ms
    ON {THRESHOLD_ALERT_TABLE}(event_time_ms);

CREATE INDEX IF NOT EXISTS idx_{THRESHOLD_ALERT_TABLE}_tank_time
    ON {THRESHOLD_ALERT_TABLE}(tank_id, event_time_ms);

CREATE INDEX IF NOT EXISTS idx_{THRESHOLD_ALERT_TABLE}_event_type
    ON {THRESHOLD_ALERT_TABLE}(event_type);
"""


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    existing = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({WIDE_TABLE})").fetchall()
    }
    for key in SENSOR_COLUMNS:
        if key not in existing:
            conn.execute(f"ALTER TABLE {WIDE_TABLE} ADD COLUMN {key} BOOLEAN NOT NULL DEFAULT FALSE")
            existing.add(key)
    for key in kafka_payload.MEASUREMENT_KEYS:
        if key not in existing:
            conn.execute(f"ALTER TABLE {WIDE_TABLE} ADD COLUMN {key} REAL")
            existing.add(key)

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
