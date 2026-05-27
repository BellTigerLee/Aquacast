"""Deterministic water-quality rule engine for Aquacast."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

import thermal_dynamics
import water_quality_dynamics as dynamics


DEFAULT_SENSOR_NAMES = (
    "inlet_reference",
    "feed_zone_tan",
    "fish_core_do",
    "bottom_co2",
    "biofilter_sentinel",
    "mixed_tank_outlet",
)


@dataclass
class WaterQualityState:
    temperature_c: float = 14.0
    dissolved_oxygen_mg_l: float = 9.0
    tan_mg_l: float = 0.3
    co2_mg_l: float = 5.0
    alkalinity_mg_l_as_caco3: float = 120.0
    feed_pool_kg: float = 0.0
    sim_time_h: float = 0.0

    @property
    def do_mg_l(self) -> float:
        return self.dissolved_oxygen_mg_l

    @property
    def ph(self) -> float:
        return dynamics.ph_from_carbonate(self.co2_mg_l, self.alkalinity_mg_l_as_caco3)

    @property
    def nh3_mg_l(self) -> float:
        return self.tan_mg_l * dynamics.nh3_fraction(self.temperature_c, self.ph)

    def as_dict(self) -> dict[str, float]:
        ph = self.ph
        return {
            "temperature_c": self.temperature_c,
            "dissolved_oxygen_mg_l": self.dissolved_oxygen_mg_l,
            "do_mg_l": self.dissolved_oxygen_mg_l,
            "tan_mg_l": self.tan_mg_l,
            "co2_mg_l": self.co2_mg_l,
            "alkalinity_mg_l_as_caco3": self.alkalinity_mg_l_as_caco3,
            "ph": ph,
            "nh3_mg_l": self.tan_mg_l * dynamics.nh3_fraction(self.temperature_c, ph),
            "feed_pool_kg": self.feed_pool_kg,
            "sim_time_h": self.sim_time_h,
            "nitrite_mg_l": 0.0,
            "nitrate_mg_l": 0.0,
        }


@dataclass(frozen=True)
class SensorReading:
    sensor_name: str
    values: dict[str, float]

    def as_dict(self) -> dict[str, float | str]:
        return {"sensor_name": self.sensor_name, **self.values}


class WaterQualityModel:
    def __init__(
        self,
        constants: Mapping[str, Any] | None = None,
        feed_rate: Mapping[str, Any] | None = None,
        scenario: Mapping[str, Any] | None = None,
        scenarios: Mapping[str, Any] | None = None,
    ):
        self.constants = copy.deepcopy(dict(constants or {}))
        self.feed_rate = copy.deepcopy(dict(feed_rate or {}))
        self.scenarios = copy.deepcopy(dict(scenarios or {}))
        self.params = self._default_params()
        self.params.update(self.constants)
        scenario_dict = copy.deepcopy(dict(scenario or {}))
        self.params.update(scenario_dict.get("params", {}))
        self.state = self._state_from_initial(scenario_dict.get("initial_state", scenario_dict.get("state", {})))
        self._action_queue: list[dict[str, Any]] = []
        self.last_substep_count = 0
        self.last_derivatives: dict[str, float] = {}
        self.last_feed_base_kg_h = 0.0
        self._scenario_name = str(scenario_dict.get("name", "baseline"))
        self._particle_feature_cache: dict[str, Any] = {}

    def advance(self, real_dt_s: float, *, temperature_c: float | None = None) -> WaterQualityState:
        sim_h = max(0.0, float(real_dt_s)) * max(0.0, float(self.params.get("time_scale", 1.0)))
        if sim_h <= 0.0:
            return self.state

        substep_h = max(1e-6, float(self.params.get("substep_h", 0.0167)))
        n_steps = max(1, int(math.ceil(sim_h / substep_h)))
        dt_h = sim_h / n_steps
        self.last_substep_count = n_steps

        for step_index in range(n_steps):
            self._drain_actions()
            external_temperature = temperature_c if temperature_c is not None else None
            self._advance_one_substep(dt_h, external_temperature)
            self.state.sim_time_h += dt_h
        return self.state

    def step(self, dt_s: float, *, temperature_c: float | None = None, inflow_enabled: bool | None = None) -> WaterQualityState:
        if inflow_enabled is not None:
            self.params["inflow_enabled"] = bool(inflow_enabled)
        return self.advance(dt_s, temperature_c=temperature_c)

    def apply_action(self, action: Mapping[str, Any]) -> None:
        self._action_queue.append(dict(action))

    def apply_feed(self, mass_kg: float) -> None:
        self.apply_action({"type": "feed", "mass_kg": float(mass_kg)})

    def set_water_exchange(self, q_lph: float) -> None:
        self.apply_action({"type": "set_water_exchange", "q_lph": float(q_lph)})

    def set_inflow(self, enabled: bool) -> None:
        self.apply_action({"type": "set_inflow", "enabled": bool(enabled)})

    def set_heater(self, power: float) -> None:
        self.apply_action({"type": "set_heater", "power": float(power)})

    def set_biofilter(self, enabled: bool) -> None:
        self.apply_action({"type": "set_biofilter", "enabled": bool(enabled)})

    def set_stock(self, n: float, w_kg: float) -> None:
        self.apply_action({"type": "set_stock", "fish_count": float(n), "fish_weight_kg": float(w_kg)})

    def load_scenario(self, name: str) -> bool:
        scenario = self.scenarios.get(name)
        if not scenario:
            return False
        self.params = self._default_params()
        self.params.update(self.constants)
        self.params.update(scenario.get("params", {}))
        self.state = self._state_from_initial(scenario.get("initial_state", scenario.get("state", {})))
        self._action_queue.clear()
        for action in scenario.get("actions", []):
            self.apply_action(action)
        self._scenario_name = str(name)
        return True

    def snapshot(self) -> dict[str, float | bool | str]:
        values: dict[str, float | bool | str] = self.state.as_dict()
        values.update(
            {
                "biofilter_on": bool(self.params.get("biofilter_on", True)),
                "inflow_enabled": bool(self.params.get("inflow_enabled", True)),
                "flow_lph": float(self.params.get("flow_lph", 0.0)),
                "fish_count": float(self.params.get("fish_count", 0.0)),
                "fish_weight_kg": float(self.params.get("fish_weight_kg", 0.0)),
                "scenario": self._scenario_name,
                "feed_rate_kg_h": float(self.last_derivatives.get("feed_rate_kg_h", 0.0)),
                "baseline_feed_kg_h": float(self.last_feed_base_kg_h),
            }
        )
        return values

    def sensor_reading(self, sensor_name: str) -> SensorReading:
        name = sensor_name if sensor_name in DEFAULT_SENSOR_NAMES else "mixed_tank_outlet"
        factors = self._sensor_factors(name)
        snap = self.state.as_dict()
        temp = snap["temperature_c"] + factors.get("temperature_offset_c", 0.0)
        tan = max(0.0, snap["tan_mg_l"] * factors.get("tan", 1.0))
        do = max(0.0, snap["dissolved_oxygen_mg_l"] * factors.get("dissolved_oxygen", factors.get("do", 1.0)))
        co2 = max(0.0, snap["co2_mg_l"] * factors.get("co2", 1.0))
        alk = max(0.0, snap["alkalinity_mg_l_as_caco3"] * factors.get("alkalinity", 1.0))
        ph = dynamics.clamp(dynamics.ph_from_carbonate(co2, alk) + factors.get("ph_offset", 0.0), 4.0, 10.0)
        return SensorReading(
            sensor_name=name,
            values={
                "temperature_c": temp,
                "dissolved_oxygen_mg_l": do,
                "do_mg_l": do,
                "tan_mg_l": tan,
                "co2_mg_l": co2,
                "alkalinity_mg_l_as_caco3": alk,
                "ph": ph,
                "nh3_mg_l": tan * dynamics.nh3_fraction(temp, ph),
                "nitrite_mg_l": 0.0,
                "nitrate_mg_l": 0.0,
            },
        )

    def particle_values(self, heat_weights: list[float], positions: list[Any] | None = None) -> dict[str, list[float]]:
        count = len(heat_weights)
        if count <= 0:
            return {}

        state = self.state
        features = self._particle_features(heat_weights, positions, count)
        weights = features["weights"]
        y_norm = features["y_norm"]
        radial_norm = features["radial_norm"]

        tan_weight = np.clip(0.45 * weights + 0.55 * (1.0 - y_norm), 0.0, 1.0)
        co2_weight = np.clip(0.55 * (1.0 - y_norm) + 0.45 * radial_norm, 0.0, 1.0)
        do_weight = np.clip(0.45 * y_norm + 0.55 * (1.0 - weights), 0.0, 1.0)
        ph_weight = co2_weight

        temperature = state.temperature_c + 0.15 * weights
        tan = np.maximum(0.0, state.tan_mg_l * (0.78 + 0.44 * tan_weight))
        co2 = np.maximum(0.0, state.co2_mg_l * (0.82 + 0.36 * co2_weight))
        do = np.maximum(0.0, state.dissolved_oxygen_mg_l * (0.92 + 0.16 * do_weight))
        alk = np.maximum(0.0, state.alkalinity_mg_l_as_caco3 * (1.0 - 0.025 * ph_weight))
        alk_mol = np.maximum(1e-12, alk / 50000.0)
        co2_mol = np.maximum(1e-12, co2 / 44000.0)
        ph = np.clip(6.35 + np.log10(alk_mol / co2_mol), 4.0, 10.0)
        pka = 0.09018 + 2729.92 / np.maximum(1.0, temperature + 273.15)
        nh3 = tan * np.clip(1.0 / (1.0 + np.power(10.0, pka - ph)), 0.0, 1.0)
        return {
            "temperature": temperature.tolist(),
            "dissolved_oxygen": do.tolist(),
            "tan": tan.tolist(),
            "co2": co2.tolist(),
            "alkalinity": alk.tolist(),
            "ph": ph.tolist(),
            "nh3": nh3.tolist(),
        }

    def _particle_features(self, heat_weights: list[float], positions: list[Any] | None, count: int) -> dict[str, np.ndarray]:
        cache = self._particle_feature_cache
        cache_key = (count, id(heat_weights), id(positions))
        if cache.get("key") == cache_key:
            return cache["features"]

        weights = np.clip(np.asarray(heat_weights, dtype=np.float64), 0.0, 1.0)
        if positions is not None and len(positions) >= count:
            arr = np.empty((count, 3), dtype=np.float64)
            for index, pos in enumerate(positions[:count]):
                arr[index, 0] = float(pos[0])
                arr[index, 1] = float(pos[1])
                arr[index, 2] = float(pos[2])
            y_norm = (arr[:, 1] - np.min(arr[:, 1])) / max(1e-9, float(np.ptp(arr[:, 1])))
            radial = np.hypot(arr[:, 0] - np.mean(arr[:, 0]), arr[:, 2] - np.mean(arr[:, 2]))
            radial_norm = radial / max(1e-9, float(np.max(radial)))
        else:
            y_norm = np.zeros(count, dtype=np.float64)
            radial_norm = weights

        features = {"weights": weights, "y_norm": y_norm, "radial_norm": radial_norm}
        self._particle_feature_cache = {"key": cache_key, "features": features}
        return features

    def _advance_one_substep(self, dt_h: float, external_temperature_c: float | None) -> None:
        if external_temperature_c is None:
            self.state.temperature_c = thermal_dynamics.step_temperature(
                self.state.temperature_c,
                dt_h,
                T_room=float(self.params.get("room_temp_c", 22.0)) + float(self.params.get("heater_power", 0.0)),
                T_inlet=float(self.params.get("inlet_temp_c", 12.0)),
                k_room=float(self.params.get("thermal_k_room_h", 0.012)),
                k_inflow=float(self.params.get("thermal_k_inflow_h", 0.022)),
                inflow_enabled=bool(self.params.get("inflow_enabled", True)),
            )
        else:
            self.state.temperature_c = float(external_temperature_c)

        baseline_feed = self._baseline_feed_kg_h()
        self.last_feed_base_kg_h = baseline_feed
        self.state.feed_pool_kg = max(
            0.0,
            self.state.feed_pool_kg + (baseline_feed - self.state.feed_pool_kg / max(1e-9, float(self.params.get("tau_feed_h", 4.0)))) * dt_h,
        )

        deriv = dynamics.derivatives(self.state.as_dict(), self.params)
        self.last_derivatives = deriv
        self.state.dissolved_oxygen_mg_l = max(
            0.0, self.state.dissolved_oxygen_mg_l + deriv["dissolved_oxygen_mg_l"] * dt_h
        )
        self.state.tan_mg_l = max(0.0, self.state.tan_mg_l + deriv["tan_mg_l"] * dt_h)
        self.state.co2_mg_l = max(0.0, self.state.co2_mg_l + deriv["co2_mg_l"] * dt_h)
        self.state.alkalinity_mg_l_as_caco3 = max(
            0.0,
            self.state.alkalinity_mg_l_as_caco3 + deriv["alkalinity_mg_l_as_caco3"] * dt_h,
        )

    def _baseline_feed_kg_h(self) -> float:
        fish_count = max(0.0, float(self.params.get("fish_count", 200.0)))
        fish_weight_kg = max(0.0, float(self.params.get("fish_weight_kg", 1.0)))
        biomass = fish_count * fish_weight_kg
        dfr = self._daily_feed_rate_fraction(self.state.temperature_c, fish_weight_kg)
        appetite = dynamics.appetite_factor(
            self.state.dissolved_oxygen_mg_l,
            do_zero=float(self.params.get("do_zero", 3.0)),
            do_maxFI=float(self.params.get("do_maxFI", self.params.get("do_maxfi", 7.0))),
        )
        return biomass * dfr * appetite / 24.0

    def _daily_feed_rate_fraction(self, temp_c: float, fish_weight_kg: float) -> float:
        temps = np.asarray(self.feed_rate.get("temperature_c", [8.0, 12.0, 16.0, 20.0]), dtype=np.float64)
        weights = np.asarray(self.feed_rate.get("fish_weight_kg", [0.25, 0.5, 1.0, 2.0]), dtype=np.float64)
        table = np.asarray(
            self.feed_rate.get(
                "percent_bw_day",
                [
                    [0.8, 0.65, 0.5, 0.35],
                    [1.4, 1.1, 0.85, 0.65],
                    [1.8, 1.45, 1.1, 0.8],
                    [1.0, 0.8, 0.6, 0.45],
                ],
            ),
            dtype=np.float64,
        )
        if table.shape != (len(temps), len(weights)):
            return max(0.0, float(self.feed_rate.get("default_percent_bw_day", 1.0))) / 100.0
        temp_interp = np.asarray([np.interp(float(temp_c), temps, row) for row in table.T], dtype=np.float64)
        percent = float(np.interp(float(fish_weight_kg), weights, temp_interp))
        return max(0.0, percent) / 100.0

    def _drain_actions(self) -> None:
        if not self._action_queue:
            return
        actions = self._action_queue
        self._action_queue = []
        for action in actions:
            kind = str(action.get("type", "")).strip().lower()
            if kind in {"feed", "apply_feed"}:
                self.state.feed_pool_kg = max(0.0, self.state.feed_pool_kg + max(0.0, float(action.get("mass_kg", 0.0))))
            elif kind == "set_water_exchange":
                self.params["flow_lph"] = max(0.0, float(action.get("q_lph", 0.0)))
            elif kind == "set_inflow":
                self.params["inflow_enabled"] = bool(action.get("enabled", True))
            elif kind == "set_heater":
                self.params["heater_power"] = float(action.get("power", 0.0))
            elif kind == "set_biofilter":
                self.params["biofilter_on"] = bool(action.get("enabled", True))
            elif kind == "set_stock":
                self.params["fish_count"] = max(0.0, float(action.get("fish_count", action.get("n", 0.0))))
                self.params["fish_weight_kg"] = max(1e-9, float(action.get("fish_weight_kg", action.get("w_kg", 1.0))))
            elif kind == "load_scenario":
                self.load_scenario(str(action.get("name", "baseline")))

    def _state_from_initial(self, initial: Mapping[str, Any]) -> WaterQualityState:
        return WaterQualityState(
            temperature_c=float(initial.get("temperature_c", initial.get("temperature", 14.0))),
            dissolved_oxygen_mg_l=float(initial.get("dissolved_oxygen_mg_l", initial.get("do_mg_l", 9.0))),
            tan_mg_l=float(initial.get("tan_mg_l", 0.3)),
            co2_mg_l=float(initial.get("co2_mg_l", 5.0)),
            alkalinity_mg_l_as_caco3=float(initial.get("alkalinity_mg_l_as_caco3", initial.get("alk_mg_l", 120.0))),
            feed_pool_kg=float(initial.get("feed_pool_kg", 0.0)),
            sim_time_h=float(initial.get("sim_time_h", 0.0)),
        )

    def _default_params(self) -> dict[str, Any]:
        return {
            "tank_volume_l": 10000.0,
            "fish_count": 200.0,
            "fish_weight_kg": 1.0,
            "flow_lph": 2000.0,
            "protein_content": 0.45,
            "kla_o2_h": 2.0,
            "kla_co2_h": 1.5,
            "k_nitrif_h": 0.8,
            "vtr_max_mg_l_h": 5.0,
            "tau_feed_h": 4.0,
            "do_maxFI": 7.0,
            "do_zero": 3.0,
            "do_in": 9.0,
            "co2_eq": 0.5,
            "alk_in": 120.0,
            "biofilter_on": True,
            "inflow_enabled": True,
            "time_scale": 1.0,
            "substep_h": 0.0167,
            "tan_per_feed": 0.092,
            "co2_per_o2": 1.375,
            "alk_per_tan": 7.14,
            "o2_per_tan": 4.57,
            "o2_per_feed": 0.225,
            "mo2_a": 83.0,
            "mo2_w_exp": -0.14,
            "mo2_q10": 2.5,
            "mo2_t_ref": 10.0,
            "pk1": 6.35,
        }

    def _sensor_factors(self, sensor_name: str) -> dict[str, float]:
        return {
            "inlet_reference": {"tan": 0.72, "dissolved_oxygen": 1.08, "co2": 0.72, "temperature_offset_c": -0.12},
            "feed_zone_tan": {"tan": 1.45, "dissolved_oxygen": 0.90, "co2": 1.18, "ph_offset": -0.03},
            "fish_core_do": {"tan": 1.05, "dissolved_oxygen": 0.84, "co2": 1.16, "ph_offset": -0.02},
            "bottom_co2": {"tan": 1.08, "dissolved_oxygen": 0.88, "co2": 1.36, "ph_offset": -0.06},
            "biofilter_sentinel": {"tan": 0.62, "dissolved_oxygen": 0.96, "co2": 1.02},
            "mixed_tank_outlet": {},
        }.get(sensor_name, {})


def _read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def load_model(constants_path: str | Path, feed_rate_path: str | Path, scenarios_path: str | Path, scenario_name: str) -> WaterQualityModel:
    constants = _read_json(constants_path)
    feed_rate = _read_json(feed_rate_path)
    scenarios = _read_json(scenarios_path)
    scenario = copy.deepcopy(scenarios.get(scenario_name) or scenarios.get("baseline") or {})
    scenario.setdefault("name", scenario_name)
    return WaterQualityModel(constants, feed_rate, scenario, scenarios)
