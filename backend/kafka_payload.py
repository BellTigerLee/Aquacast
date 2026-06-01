"""Pure helpers for water-quality Kafka payloads."""

from __future__ import annotations

from datetime import datetime, timezone
import json


# `do_mg_l` is a duplicate of `dissolved_oxygen_mg_l` in the model reading.
MEASUREMENT_KEYS = (
    "temperature_c",
    "dissolved_oxygen_mg_l",
    "tan_mg_l",
    "nh3_mg_l",
    "co2_mg_l",
    "ph",
    "alkalinity_mg_l_as_caco3",
    "nitrite_mg_l",
    "nitrate_mg_l",
)


def iso_from_ms(event_time_ms: int) -> str:
    dt = datetime.fromtimestamp(event_time_ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def message_key(tank_id: str, sensor_name: str) -> bytes:
    return f"{tank_id}:{sensor_name}".encode("utf-8")


def build_message(
    reading: dict,
    *,
    tank_id: str,
    event_time_ms: int,
    seq: int,
    sim_time_h: float | None = None,
    schema_version: int = 1,
    source: str = "aquacast-backend",
) -> dict | None:
    status = reading.get("status")
    if status is not None and status != "ok":
        return None

    sensor_name = reading.get("sensor_name")
    if not sensor_name:
        return None

    message = {
        "schema_version": schema_version,
        "source": source,
        "tank_id": tank_id,
        "sensor_name": sensor_name,
        "event_time": iso_from_ms(event_time_ms),
        "event_time_ms": event_time_ms,
        "seq": seq,
        "measurements": {key: reading[key] for key in MEASUREMENT_KEYS if key in reading},
    }
    if sim_time_h is not None:
        message["sim_time_h"] = sim_time_h
    return message


def serialize(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
