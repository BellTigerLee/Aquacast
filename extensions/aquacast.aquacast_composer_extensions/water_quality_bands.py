"""Shared healthy/warn/critical water-quality band rules."""

from __future__ import annotations

from typing import Any, Mapping


STATE_HEALTHY = "healthy"
STATE_WARN = "warn"
STATE_CRITICAL = "critical"
STATE_UNKNOWN = "unknown"

STATE_PRIORITY = {
    STATE_UNKNOWN: 0,
    STATE_HEALTHY: 1,
    STATE_WARN: 2,
    STATE_CRITICAL: 3,
}


DEFAULT_WQ_METRIC_BANDS: dict[str, dict[str, list[dict[str, float]]]] = {
    "temperature_c": {
        STATE_HEALTHY: [{"gte": 11.5, "lte": 12.5}],
        STATE_WARN: [{"lt": 11.5}, {"gt": 12.5, "lte": 18.0}],
        STATE_CRITICAL: [{"gt": 18.0}],
    },
    "dissolved_oxygen_mg_l": {
        STATE_HEALTHY: [{"gte": 8.0}],
        STATE_WARN: [{"gte": 5.0, "lt": 8.0}],
        STATE_CRITICAL: [{"lt": 5.0}],
    },
    "tan_mg_l": {
        STATE_HEALTHY: [{"lt": 1.0}],
        STATE_WARN: [{"gte": 1.0, "lte": 3.0}],
        STATE_CRITICAL: [{"gt": 3.0}],
    },
    "nh3_mg_l": {
        STATE_HEALTHY: [{"lt": 0.0125}],
        STATE_WARN: [{"gte": 0.0125, "lte": 0.05}],
        STATE_CRITICAL: [{"gt": 0.05}],
    },
    "ph": {
        STATE_HEALTHY: [{"gte": 6.8, "lte": 8.3}],
        STATE_WARN: [{"gte": 6.0, "lt": 6.8}, {"gt": 8.3, "lte": 9.0}],
        STATE_CRITICAL: [{"lt": 6.0}, {"gt": 9.0}],
    },
    "co2_mg_l": {
        STATE_HEALTHY: [{"gte": 0.0, "lte": 5.0}],
        STATE_WARN: [{"gt": 5.0, "lte": 15.0}],
        STATE_CRITICAL: [{"gt": 15.0}],
    },
    "alkalinity_mg_l_as_caco3": {
        STATE_HEALTHY: [{"gte": 80.0, "lte": 150.0}],
        STATE_WARN: [{"gte": 50.0, "lt": 80.0}, {"gt": 150.0}],
        STATE_CRITICAL: [{"lt": 50.0}],
    },
    "salinity_ppt": {
        STATE_HEALTHY: [{"gte": 0.0, "lte": 0.5}],
        STATE_WARN: [{"gt": 0.5}],
        STATE_CRITICAL: [],
    },
    "turbidity_ntu": {
        STATE_HEALTHY: [{"lt": 5.0}],
        STATE_WARN: [{"gte": 5.0, "lte": 20.0}],
        STATE_CRITICAL: [{"gt": 20.0}],
    },
    "nitrite_mg_l": {
        STATE_HEALTHY: [{"lt": 0.1}],
        STATE_WARN: [{"gte": 0.1, "lte": 1.0}],
        STATE_CRITICAL: [{"gt": 1.0}],
    },
    "nitrate_mg_l": {
        STATE_HEALTHY: [{"lt": 50.0}],
        STATE_WARN: [{"gte": 50.0, "lte": 200.0}],
        STATE_CRITICAL: [{"gt": 200.0}],
    },
}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_condition(condition: Any) -> dict[str, float] | None:
    if not isinstance(condition, Mapping):
        return None
    normalized: dict[str, float] = {}
    aliases = {"min": "gte", "max": "lte"}
    for raw_key, raw_value in condition.items():
        key = aliases.get(str(raw_key), str(raw_key))
        if key not in {"lt", "lte", "gt", "gte"}:
            continue
        value = _as_float(raw_value)
        if value is not None:
            normalized[key] = value
    return normalized or None


def normalize_bands(thresholds: Mapping[str, Any] | None = None) -> dict[str, dict[str, list[dict[str, float]]]]:
    """Normalize metric band thresholds, accepting legacy value/mode entries."""
    raw = thresholds.get("thresholds") if isinstance(thresholds, Mapping) and "thresholds" in thresholds else thresholds
    raw = raw if isinstance(raw, Mapping) else {}
    normalized: dict[str, dict[str, list[dict[str, float]]]] = {}
    for metric, default_bands in DEFAULT_WQ_METRIC_BANDS.items():
        item = raw.get(metric, default_bands)
        normalized[metric] = _normalize_metric_item(item, default_bands)
    return normalized


