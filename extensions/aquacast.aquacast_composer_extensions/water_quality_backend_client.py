"""HTTP client for the Aquacast local water-quality backend."""

from __future__ import annotations

from dataclasses import dataclass
import json
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np


@dataclass(frozen=True)
class BackendSensorReading:
    values: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


class WaterQualityBackendClient:
    def __init__(self, base_url: str, *, timeout_s: float = 0.25):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self._snapshot: dict[str, Any] = {}
        self._particle_feature_cache: dict[str, Any] = {}

    @property
    def state(self) -> SimpleNamespace:
        return SimpleNamespace(**self._snapshot)

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def advance(self, real_dt_s: float, *, temperature_c: float | None = None) -> SimpleNamespace:
        payload: dict[str, Any] = {"real_dt_s": float(real_dt_s)}
        if temperature_c is not None:
            payload["temperature_c"] = float(temperature_c)
        self._snapshot = self._post("/advance", payload)
        return self.state

    def step(self, dt_s: float, *, temperature_c: float | None = None, inflow_enabled: bool | None = None) -> SimpleNamespace:
        if inflow_enabled is not None:
            self.set_inflow(inflow_enabled)
        return self.advance(dt_s, temperature_c=temperature_c)

    def snapshot(self) -> dict[str, Any]:
        self._snapshot = self._get("/snapshot")
        return dict(self._snapshot)

    def reset(self, scenario_name: str | None = None) -> dict[str, Any]:
        payload = {}
        if scenario_name:
            payload["scenario_name"] = scenario_name
        self._snapshot = self._post("/reset", payload)
        self._particle_feature_cache = {}
        return dict(self._snapshot)

    def sensor_reading(self, sensor_name: str) -> BackendSensorReading:
        payload = self._get("/sensor", {"name": sensor_name})
        return BackendSensorReading(payload)

    def particle_values(self, heat_weights: list[float], positions: list[Any] | None = None) -> dict[str, list[float]]:
        try:
            payload = self._get("/particles/values")
            values = payload.get("values", {})
            if values:
                return values
        except Exception:
            pass

        count = len(heat_weights)
        if count <= 0:
            return {}
        if not self._snapshot:
            self.snapshot()

        features = self._particle_features(heat_weights, positions, count)
        weights = features["weights"]
        y_norm = features["y_norm"]
        radial_norm = features["radial_norm"]

        tan_weight = np.clip(0.45 * weights + 0.55 * (1.0 - y_norm), 0.0, 1.0)
        co2_weight = np.clip(0.55 * (1.0 - y_norm) + 0.45 * radial_norm, 0.0, 1.0)
        do_weight = np.clip(0.45 * y_norm + 0.55 * (1.0 - weights), 0.0, 1.0)
        ph_weight = co2_weight

        temperature = self._snapshot_float("temperature_c", 14.0) + 0.15 * weights
        tan = np.maximum(0.0, self._snapshot_float("tan_mg_l", 0.0) * (0.78 + 0.44 * tan_weight))
        co2 = np.maximum(0.0, self._snapshot_float("co2_mg_l", 0.0) * (0.82 + 0.36 * co2_weight))
        do = np.maximum(0.0, self._snapshot_float("dissolved_oxygen_mg_l", 9.0) * (0.92 + 0.16 * do_weight))
        alk = np.maximum(0.0, self._snapshot_float("alkalinity_mg_l_as_caco3", 120.0) * (1.0 - 0.025 * ph_weight))
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

    def register_particles(
        self,
        positions: list[Any],
        heat_weights: list[float] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "positions": [
                [float(pos[0]), float(pos[1]), float(pos[2])]
                for pos in positions
            ],
            "count": len(positions),
        }
        if heat_weights is not None:
            payload["heat_weights"] = [float(value) for value in heat_weights]
        if tags is not None:
            payload["tags"] = list(tags)
        return self._post("/particles/register", payload)

    def apply_feed(self, mass_kg: float) -> None:
        self._action({"type": "feed", "mass_kg": float(mass_kg)})

    def set_water_exchange(self, q_lph: float) -> None:
        self._action({"type": "set_water_exchange", "q_lph": float(q_lph)})

    def set_inflow(self, enabled: bool) -> None:
        self._action({"type": "set_inflow", "enabled": bool(enabled)})

    def set_heater(self, power: float) -> None:
        self._action({"type": "set_heater", "power": float(power)})

    def set_biofilter(self, enabled: bool) -> None:
        self._action({"type": "set_biofilter", "enabled": bool(enabled)})

    def set_stock(self, n: float, w_kg: float) -> None:
        self._action({"type": "set_stock", "fish_count": float(n), "fish_weight_kg": float(w_kg)})

    def load_scenario(self, name: str) -> bool:
        result = self._action({"type": "load_scenario", "name": str(name)})
        return result.get("status") == "ok"

    def _action(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._snapshot = self._post("/action", payload)
        return dict(self._snapshot)

    def _get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        try:
            with urlopen(url, timeout=self.timeout_s) as response:
                return self._decode_response(response.read())
        except URLError as exc:
            raise RuntimeError(f"water-quality backend unavailable: {exc}") from exc

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                return self._decode_response(response.read())
        except URLError as exc:
            raise RuntimeError(f"water-quality backend unavailable: {exc}") from exc

    def _decode_response(self, body: bytes) -> dict[str, Any]:
        payload = json.loads(body.decode("utf-8") or "{}")
        if payload.get("status") == "error":
            raise RuntimeError(str(payload.get("error", "unknown backend error")))
        return payload

    def _snapshot_float(self, key: str, default: float) -> float:
        try:
            return float(self._snapshot.get(key, default))
        except Exception:
            return float(default)

    def _particle_features(self, heat_weights: list[float], positions: list[Any] | None, count: int) -> dict[str, np.ndarray]:
        cache_key = (count, id(heat_weights), id(positions))
        if self._particle_feature_cache.get("key") == cache_key:
            return self._particle_feature_cache["features"]

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
