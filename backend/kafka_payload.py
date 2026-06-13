"""Pure helpers for water-quality Kafka payloads."""

from __future__ import annotations

from datetime import datetime, timezone
import json


# `do_mg_l` is a duplicate of `dissolved_oxygen_mg_l` in the model reading.
# `reference_measurements` carries the same canonical keys for inlet_reference.
MEASUREMENT_KEYS = (
    "temperature_c",
    "dissolved_oxygen_mg_l",
    "tan_mg_l",
    "nh3_mg_l",
    "co2_mg_l",
    "ph",
    "alkalinity_mg_l_as_caco3",
    "salinity_ppt",
    "turbidity_ntu",
    "nitrite_mg_l",
    "nitrate_mg_l",
)

THRESHOLD_PARAMETER_METADATA = {
    "dissolved_oxygen_mg_l": {"label": "Dissolved Oxygen", "unit": "mg/L", "mode": "min"},
    "tan_mg_l": {"label": "TAN", "unit": "mg/L", "mode": "max"},
    "ph": {"label": "pH", "unit": "", "mode": "max"},
    "co2_mg_l": {"label": "CO2", "unit": "mg/L", "mode": "max"},
}

STOCK_KEYS = (
    "fish_count",
    "fish_weight_kg",
    "biomass_kg",
)

LOAD_KEYS = (
    "feed_rate_kg_h",
    "baseline_feed_kg_h",
    "fish_o2_mg_h",
    "feed_o2_mg_h",
    "total_o2_mg_h",
    "fish_co2_mg_h",
    "feed_co2_mg_h",
    "total_co2_mg_h",
    "fish_tan_kg_h",
    "feed_tan_kg_h",
    "total_tan_kg_h",
    "r_nitrif_mg_l_h",
    "turbidity_source_ntu_h",
)

SENSOR_MEASUREMENT_KEYS = {
    "inlet_reference": (
        "alkalinity_mg_l_as_caco3",
        "salinity_ppt",
        "turbidity_ntu",
    ),
    "feed_zone_tan": (
        "tan_mg_l",
        "nh3_mg_l",
    ),
    "fish_core_do": (
        "temperature_c",
        "ph",
    ),
    "bottom_co2": (
        "co2_mg_l",
    ),
    "biofilter_sentinel": (
        "nitrite_mg_l",
        "nitrate_mg_l",
    ),
    "mixed_tank_outlet": (
        "dissolved_oxygen_mg_l",
    ),
}