def _normalize_metric_item(item: Any, default_bands: Mapping[str, Any]) -> dict[str, list[dict[str, float]]]:
    if isinstance(item, Mapping) and "bands" in item:
        item = item.get("bands")
    if isinstance(item, Mapping) and ("value" in item or "mode" in item):
        legacy = _legacy_threshold_to_bands(item)
        if legacy is not None:
            return legacy
    if not isinstance(item, Mapping):
        return _normalize_metric_item(default_bands, {}) if default_bands else {state: [] for state in _band_states()}

    bands: dict[str, list[dict[str, float]]] = {}
    for state in _band_states():
        raw_conditions = item.get(state, [])
        if isinstance(raw_conditions, Mapping):
            raw_conditions = [raw_conditions]
        if not isinstance(raw_conditions, (list, tuple)):
            raw_conditions = []
        conditions = []
        for condition in raw_conditions:
            normalized = _normalize_condition(condition)
            if normalized is not None:
                conditions.append(normalized)
        bands[state] = conditions
    return bands


def _band_states() -> tuple[str, str, str]:
    return (STATE_HEALTHY, STATE_WARN, STATE_CRITICAL)


def _legacy_threshold_to_bands(item: Mapping[str, Any]) -> dict[str, list[dict[str, float]]] | None:
    value = _as_float(item.get("value"))
    if value is None:
        return None
    mode = str(item.get("mode", "max") or "max").strip().lower()
    if mode == "min":
        return {STATE_HEALTHY: [{"gte": value}], STATE_WARN: [], STATE_CRITICAL: [{"lt": value}]}
    return {STATE_HEALTHY: [{"lte": value}], STATE_WARN: [], STATE_CRITICAL: [{"gt": value}]}


def condition_matches(value: float, condition: Mapping[str, Any]) -> bool:
    checks = (
        ("lt", lambda actual, limit: actual < limit),
        ("lte", lambda actual, limit: actual <= limit),
        ("gt", lambda actual, limit: actual > limit),
        ("gte", lambda actual, limit: actual >= limit),
    )
    for key, predicate in checks:
        limit = _as_float(condition.get(key))
        if limit is not None and not predicate(value, limit):
            return False
    return True


def metric_state(metric: str, value: Any, thresholds: Mapping[str, Any] | None = None) -> dict[str, Any]:
    actual = _as_float(value)
    if actual is None:
        return {"state": STATE_UNKNOWN, "condition": {}, "value": None}
    bands = normalize_bands(thresholds).get(metric, {})
    for state in (STATE_CRITICAL, STATE_HEALTHY, STATE_WARN):
        for condition in bands.get(state, []):
            if condition_matches(actual, condition):
                return {"state": state, "condition": dict(condition), "value": actual}
    return {"state": STATE_UNKNOWN, "condition": {}, "value": actual}


def snapshot_states(
    snapshot: Mapping[str, Any],
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    bands = normalize_bands(thresholds)
    return {
        metric: metric_state(metric, snapshot.get(metric), bands)
        for metric in bands
        if metric in snapshot
    }


def worst_state(states: Mapping[str, Mapping[str, Any]]) -> str:
    worst = STATE_HEALTHY
    for result in states.values():
        state = str(result.get("state") or STATE_UNKNOWN)
        if STATE_PRIORITY.get(state, 0) > STATE_PRIORITY.get(worst, 0):
            worst = state
    return worst


def reasons_for_states(states: Mapping[str, Mapping[str, Any]], wanted: set[str]) -> list[str]:
    reasons = []
    for metric, result in states.items():
        state = str(result.get("state") or STATE_UNKNOWN)
        if state in wanted:
            reasons.append(f"{metric}_{state}")
    return reasons


def condition_values(threshold: Mapping[str, Any] | None) -> list[float]:
    values: list[float] = []
    bands = _normalize_metric_item(threshold or {}, {}) if threshold else {state: [] for state in _band_states()}
    for conditions in bands.values():
        for condition in conditions:
            for value in condition.values():
                parsed = _as_float(value)
                if parsed is not None:
                    values.append(parsed)
    return values


def condition_label(condition: Mapping[str, Any]) -> str:
    lower_key = "gte" if "gte" in condition else "gt" if "gt" in condition else ""
    upper_key = "lte" if "lte" in condition else "lt" if "lt" in condition else ""
    if lower_key and upper_key:
        lower_op = ">=" if lower_key == "gte" else ">"
        upper_op = "<=" if upper_key == "lte" else "<"
        return f"{lower_op}{condition[lower_key]:g} and {upper_op}{condition[upper_key]:g}"
    if lower_key:
        lower_op = ">=" if lower_key == "gte" else ">"
        return f"{lower_op}{condition[lower_key]:g}"
    if upper_key:
        upper_op = "<=" if upper_key == "lte" else "<"
        return f"{upper_op}{condition[upper_key]:g}"
    return "any"
