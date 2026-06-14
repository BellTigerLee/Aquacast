"""Pure fish survival rules for Aquacast water-quality coupling."""

from __future__ import annotations

from typing import Any, Mapping

import water_quality_bands


DEFAULT_SHARED_SALMON_THRESHOLDS = water_quality_bands.DEFAULT_WQ_METRIC_BANDS


def _float_from(snapshot: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in snapshot:
            continue
        value = snapshot.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _threshold_value(thresholds: Mapping[str, Any], group: str, key: str) -> float | None:
    values = thresholds.get(group, {}) if isinstance(thresholds, Mapping) else {}
    if not isinstance(values, Mapping) or key not in values:
        return None
    try:
        return float(values[key])
    except (TypeError, ValueError):
        return None


def critical_reasons(
    snapshot: Mapping[str, Any],
    thresholds: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return critical water-quality violations for shared salmon survival rules."""
    thresholds = thresholds or DEFAULT_SHARED_SALMON_THRESHOLDS
    if _uses_band_thresholds(thresholds):
        states = water_quality_bands.snapshot_states(snapshot, thresholds)
        return water_quality_bands.reasons_for_states(states, {water_quality_bands.STATE_CRITICAL})

    reasons: list[str] = []

    temp_c = _float_from(snapshot, "temperature_c", "temperature")
    temp_high = _threshold_value(thresholds, "temperature", "critical_high")
    temp_low = _threshold_value(thresholds, "temperature", "critical_low")
    if temp_c is not None and temp_high is not None and temp_c > temp_high:
        reasons.append("temperature_high")
    if temp_c is not None and temp_low is not None and temp_c < temp_low:
        reasons.append("temperature_low")

    do_mg_l = _float_from(snapshot, "dissolved_oxygen_mg_l", "do_mg_l", "dissolved_oxygen")
    do_low = _threshold_value(thresholds, "dissolved_oxygen", "critical_low_mg_l")
    if do_mg_l is not None and do_low is not None and do_mg_l < do_low:
        reasons.append("dissolved_oxygen_low")

    nh3_mg_l = _float_from(snapshot, "nh3_mg_l", "nh3")
    nh3_high = _threshold_value(thresholds, "nh3", "critical_high")
    if nh3_mg_l is not None and nh3_high is not None and nh3_mg_l > nh3_high:
        reasons.append("nh3_high")

    ph = _float_from(snapshot, "ph")
    ph_low = _threshold_value(thresholds, "ph", "critical_low")
    ph_high = _threshold_value(thresholds, "ph", "critical_high")
    if ph is not None and ph_low is not None and ph < ph_low:
        reasons.append("ph_low")
    if ph is not None and ph_high is not None and ph > ph_high:
        reasons.append("ph_high")

    co2_mg_l = _float_from(snapshot, "co2_mg_l", "co2")
    co2_high = _threshold_value(thresholds, "co2", "critical_high")
    if co2_mg_l is not None and co2_high is not None and co2_mg_l > co2_high:
        reasons.append("co2_high")

    return reasons


def _uses_band_thresholds(thresholds: Mapping[str, Any]) -> bool:
    if not isinstance(thresholds, Mapping):
        return False
    for key, value in thresholds.items():
        if key in water_quality_bands.DEFAULT_WQ_METRIC_BANDS:
            return True
        if isinstance(value, Mapping) and "bands" in value:
            return True
    return False


def water_quality_state(
    snapshot: Mapping[str, Any],
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the tank-level fish health state from water-quality bands."""
    thresholds = thresholds or DEFAULT_SHARED_SALMON_THRESHOLDS
    if not _uses_band_thresholds(thresholds):
        critical = critical_reasons(snapshot, thresholds)
        return {
            "state": water_quality_bands.STATE_CRITICAL if critical else water_quality_bands.STATE_HEALTHY,
            "critical_reasons": critical,
            "warn_reasons": [],
            "states": {},
        }

    states = water_quality_bands.snapshot_states(snapshot, thresholds)
    return {
        "state": water_quality_bands.worst_state(states),
        "critical_reasons": water_quality_bands.reasons_for_states(states, {water_quality_bands.STATE_CRITICAL}),
        "warn_reasons": water_quality_bands.reasons_for_states(states, {water_quality_bands.STATE_WARN}),
        "states": states,
    }


def next_survival_state(
    snapshot: Mapping[str, Any],
    stress_ticks: int,
    death_ticks: int,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance one survival tick and report whether mortality should occur."""
    quality_state = water_quality_state(snapshot, thresholds)
    reasons = list(quality_state.get("critical_reasons", []))
    warn_reasons = list(quality_state.get("warn_reasons", []))
    state = str(quality_state.get("state") or water_quality_bands.STATE_HEALTHY)
    if not reasons:
        return {
            "critical": False,
            "stress_ticks": 0,
            "dead": False,
            "reason": ",".join(warn_reasons),
            "wq_state": state,
            "wq_state_reason": ",".join(warn_reasons),
        }

    next_ticks = max(0, int(stress_ticks)) + 1
    required_ticks = max(1, int(death_ticks))
    return {
        "critical": True,
        "stress_ticks": next_ticks,
        "dead": next_ticks >= required_ticks,
        "reason": ",".join(reasons),
        "wq_state": state,
        "wq_state_reason": ",".join(reasons),
    }
