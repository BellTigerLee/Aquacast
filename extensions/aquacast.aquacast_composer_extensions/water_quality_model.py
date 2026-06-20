"""Deterministic water-quality rule engine for Aquacast."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import math
import os
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
    temperature_c: float = 10.5
    dissolved_oxygen_mg_l: float = 9.0
    tan_mg_l: float = 0.3
    co2_mg_l: float = 5.0
    alkalinity_mg_l_as_caco3: float = 120.0
    salinity_ppt: float = 0.2
    turbidity_ntu: float = 2.0
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
            "salinity_ppt": self.salinity_ppt,
            "turbidity_ntu": self.turbidity_ntu,
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


@dataclass
class ParticleField:
    positions: np.ndarray
    graph: dict[str, Any]
    temperatures: np.ndarray
    heat_weights: np.ndarray
    graph_hash: str


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
        self._particle_field: ParticleField | None = None

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

    def apply_control(self, action: Mapping[str, Any]) -> dict[str, Any]:
        """Apply an operator/AI control action immediately and return a snapshot."""
        self._apply_action_now(dict(action))
        snap = self.snapshot()
        snap["status"] = "ok"
        snap["action"] = str(action.get("type", action.get("action", ""))).strip().lower()
        if action.get("tank_path") is not None:
            snap["tank_path"] = str(action.get("tank_path"))
        if action.get("tank_id") is not None:
            snap["tank_id"] = str(action.get("tank_id"))
        return snap

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
        self._particle_field = None
        self._particle_feature_cache = {}
        self._action_queue.clear()
        for action in scenario.get("actions", []):
            self.apply_action(action)
        self._scenario_name = str(name)
        return True

    def snapshot(self) -> dict[str, float | bool | str]:
        values: dict[str, float | bool | str] = self.state.as_dict()
        derivative_values = self._current_derivatives()
        try:
            heat = thermal_dynamics.net_heat_w(
                self.state.temperature_c,
                self.params,
                inflow_enabled=bool(self.params.get("inflow_enabled", True)),
            )
        except Exception:
            heat = {}
        inflow_enabled = bool(self.params.get("inflow_enabled", True))
        flow_lph = float(self.params.get("flow_lph", 0.0))
        q_makeup_lph = float(self.params.get("q_makeup_lph", flow_lph))
        heater_power_w = float(self.params.get("heater_power_w", self.params.get("heater_power", 0.0)))
        turbidity_settle_h = float(self.params.get("turbidity_settle_h", 0.0))
        values.update(
            {
                "biofilter_on": bool(self.params.get("biofilter_on", True)),
                "inflow_enabled": inflow_enabled,
                "inlet_enabled": bool(inflow_enabled and q_makeup_lph > 0.0),
                "outlet_enabled": bool(inflow_enabled and flow_lph > 0.0),
                "mechanical_filter_on": bool(turbidity_settle_h > 0.0),
                "heater_on": bool(heater_power_w > 0.0),
                "flow_lph": flow_lph,
                "q_makeup_lph": q_makeup_lph,
                "inlet_temp_c": float(self.params.get("inlet_temp_c", 12.0)),
                "do_in": float(self.params.get("do_in", 9.0)),
                "tan_in_mg_l": float(self.params.get("tan_in_mg_l", 0.0)),
                "co2_eq": float(self.params.get("co2_eq", 0.5)),
                "alk_in": float(self.params.get("alk_in", 120.0)),
                "salinity_in_ppt": float(self.params.get("salinity_in_ppt", 0.0)),
                "turbidity_in_ntu": float(self.params.get("turbidity_in_ntu", 1.0)),
                "kla_o2_h": float(self.params.get("kla_o2_h", 0.0)),
                "kla_co2_h": float(self.params.get("kla_co2_h", 0.0)),
                "k_nitrif_h": float(self.params.get("k_nitrif_h", self.params.get("k_nitrif", 0.0))),
                "vtr_max_mg_l_h": float(self.params.get("vtr_max_mg_l_h", self.params.get("vtr_max", 0.0))),
                "fish_count": float(self.params.get("fish_count", 0.0)),
                "fish_weight_kg": float(self.params.get("fish_weight_kg", 0.0)),
                "scenario": self._scenario_name,
                "feed_rate_kg_h": float(derivative_values.get("feed_rate_kg_h", 0.0)),
                "biomass_kg": float(derivative_values.get("biomass_kg", 0.0)),
                "fish_o2_mg_h": float(derivative_values.get("fish_o2_mg_h", 0.0)),
                "feed_o2_mg_h": float(derivative_values.get("feed_o2_mg_h", 0.0)),
                "total_o2_mg_h": float(derivative_values.get("total_o2_mg_h", 0.0)),
                "fish_co2_mg_h": float(derivative_values.get("fish_co2_mg_h", 0.0)),
                "feed_co2_mg_h": float(derivative_values.get("feed_co2_mg_h", 0.0)),
                "total_co2_mg_h": float(derivative_values.get("total_co2_mg_h", 0.0)),
                "fish_tan_kg_h": float(derivative_values.get("fish_tan_kg_h", 0.0)),
                "feed_tan_kg_h": float(derivative_values.get("feed_tan_kg_h", 0.0)),
                "total_tan_kg_h": float(derivative_values.get("total_tan_kg_h", 0.0)),
                "r_nitrif_mg_l_h": float(derivative_values.get("r_nitrif_mg_l_h", 0.0)),
                "turbidity_source_ntu_h": float(derivative_values.get("turbidity_source_ntu_h", 0.0)),
                "baseline_feed_kg_h": float(self.last_feed_base_kg_h),
                "heater_power_w": heater_power_w,
                "turbidity_settle_h": turbidity_settle_h,
                "tank_radius_m": float(self.params.get("tank_radius_m", 1.2)),
                "tank_water_height_m": float(self.params.get("tank_water_height_m", 2.21)),
                "thermal_q_net_w": float(heat.get("q_net_w", 0.0)),
                "thermal_q_surface_w": float(heat.get("q_surface_w", 0.0)),
                "thermal_q_wall_w": float(heat.get("q_wall_w", 0.0)),
                "thermal_q_adv_w": float(heat.get("q_adv_w", 0.0)),
            }
        )
        return values

    def _current_derivatives(self) -> dict[str, float]:
        try:
            self.last_feed_base_kg_h = self._baseline_feed_kg_h()
            self.last_derivatives = dynamics.derivatives(self.state.as_dict(), self.params)
        except Exception:
            pass
        return self.last_derivatives

    def sensor_reading(self, sensor_name: str) -> SensorReading:
        name = sensor_name if sensor_name in DEFAULT_SENSOR_NAMES else "mixed_tank_outlet"
        factors = self._sensor_factors(name)
        snap = self.state.as_dict()
        temp = snap["temperature_c"] + factors.get("temperature_offset_c", 0.0)
        tan = max(0.0, snap["tan_mg_l"] * factors.get("tan", 1.0))
        do = max(0.0, snap["dissolved_oxygen_mg_l"] * factors.get("dissolved_oxygen", factors.get("do", 1.0)))
        co2 = max(0.0, snap["co2_mg_l"] * factors.get("co2", 1.0))
        alk = max(0.0, snap["alkalinity_mg_l_as_caco3"] * factors.get("alkalinity", 1.0))
        salinity = max(0.0, snap["salinity_ppt"] * factors.get("salinity", 1.0))
        turbidity = max(0.0, snap["turbidity_ntu"] * factors.get("turbidity", 1.0))
        if name == "inlet_reference":
            temp = float(self.params.get("inlet_temp_c", temp))
            tan = max(0.0, float(self.params.get("tan_in_mg_l", 0.0)))
            do = max(0.0, float(self.params.get("do_in", do)))
            co2 = max(0.0, float(self.params.get("co2_eq", co2)))
            alk = max(0.0, float(self.params.get("alk_in", alk)))
            salinity = max(0.0, float(self.params.get("salinity_in_ppt", salinity)))
            turbidity = max(0.0, float(self.params.get("turbidity_in_ntu", turbidity)))
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
                "salinity_ppt": salinity,
                "turbidity_ntu": turbidity,
                "ph": ph,
                "nh3_mg_l": tan * dynamics.nh3_fraction(temp, ph),
                "nitrite_mg_l": 0.0,
                "nitrate_mg_l": 0.0,
            },
        )

    def register_particles(
        self,
        positions: list[Any],
        heat_weights: list[float] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        del tags
        arr = np.asarray(
            [[float(pos[0]), float(pos[1]), float(pos[2])] for pos in positions],
            dtype=np.float64,
        )
        if arr.size == 0:
            arr = np.empty((0, 3), dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != 3:
            raise ValueError("positions must be an Nx3 array")
        graph_hash = self._particle_hash(arr)
        existing = self._particle_field
        if existing is not None and existing.graph_hash == graph_hash:
            existing.temperatures += self.state.temperature_c - float(np.mean(existing.temperatures))
            return {"status": "ok", "count": int(len(arr)), "graph_hash": graph_hash, "reused": True}

        graph = thermal_dynamics.build_knn_graph(arr, int(self.params.get("particle_knn_k", 8)))
        weights = self._heat_weights_from_positions(arr, heat_weights)
        self._particle_field = ParticleField(
            positions=arr,
            graph=graph,
            temperatures=np.full(len(arr), self.state.temperature_c, dtype=np.float64),
            heat_weights=weights,
            graph_hash=graph_hash,
        )
        self._particle_feature_cache = {}
        return {"status": "ok", "count": int(len(arr)), "graph_hash": graph_hash, "reused": False}

    def registered_particle_values(self) -> dict[str, list[float]]:
        field = self._particle_field
        if field is None:
            return {}
        values = self.particle_values(field.heat_weights.tolist(), field.positions.tolist())
        values["temperature"] = field.temperatures.astype(float).tolist()
        return values

    def particle_values(self, heat_weights: list[float], positions: list[Any] | None = None) -> dict[str, list[float]]:
        count = len(heat_weights)
        if count <= 0 and positions is not None:
            count = len(positions)
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
        turbidity_weight = np.clip(0.55 * weights + 0.45 * (1.0 - y_norm), 0.0, 1.0)
        salinity_weight = np.clip(0.55 * radial_norm + 0.45 * y_norm, 0.0, 1.0)

        temperature = state.temperature_c + 0.15 * weights
        tan = np.maximum(0.0, state.tan_mg_l * (0.78 + 0.44 * tan_weight))
        co2 = np.maximum(0.0, state.co2_mg_l * (0.82 + 0.36 * co2_weight))
        do = np.maximum(0.0, state.dissolved_oxygen_mg_l * (0.92 + 0.16 * do_weight))
        alk = np.maximum(0.0, state.alkalinity_mg_l_as_caco3 * (1.0 - 0.025 * ph_weight))
        salinity = np.maximum(0.0, state.salinity_ppt * (0.99 + 0.02 * salinity_weight))
        turbidity = np.maximum(0.0, state.turbidity_ntu * (0.75 + 0.55 * turbidity_weight))
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
            "salinity": salinity.tolist(),
            "turbidity": turbidity.tolist(),
            "ph": ph.tolist(),
            "nh3": nh3.tolist(),
        }

    def _particle_features(self, heat_weights: list[float], positions: list[Any] | None, count: int) -> dict[str, np.ndarray]:
        cache = self._particle_feature_cache
        cache_key = (count, id(heat_weights), id(positions))
        if cache.get("key") == cache_key:
            return cache["features"]

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
            radial_norm = np.zeros(count, dtype=np.float64)
        weights = np.clip(np.asarray(heat_weights, dtype=np.float64), 0.0, 1.0)
        if len(weights) != count:
            weights = radial_norm.copy()

        features = {"weights": weights, "y_norm": y_norm, "radial_norm": radial_norm}
        self._particle_feature_cache = {"key": cache_key, "features": features}
        return features

    def _advance_one_substep(self, dt_h: float, external_temperature_c: float | None) -> None:
        dt_s = max(0.0, float(dt_h)) * 3600.0
        if external_temperature_c is None:
            self.state.temperature_c = thermal_dynamics.step_temperature_rk4(
                self.state.temperature_c,
                dt_s,
                self.params,
                inflow_enabled=bool(self.params.get("inflow_enabled", True)),
            )
        else:
            self.state.temperature_c = float(external_temperature_c)
        self._advance_particle_field(dt_s)

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
        self.state.salinity_ppt = max(0.0, self.state.salinity_ppt + deriv["salinity_ppt"] * dt_h)
        self.state.turbidity_ntu = max(0.0, self.state.turbidity_ntu + deriv["turbidity_ntu"] * dt_h)

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

    def _advance_particle_field(self, dt_s: float) -> None:
        field = self._particle_field
        if field is None or len(field.temperatures) == 0:
            return
        source = self._particle_source_c_s(field)
        field.temperatures = thermal_dynamics.diffuse_step(
            field.temperatures,
            bulk_temp_c=self.state.temperature_c,
            graph=field.graph,
            dt_s=dt_s,
            diffusion_d=float(self.params.get("particle_diffusion_d", 0.015)),
            bulk_relax_lambda=float(self.params.get("particle_bulk_relax_lambda", 0.0015)),
            source_c_s=source,
        )

    def _particle_source_c_s(self, field: ParticleField) -> np.ndarray:
        positions = field.positions
        temps = field.temperatures
        count = len(temps)
        if count <= 0:
            return np.empty((0,), dtype=np.float64)

        y = positions[:, 1]
        radial = np.hypot(positions[:, 0] - np.mean(positions[:, 0]), positions[:, 2] - np.mean(positions[:, 2]))
        radial_norm = radial / max(1e-9, float(np.max(radial)))
        source = np.zeros(count, dtype=np.float64)

        wall_start = float(self.params.get("particle_wall_band_start", 0.85))
        wall_mask = np.clip((radial_norm - wall_start) / max(1e-9, 1.0 - wall_start), 0.0, 1.0)
        wall_lambda = max(0.0, float(self.params.get("particle_wall_source_lambda", 0.00025)))
        room_temp = float(self.params.get("room_temp_c", self.params.get("air_temp_c", 22.0)))
        source += wall_lambda * wall_mask * (room_temp - temps)

        inlet_location = self._tank_local_to_cloud_world(self.params.get("inlet_location", [-1.1, 1.1, 0.0]), positions)
        inlet_radius = max(1e-6, float(self.params.get("particle_inlet_radius_m", 0.35)))
        inlet_mask = self._gaussian_mask(positions, inlet_location, inlet_radius)
        inlet_lambda = max(0.0, float(self.params.get("particle_inlet_source_lambda", 0.0012)))
        inlet_temp = float(self.params.get("inlet_temp_c", 12.0))
        if bool(self.params.get("inflow_enabled", True)):
            source += inlet_lambda * inlet_mask * (inlet_temp - temps)

        heater_power = float(self.params.get("heater_power_w", self.params.get("heater_power", 0.0)))
        if heater_power > 0.0:
            heater_location = self._tank_local_to_cloud_world(self.params.get("heater_location", [1.0, 0.35, 0.0]), positions)
            heater_radius = max(1e-6, float(self.params.get("particle_heater_radius_m", 0.4)))
            heater_mask = self._gaussian_mask(positions, heater_location, heater_radius)
            heat = thermal_dynamics.net_heat_w(self.state.temperature_c, self.params)
            heat_rate_c_s = heater_power / max(1e-9, float(heat.get("heat_capacity_j_k", 1.0)))
            source += heat_rate_c_s * heater_mask / max(1e-9, float(np.mean(heater_mask)))
        return source

    def _heat_weights_from_positions(self, positions: np.ndarray, heat_weights: list[float] | None) -> np.ndarray:
        if heat_weights is not None and len(heat_weights) == len(positions):
            return np.clip(np.asarray(heat_weights, dtype=np.float64), 0.0, 1.0)
        radial = np.hypot(positions[:, 0] - np.mean(positions[:, 0]), positions[:, 2] - np.mean(positions[:, 2]))
        return np.clip(radial / max(1e-9, float(np.max(radial))), 0.0, 1.0)

    def _tank_local_to_cloud_world(self, local: Any, positions: np.ndarray) -> np.ndarray:
        values = np.asarray(local, dtype=np.float64)
        if values.shape != (3,):
            values = np.zeros(3, dtype=np.float64)
        center = np.array([np.mean(positions[:, 0]), np.min(positions[:, 1]), np.mean(positions[:, 2])], dtype=np.float64)
        return center + values

    def _gaussian_mask(self, positions: np.ndarray, center: np.ndarray, radius: float) -> np.ndarray:
        dist2 = np.sum((positions - center[None, :]) ** 2, axis=1)
        return np.exp(-dist2 / (2.0 * radius * radius))

    def _particle_hash(self, positions: np.ndarray) -> str:
        rounded = np.round(np.asarray(positions, dtype=np.float64), decimals=5)
        return hashlib.sha1(rounded.tobytes()).hexdigest()[:16]

    def _drain_actions(self) -> None:
        if not self._action_queue:
            return
        actions = self._action_queue
        self._action_queue = []
        for action in actions:
            self._apply_action_now(action)

    def _apply_action_now(self, action: Mapping[str, Any]) -> None:
        kind = str(action.get("type", action.get("action", ""))).strip().lower()
        if kind in {"feed", "apply_feed", "feed_pulse"}:
            self.state.feed_pool_kg = max(0.0, self.state.feed_pool_kg + max(0.0, float(action.get("mass_kg", action.get("kg", 0.0)))))
        elif kind in {"set_water_exchange", "set_flow_rate"}:
            q_lph = max(0.0, float(action.get("q_lph", action.get("flow_lph", 0.0))))
            self.params["flow_lph"] = q_lph
            self.params["q_makeup_lph"] = q_lph
        elif kind == "set_inflow":
            self.params["inflow_enabled"] = bool(action.get("enabled", True))
        elif kind == "set_temperature":
            self.state.temperature_c = float(action.get("temperature_c", action.get("target_temperature_c", self.state.temperature_c)))
            if self._particle_field is not None and len(self._particle_field.temperatures) > 0:
                self._particle_field.temperatures += self.state.temperature_c - float(np.mean(self._particle_field.temperatures))
        elif kind == "set_heater":
            power = float(action.get("power_w", action.get("power", 0.0)))
            self.params["heater_power_w"] = power
            self.params["heater_power"] = power
        elif kind in {"set_inlet_temperature", "set_inlet_temp"}:
            self.params["inlet_temp_c"] = float(action.get("temperature_c", action.get("inlet_temp_c", self.params.get("inlet_temp_c", 12.0))))
        elif kind in {"set_biofilter", "toggle_biofilter"}:
            self.params["biofilter_on"] = bool(action.get("enabled", True))
        elif kind == "set_stock":
            self.params["fish_count"] = max(0.0, float(action.get("fish_count", action.get("n", 0.0))))
            self.params["fish_weight_kg"] = max(1e-9, float(action.get("fish_weight_kg", action.get("w_kg", 1.0))))
        elif kind in {"set_inlet_salinity", "set_salinity_in"}:
            self.params["salinity_in_ppt"] = max(0.0, float(action.get("salinity_ppt", action.get("ppt", 0.0))))
        elif kind in {"set_inlet_turbidity", "set_turbidity_in"}:
            self.params["turbidity_in_ntu"] = max(0.0, float(action.get("turbidity_ntu", action.get("ntu", 0.0))))
        elif kind == "set_inlet_do":
            self.params["do_in"] = max(0.0, float(action.get("dissolved_oxygen_mg_l", action.get("do_mg_l", 0.0))))
        elif kind == "set_inlet_alkalinity":
            self.params["alk_in"] = max(0.0, float(action.get("alkalinity_mg_l_as_caco3", action.get("alk_mg_l", 0.0))))
        elif kind == "set_inlet_tan":
            self.params["tan_in_mg_l"] = max(0.0, float(action.get("tan_mg_l", 0.0)))
        elif kind in {"set_aeration", "set_kla_o2"}:
            self.params["kla_o2_h"] = max(0.0, float(action.get("kla_o2_h", action.get("value", 0.0))))
        elif kind in {"set_co2_stripping", "set_kla_co2"}:
            self.params["kla_co2_h"] = max(0.0, float(action.get("kla_co2_h", action.get("value", 0.0))))
        elif kind == "set_biofilter_capacity":
            self.params["vtr_max_mg_l_h"] = max(0.0, float(action.get("vtr_max_mg_l_h", action.get("value", 0.0))))
        elif kind == "set_nitrification_rate":
            self.params["k_nitrif_h"] = max(0.0, float(action.get("k_nitrif_h", action.get("value", 0.0))))
        elif kind in {"set_solids_removal", "set_mechanical_filter"}:
            if "enabled" in action:
                self.params["turbidity_settle_h"] = float(action.get("settle_h", 0.35)) if bool(action.get("enabled")) else 0.0
            else:
                self.params["turbidity_settle_h"] = max(0.0, float(action.get("turbidity_settle_h", action.get("settle_h", 0.0))))
        elif kind == "dose_alkalinity":
            self.state.alkalinity_mg_l_as_caco3 = max(0.0, self.state.alkalinity_mg_l_as_caco3 + float(action.get("mg_l_as_caco3", action.get("delta_mg_l", 0.0))))
        elif kind == "dose_salt":
            self.state.salinity_ppt = max(0.0, self.state.salinity_ppt + float(action.get("ppt", action.get("delta_ppt", 0.0))))
        elif kind == "add_turbidity":
            self.state.turbidity_ntu = max(0.0, self.state.turbidity_ntu + float(action.get("ntu", action.get("delta_ntu", 0.0))))
        elif kind == "oxygen_boost":
            self.state.dissolved_oxygen_mg_l = max(0.0, self.state.dissolved_oxygen_mg_l + float(action.get("mg_l", action.get("delta_mg_l", 0.0))))
        elif kind == "co2_pulse":
            self.state.co2_mg_l = max(0.0, self.state.co2_mg_l + float(action.get("mg_l", action.get("delta_mg_l", 0.0))))
        elif kind == "load_scenario":
            if not self.load_scenario(str(action.get("name", "baseline"))):
                raise ValueError(f"unknown scenario: {action.get('name')}")
        else:
            raise ValueError(f"unknown action type: {kind}")

    def _state_from_initial(self, initial: Mapping[str, Any]) -> WaterQualityState:
        return WaterQualityState(
            temperature_c=float(initial.get("temperature_c", initial.get("temperature", 10.5))),
            dissolved_oxygen_mg_l=float(initial.get("dissolved_oxygen_mg_l", initial.get("do_mg_l", 9.0))),
            tan_mg_l=float(initial.get("tan_mg_l", 0.3)),
            co2_mg_l=float(initial.get("co2_mg_l", 5.0)),
            alkalinity_mg_l_as_caco3=float(initial.get("alkalinity_mg_l_as_caco3", initial.get("alk_mg_l", 120.0))),
            salinity_ppt=float(initial.get("salinity_ppt", initial.get("salinity", 0.2))),
            turbidity_ntu=float(initial.get("turbidity_ntu", initial.get("turbidity", 2.0))),
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
            "nitrif_theta": 1.07,
            "nitrif_t_ref_c": 20.0,
            "nitrif_k_o2_mg_l": 1.0,
            "tau_feed_h": 4.0,
            "do_maxFI": 7.0,
            "do_zero": 3.0,
            "do_in": 9.0,
            "tan_in_mg_l": 0.0,
            "co2_eq": 0.5,
            "alk_in": 120.0,
            "salinity_in_ppt": 0.2,
            "turbidity_in_ntu": 1.0,
            "biofilter_on": True,
            "inflow_enabled": True,
            "time_scale": 1.0,
            "substep_h": 0.0167,
            "tan_per_feed": 0.092,
            "co2_per_o2": 1.375,
            "alk_per_tan": 7.14,
            "o2_per_tan": 4.57,
            "o2_per_feed": 0.225,
            "solids_per_feed": 0.275,
            "fish_tan_mg_kg_h": 2.5,
            "fish_tss_mg_kg_h": 1.5,
            "turbidity_ntu_per_mg_l_tss": 0.35,
            "turbidity_settle_h": 0.35,
            "mo2_a": 83.0,
            "mo2_w_exp": -0.14,
            "mo2_q10": 2.5,
            "mo2_t_ref": 10.0,
            "pk1": 6.35,
            "tank_radius_m": 1.2,
            "tank_water_height_m": 2.21,
            "water_density": 998.0,
            "water_cp": 4186.0,
            "u_wall_w_m2k": 5.0,
            "emissivity": 0.96,
            "air_temp_c": 22.0,
            "rel_humidity": 0.60,
            "air_speed_ms": 0.2,
            "evap_a_w_m2_kpa": 18.0,
            "evap_b_w_m2_kpa_per_ms": 12.0,
            "bowen_gamma_kpa_k": 0.066,
            "q_makeup_lph": 220.0,
            "inlet_temp_c": 12.0,
            "room_temp_c": 22.0,
            "heater_power_w": 0.0,
            "particle_diffusion_d": 0.015,
            "particle_knn_k": 8,
            "particle_bulk_relax_lambda": 0.0015,
            "particle_wall_band_start": 0.85,
            "particle_wall_source_lambda": 0.00025,
            "particle_inlet_source_lambda": 0.0012,
            "particle_inlet_radius_m": 0.35,
            "particle_heater_radius_m": 0.4,
            "inlet_location": [-1.1, 1.1, 0.0],
            "heater_location": [1.0, 0.35, 0.0],
        }

    def _sensor_factors(self, sensor_name: str) -> dict[str, float]:
        return {
            "inlet_reference": {"tan": 0.72, "dissolved_oxygen": 1.08, "co2": 0.72, "temperature_offset_c": -0.12, "turbidity": 0.72},
            "feed_zone_tan": {"tan": 1.45, "dissolved_oxygen": 0.90, "co2": 1.18, "turbidity": 1.28, "ph_offset": -0.03},
            "fish_core_do": {"tan": 1.05, "dissolved_oxygen": 0.84, "co2": 1.16, "turbidity": 1.08, "ph_offset": -0.02},
            "bottom_co2": {"tan": 1.08, "dissolved_oxygen": 0.88, "co2": 1.36, "turbidity": 1.18, "ph_offset": -0.06},
            "biofilter_sentinel": {"tan": 0.62, "dissolved_oxygen": 0.96, "co2": 1.02, "turbidity": 0.82},
            "mixed_tank_outlet": {},
        }.get(sensor_name, {})


def _read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _coerce_env_value(raw: str) -> Any:
    value = raw.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    try:
        return float(value)
    except ValueError:
        return value


def _apply_env_overrides(constants: dict[str, Any]) -> dict[str, Any]:
    for key in list(constants.keys()) + list(WaterQualityModel()._default_params().keys()):
        env_name = f"AQUACAST_WQ_{key.upper()}"
        if env_name in os.environ:
            constants[key] = _coerce_env_value(os.environ[env_name])
    return constants


def load_model(constants_path: str | Path, feed_rate_path: str | Path, scenarios_path: str | Path, scenario_name: str) -> WaterQualityModel:
    constants = _apply_env_overrides(_read_json(constants_path))
    feed_rate = _read_json(feed_rate_path)
    scenarios = _read_json(scenarios_path)
    scenario = copy.deepcopy(scenarios.get(scenario_name) or scenarios.get("baseline") or {})
    scenario.setdefault("name", scenario_name)
    return WaterQualityModel(constants, feed_rate, scenario, scenarios)
