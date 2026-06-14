from __future__ import annotations

import sqlite3
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aquacast_db as db  # noqa: E402
import kafka_payload as kp  # noqa: E402


def _reading(sensor_name: str) -> dict:
    return {
        "sensor_name": sensor_name,
        "temperature_c": 14.2,
        "dissolved_oxygen_mg_l": 8.8,
        "tan_mg_l": 0.42,
        "nh3_mg_l": 0.012,
        "co2_mg_l": 5.1,
        "ph": 7.2,
        "alkalinity_mg_l_as_caco3": 120.0,
        "salinity_ppt": 0.25,
        "turbidity_ntu": 2.5,
        "nitrite_mg_l": 0.03,
        "nitrate_mg_l": 7.5,
    }


def test_wide_insert_merges_sensor_messages_and_keeps_new_metrics(tmp_path):
    db_path = tmp_path / "aquacast.db"
    db.init_db(db_path)
    event_time_ms = 1717245296789

    with sqlite3.connect(db_path) as conn:
        for seq, sensor_name in enumerate(kp.SENSOR_MEASUREMENT_KEYS, start=1):
            message = kp.build_message(
                _reading(sensor_name),
                tank_id="tank-01",
                event_time_ms=event_time_ms,
                seq=seq,
                sim_time_h=1.5,
            )
            db.insert_kafka_message(conn, message, topic="aquacast.water_quality")
        conn.commit()

    rows = db.query_recent_wide(db_path, hours=1.0, now_ms=event_time_ms + 1000)
    assert len(rows) == 1
    row = rows[0]
    assert row["tan_mg_l"] == 0.42
    assert row["nh3_mg_l"] == 0.012
    assert row["salinity_ppt"] == 0.25
    assert row["turbidity_ntu"] == 2.5
    assert row["mixed_tank_outlet"] == 1

    dashboard_row = db.dashboard_rows_from_wide(rows)[0]
    assert dashboard_row["TAN"] == 0.42
    assert dashboard_row["ammonia"] == 0.012
    assert dashboard_row["salinity"] == 0.25
    assert dashboard_row["turbidity"] == 2.5


def test_init_db_migrates_missing_salinity_and_turbidity_columns(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE {db.WIDE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                tank_id TEXT NOT NULL,
                event_time TEXT NOT NULL,
                event_time_ms INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tank_id, event_time_ms)
            )
            """
        )
        conn.commit()

    db.init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({db.WIDE_TABLE})")}
    assert "tan_mg_l" in columns
    assert "nh3_mg_l" in columns
    assert "salinity_ppt" in columns
    assert "turbidity_ntu" in columns


def test_threshold_alert_insert_and_context_payload(tmp_path):
    db_path = tmp_path / "aquacast.db"
    db.init_db(db_path)
    event_time_ms = 1717245296789
    alert = {
        "schema_version": 1,
        "message_type": "threshold_alert",
        "source": "aquacast-backend",
        "alert_id": "tank-01:threshold:1717245296789",
        "event_type": "threshold_violation",
        "severity": "critical",
        "tank_id": "tank-01",
        "tank_name": "tank-01",
        "tank_path": "/World/Fishtank_1/Water",
        "event_time": kp.iso_from_ms(event_time_ms),
        "event_time_ms": event_time_ms,
        "seq": 99,
        "sim_time_h": 1.5,
        "violated_parameter_names": ["turbidity_ntu", "nh3_mg_l"],
        "violations": [
            {"parameter": "turbidity_ntu", "value": 25.0, "band_state": "critical"},
            {"parameter": "nh3_mg_l", "value": 0.05, "band_state": "critical"},
        ],
        "thresholds": {},
        "measurements": {"tan_mg_l": 0.9, "nh3_mg_l": 0.05, "turbidity_ntu": 25.0},
        "stock": {"fish_count": 100},
        "loads": {"feed_rate_kg_h": 1.2},
    }

    with sqlite3.connect(db_path) as conn:
        db.insert_threshold_alert(conn, alert, topic="aquacast.threshold_alert")
        conn.commit()

    rows = db.query_recent_threshold_alerts(db_path, hours=1.0, now_ms=event_time_ms + 1000)
    assert len(rows) == 1
    assert rows[0]["violated_parameter_names"] == ["turbidity_ntu", "nh3_mg_l"]
    assert rows[0]["measurements"]["tan_mg_l"] == 0.9

    context = db.build_llm_context_payload(db_path, hours=1.0, now_ms=event_time_ms + 1000)
    assert context["threshold_alert_count"] == 1
    assert "Recent threshold/anomaly alerts" in context["context_text"]