def iso_from_ms(event_time_ms: int) -> str:
    dt = datetime.fromtimestamp(event_time_ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def message_key(tank_id: str, sensor_name: str) -> bytes:
    return f"{tank_id}:{sensor_name}".encode("utf-8")


def threshold_alert_key(tank_id: str) -> bytes:
    return str(tank_id or "tank").encode("utf-8")


def build_message(
    reading: dict,
    *,
    tank_id: str,
    event_time_ms: int,
    seq: int,
    sim_time_h: float | None = None,
    reference_reading: dict | None = None,
    schema_version: int = 2,
    source: str = "aquacast-backend",
) -> dict | None:
    status = reading.get("status")
    if status is not None and status != "ok":
        return None

    sensor_name = reading.get("sensor_name")
    if not sensor_name:
        return None

    measurement_keys = SENSOR_MEASUREMENT_KEYS.get(sensor_name, MEASUREMENT_KEYS)
    message = {
        "schema_version": schema_version,
        "source": source,
        "tank_id": tank_id,
        "sensor_name": sensor_name,
        "event_time": iso_from_ms(event_time_ms),
        "event_time_ms": event_time_ms,
        "seq": seq,
        "measurements": {key: reading[key] for key in measurement_keys if key in reading},
    }
    if sim_time_h is not None:
        message["sim_time_h"] = sim_time_h
    reference_measurements = _reference_measurements(reference_reading)
    if reference_measurements:
        message["reference_sensor_name"] = "inlet_reference"
        message["reference_measurements"] = reference_measurements
    return message


def build_threshold_alert(
    snapshot: dict,
    thresholds: dict,
    *,
    tank_id: str,
    event_time_ms: int,
    seq: int,
    tank_name: str | None = None,
    tank_path: str | None = None,
    schema_version: int = 1,
    source: str = "aquacast-backend",
) -> dict | None:
    violations = threshold_violations(snapshot, thresholds)
    if not violations:
        return None

    tank_id = str(tank_id or snapshot.get("tank_id") or "tank")
    tank_name = str(tank_name or snapshot.get("tank_name") or snapshot.get("tank_id") or tank_id)
    tank_path = str(tank_path or snapshot.get("tank_path") or "")
    event_time = iso_from_ms(event_time_ms)
    return {
        "schema_version": schema_version,
        "message_type": "threshold_alert",
        "source": source,
        "alert_id": f"{tank_id}:threshold:{event_time_ms}",
        "event_type": "threshold_violation",
        "severity": "warning",
        "tank_id": tank_id,
        "tank_name": tank_name,
        "tank_path": tank_path,
        "event_time": event_time,
        "event_time_ms": event_time_ms,
        "seq": seq,
        "sim_time_h": snapshot.get("sim_time_h"),
        "violated_parameter_names": [item["parameter"] for item in violations],
        "violations": violations,
        "thresholds": _normalized_thresholds(thresholds),
        "measurements": {key: snapshot[key] for key in MEASUREMENT_KEYS if key in snapshot},
        "stock": {key: snapshot[key] for key in STOCK_KEYS if key in snapshot},
        "loads": {key: snapshot[key] for key in LOAD_KEYS if key in snapshot},
    }


def threshold_violations(snapshot: dict, thresholds: dict) -> list[dict]:
    violations = []
    for parameter, threshold in _normalized_thresholds(thresholds).items():
        if parameter not in snapshot:
            continue
        try:
            value = float(snapshot[parameter])
            threshold_value = float(threshold["value"])
        except (TypeError, ValueError, KeyError):
            continue
        mode = threshold.get("mode") or THRESHOLD_PARAMETER_METADATA.get(parameter, {}).get("mode", "max")
        violated = value < threshold_value if mode == "min" else value > threshold_value
        if not violated:
            continue
        metadata = THRESHOLD_PARAMETER_METADATA.get(parameter, {})
        condition = "value < threshold" if mode == "min" else "value > threshold"
        violations.append(
            {
                "parameter": parameter,
                "label": metadata.get("label", parameter),
                "unit": metadata.get("unit", ""),
                "value": value,
                "threshold": threshold_value,
                "mode": mode,
                "condition": condition,
                "delta": value - threshold_value,
            }
        )
    return violations


def _normalized_thresholds(thresholds: dict | None) -> dict:
    raw = thresholds.get("thresholds") if isinstance(thresholds, dict) and "thresholds" in thresholds else thresholds
    raw = raw if isinstance(raw, dict) else {}
    normalized = {}
    for parameter, metadata in THRESHOLD_PARAMETER_METADATA.items():
        item = raw.get(parameter)
        if item is None:
            continue
        if isinstance(item, dict):
            value = item.get("value")
            mode = item.get("mode", metadata.get("mode", "max"))
        else:
            value = item
            mode = metadata.get("mode", "max")
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        mode = str(mode or metadata.get("mode", "max")).strip().lower()
        if mode not in {"min", "max"}:
            mode = metadata.get("mode", "max")
        normalized[parameter] = {"value": value, "mode": mode}
    return normalized


def _reference_measurements(reference_reading: dict | None) -> dict:
    if not isinstance(reference_reading, dict):
        return {}
    status = reference_reading.get("status")
    if status is not None and status != "ok":
        return {}
    return {key: reference_reading[key] for key in MEASUREMENT_KEYS if key in reference_reading}


def serialize(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
