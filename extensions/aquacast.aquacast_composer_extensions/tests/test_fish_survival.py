from pathlib import Path
import sys


EXT_ROOT = Path(__file__).resolve().parents[1]
if str(EXT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXT_ROOT))

import fish_survival


def safe_snapshot():
    return {
        "temperature_c": 10.0,
        "dissolved_oxygen_mg_l": 9.0,
        "nh3_mg_l": 0.005,
        "ph": 7.4,
        "co2_mg_l": 5.0,
        "tan_mg_l": 0.5,
        "alkalinity_mg_l_as_caco3": 100.0,
        "salinity_ppt": 0.2,
        "turbidity_ntu": 2.0,
        "nitrite_mg_l": 0.05,
        "nitrate_mg_l": 30.0,
    }


def test_safe_snapshot_resets_stress_ticks():
    state = fish_survival.next_survival_state(safe_snapshot(), stress_ticks=23, death_ticks=24)

    assert state["critical"] is False
    assert state["stress_ticks"] == 0
    assert state["dead"] is False


def test_critical_state_accumulates_without_death_before_limit():
    snapshot = {**safe_snapshot(), "dissolved_oxygen_mg_l": 3.9}

    state = fish_survival.next_survival_state(snapshot, stress_ticks=22, death_ticks=24)

    assert state["critical"] is True
    assert state["stress_ticks"] == 23
    assert state["dead"] is False
    assert state["reason"] == "dissolved_oxygen_mg_l_critical"


def test_critical_state_dies_at_limit():
    snapshot = {**safe_snapshot(), "temperature_c": 18.1}

    state = fish_survival.next_survival_state(snapshot, stress_ticks=23, death_ticks=24)

    assert state["stress_ticks"] == 24
    assert state["dead"] is True
    assert state["reason"] == "temperature_c_critical"


def test_multiple_critical_reasons_are_reported_in_order():
    snapshot = {
        **safe_snapshot(),
        "temperature_c": 18.1,
        "dissolved_oxygen_mg_l": 3.0,
        "nh3_mg_l": 0.06,
    }

    assert fish_survival.critical_reasons(snapshot) == [
        "temperature_c_critical",
        "dissolved_oxygen_mg_l_critical",
        "nh3_mg_l_critical",
    ]


def test_thresholds_can_disable_a_metric_by_omitting_it():
    snapshot = {**safe_snapshot(), "co2_mg_l": 20.0}

    reasons = fish_survival.critical_reasons(snapshot, thresholds={"temperature": {"critical_high": 20.0}})
    assert reasons == []


def test_warn_state_resets_death_counter_without_mortality():
    snapshot = {**safe_snapshot(), "temperature_c": 14.0}

    state = fish_survival.next_survival_state(snapshot, stress_ticks=23, death_ticks=24)

    assert state["critical"] is False
    assert state["dead"] is False
    assert state["stress_ticks"] == 0
    assert state["wq_state"] == "warn"
    assert state["wq_state_reason"] == "temperature_c_warn"


def test_table_critical_metrics_are_reported():
    expected = {
        "temperature_c": (18.1, "temperature_c_critical"),
        "dissolved_oxygen_mg_l": (4.9, "dissolved_oxygen_mg_l_critical"),
        "tan_mg_l": (3.1, "tan_mg_l_critical"),
        "nh3_mg_l": (0.051, "nh3_mg_l_critical"),
        "ph": (5.9, "ph_critical"),
        "co2_mg_l": (15.1, "co2_mg_l_critical"),
        "alkalinity_mg_l_as_caco3": (49.0, "alkalinity_mg_l_as_caco3_critical"),
        "turbidity_ntu": (20.1, "turbidity_ntu_critical"),
        "nitrite_mg_l": (1.1, "nitrite_mg_l_critical"),
        "nitrate_mg_l": (201.0, "nitrate_mg_l_critical"),
    }
    for metric, (value, reason) in expected.items():
        assert fish_survival.critical_reasons({**safe_snapshot(), metric: value}) == [reason]


def test_salinity_has_warn_but_no_critical_without_phase_rule():
    snapshot = {**safe_snapshot(), "salinity_ppt": 2.0}

    assert fish_survival.critical_reasons(snapshot) == []
    assert fish_survival.next_survival_state(snapshot, stress_ticks=23, death_ticks=24)["wq_state"] == "warn"
