"""Local HTTP backend for Aquacast water-quality computation.

This process owns the deterministic water-quality model and exposes a small
JSON API. It intentionally does not import Omniverse or touch USD; Kit remains
responsible for rendering and stage writes.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = Path(__file__).resolve().parent
EXT_ROOT = ROOT / "extensions" / "aquacast.aquacast_composer_extensions"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(EXT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXT_ROOT))

from water_quality_model import DEFAULT_SENSOR_NAMES, load_model  # noqa: E402
import water_quality_bands  # noqa: E402
from kafka_publisher import KafkaPublisher  # noqa: E402
import aquacast_db  # noqa: E402


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_THRESHOLDS = water_quality_bands.DEFAULT_WQ_METRIC_BANDS


class WaterQualityBackend:
    def __init__(
        self,
        *,
        constants_path: Path,
        feed_rate_path: Path,
        scenarios_path: Path,
        scenario_name: str,
        thresholds_path: Path | None = None,
    ):
        self.constants_path = constants_path
        self.feed_rate_path = feed_rate_path
        self.scenarios_path = scenarios_path
        self.scenario_name = scenario_name
        self._lock = threading.RLock()
        self.model = load_model(constants_path, feed_rate_path, scenarios_path, scenario_name)
        self._tank_models: dict[str, Any] = {}
        self._tank_ids: dict[str, str] = {}
        self.thresholds_path = Path(
            thresholds_path
            or os.environ.get("AQUACAST_WQ_THRESHOLDS_PATH", str(BACKEND_ROOT / "wq_metric_thresholds.json"))
        )
        self.kafka = KafkaPublisher()
        self.kafka.start()

    def reset(self, scenario_name: str | None = None) -> dict[str, Any]:
        with self._lock:
            name = scenario_name or self.scenario_name
            self.model = self._new_model(name)
            self._tank_models.clear()
            self._tank_ids.clear()
            self.scenario_name = name
            snap = self.snapshot()
            self.kafka.publish_state(self)
            return snap

    def snapshot(self, tank_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            model = self._model_for_tank(tank_key, create=bool(tank_key))
            snap = self._snapshot_for_model(model, tank_key)
            if not tank_key and self._tank_models:
                snap["tank_snapshots"] = self._tank_snapshots()
            return snap

    def thresholds(self) -> dict[str, Any]:
        with self._lock:
            return {"status": "ok", "thresholds": self._read_thresholds()}

    def set_thresholds(self, thresholds: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            values = self._normalize_thresholds(thresholds)
            self.thresholds_path.parent.mkdir(parents=True, exist_ok=True)
            self.thresholds_path.write_text(
                json.dumps(values, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return {"status": "ok", "thresholds": values}

    def advance(self, real_dt_s: float, temperature_c: float | None = None) -> dict[str, Any]:
        with self._lock:
            self.model.advance(real_dt_s, temperature_c=temperature_c)
            for model in self._tank_models.values():
                model.advance(real_dt_s, temperature_c=None)
            snap = self.snapshot()
            self.kafka.publish_state(self)
            return snap

    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("type", payload.get("action", ""))).strip().lower()
        with self._lock:
            if kind == "reset":
                tank_key = self._tank_key(payload)
                if tank_key:
                    model = self._new_model(str(payload.get("scenario_name") or payload.get("name") or self.scenario_name))
                    self._tank_models[tank_key] = model
                    result = self._snapshot_for_model(model, tank_key)
                    self.kafka.publish_state(self)
                    return result
                return self.reset(str(payload.get("scenario_name") or payload.get("name") or self.scenario_name))
            tank_key = self._tank_key(payload)
            model = self._model_for_tank(tank_key, create=bool(tank_key))
            result = model.apply_control(payload)
            if tank_key:
                result["tank_path"] = tank_key
                result["tank_id"] = self._tank_id(tank_key)
            self.kafka.publish_state(self)
            return result

    def sensor(self, sensor_name: str, tank_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            name = sensor_name or "mixed_tank_outlet"
            model = self._model_for_tank(tank_key, create=bool(tank_key))
            reading = model.sensor_reading(name).as_dict()
            reading["status"] = "ok"
            self._attach_tank_fields(reading, tank_key)
            self._attach_actuator_fields(reading, model)
            return reading

    def all_sensors(self, tank_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            if tank_key:
                model = self._model_for_tank(tank_key, create=True)
                return {
                    "status": "ok",
                    "readings": self._sensor_readings(model, tank_key),
                }
            readings = self._sensor_readings(self.model, None)
            for key, model in self._tank_models.items():
                readings.extend(self._sensor_readings(model, key))
            return {
                "status": "ok",
                "readings": readings,
            }

    def particle_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        heat_weights = payload.get("heat_weights") or []
        positions = payload.get("positions")
        with self._lock:
            tank_key = self._tank_key(payload)
            model = self._model_for_tank(tank_key, create=bool(tank_key))
            return {
                "status": "ok",
                "values": model.particle_values(heat_weights, positions),
            }

    def register_particles(self, payload: dict[str, Any]) -> dict[str, Any]:
        positions = payload.get("positions") or []
        heat_weights = payload.get("heat_weights")
        tags = payload.get("tags")
        with self._lock:
            tank_key = self._tank_key(payload)
            result = self._model_for_tank(tank_key, create=bool(tank_key)).register_particles(positions, heat_weights, tags)
            self._attach_tank_fields(result, tank_key)
            return result

    def registered_particle_values(self, tank_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "values": self._model_for_tank(tank_key, create=bool(tank_key)).registered_particle_values(),
            }

    def _new_model(self, scenario_name: str | None = None):
        return load_model(
            self.constants_path,
            self.feed_rate_path,
            self.scenarios_path,
            scenario_name or self.scenario_name,
        )

    def _tank_key(self, payload: dict[str, Any] | None) -> str:
        payload = payload or {}
        return str(payload.get("tank_path") or payload.get("tank_id") or "").strip()

    def _model_for_tank(self, tank_key: str | None, *, create: bool):
        key = str(tank_key or "").strip()
        if not key:
            return self.model
        if key not in self._tank_models:
            if not create:
                return self.model
            self._tank_models[key] = self._new_model(self.scenario_name)
            self._tank_ids[key] = self._derive_tank_id(key)
        return self._tank_models[key]

    def _snapshot_for_model(self, model: Any, tank_key: str | None) -> dict[str, Any]:
        snap = dict(model.snapshot())
        snap["status"] = "ok"
        snap["backend"] = "aquacast-water-quality"
        self._attach_tank_fields(snap, tank_key)
        return snap

    def _tank_snapshots(self) -> dict[str, Any]:
        return {
            key: self._snapshot_for_model(model, key)
            for key, model in sorted(self._tank_models.items())
        }

    def _sensor_readings(self, model: Any, tank_key: str | None) -> list[dict[str, Any]]:
        readings = []
        for name in DEFAULT_SENSOR_NAMES:
            reading = model.sensor_reading(name).as_dict()
            self._attach_tank_fields(reading, tank_key)
            self._attach_actuator_fields(reading, model)
            readings.append(reading)
        return readings

    def _attach_actuator_fields(self, payload: dict[str, Any], model: Any) -> None:
        snap = model.snapshot()
        for key in (
            "sim_time_h",
            "inflow_enabled",
            "inlet_enabled",
            "outlet_enabled",
            "biofilter_on",
            "mechanical_filter_on",
            "heater_on",
            "flow_lph",
            "q_makeup_lph",
            "heater_power_w",
            "turbidity_settle_h",
        ):
            if key in snap:
                payload[key] = snap[key]

    def _attach_tank_fields(self, payload: dict[str, Any], tank_key: str | None) -> None:
        key = str(tank_key or "").strip()
        if not key:
            return
        payload["tank_path"] = key
        payload["tank_id"] = self._tank_id(key)

    def _tank_id(self, tank_key: str) -> str:
        key = str(tank_key or "").strip()
        if key not in self._tank_ids:
            self._tank_ids[key] = self._derive_tank_id(key)
        return self._tank_ids[key]

    def _derive_tank_id(self, tank_key: str) -> str:
        parts = [part for part in str(tank_key).strip("/").split("/") if part]
        if parts and parts[-1] == "Water":
            parts = parts[:-1]
        generic = {"Root", "scene", "Meshes", "Model", "Components", "Component", "Water"}
        label = "tank"
        for part in reversed(parts):
            if part in generic:
                continue
            if part.startswith("Group") and part[5:].isdigit():
                continue
            label = part
            break
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "tank"

    def _read_thresholds(self) -> dict[str, Any]:
        if self.thresholds_path.exists():
            try:
                return self._normalize_thresholds(json.loads(self.thresholds_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return self._normalize_thresholds(DEFAULT_THRESHOLDS)

    def _normalize_thresholds(self, thresholds: dict[str, Any] | None) -> dict[str, Any]:
        return water_quality_bands.normalize_bands(thresholds or DEFAULT_THRESHOLDS)


class RequestHandler(BaseHTTPRequestHandler):
    backend: WaterQualityBackend

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        tank_key = self._query_value(query, "tank_path") or self._query_value(query, "tank_id")
        try:
            if parsed.path == "/health":
                self._write_json({"status": "ok", "service": "aquacast-water-quality"})
            elif parsed.path == "/thresholds":
                self._write_json(self.backend.thresholds())
            elif parsed.path == "/snapshot":
                self._write_json(self.backend.snapshot(tank_key))
            elif parsed.path == "/sensor":
                self._write_json(self.backend.sensor(self._query_value(query, "name") or "mixed_tank_outlet", tank_key))
            elif parsed.path == "/sensors":
                self._write_json(self.backend.all_sensors(tank_key))
            elif parsed.path == "/particles/values":
                self._write_json(self.backend.registered_particle_values(tank_key))
            else:
                self._write_json({"status": "error", "error": "not found"}, status=404)
        except Exception as exc:
            self._write_json({"status": "error", "error": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/advance":
                self._write_json(
                    self.backend.advance(
                        float(payload.get("real_dt_s", 0.0)),
                        temperature_c=payload.get("temperature_c"),
                    )
                )
            elif parsed.path == "/action":
                self._write_json(self.backend.action(payload))
            elif parsed.path == "/thresholds":
                self._write_json(self.backend.set_thresholds(payload.get("thresholds", payload)))
            elif parsed.path == "/reset":
                if payload.get("tank_path") or payload.get("tank_id"):
                    self._write_json(self.backend.action({"type": "reset", **payload}))
                else:
                    self._write_json(self.backend.reset(payload.get("scenario_name") or payload.get("name")))
            elif parsed.path == "/particle-values":
                self._write_json(self.backend.particle_values(payload))
            elif parsed.path == "/particles/register":
                self._write_json(self.backend.register_particles(payload))
            else:
                self._write_json({"status": "error", "error": "not found"}, status=404)
        except ValueError as exc:
            self._write_json({"status": "error", "error": str(exc)}, status=400)
        except Exception as exc:
            self._write_json({"status": "error", "error": str(exc)}, status=500)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._write_headers()
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("AQUACAST_BACKEND_ACCESS_LOG", "0") == "1":
            super().log_message(fmt, *args)

    def _query_value(self, query: dict[str, list[str]], name: str) -> str:
        values = query.get(name) or []
        if not values:
            return ""
        return str(values[0]).strip()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _write_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._write_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_headers(self) -> None:
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


def _path_from_env(name: str, default: Path) -> Path:
    value = Path(os.environ.get(name, str(default)))
    if not value.is_absolute():
        value = ROOT / value
    return value.resolve()


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_server(host: str, port: int, scenario_name: str) -> ThreadingHTTPServer:
    data_dir = EXT_ROOT / "data"
    backend = WaterQualityBackend(
        constants_path=_path_from_env("AQUACAST_WQ_CONSTANTS", data_dir / "wq_constants.json"),
        feed_rate_path=_path_from_env("AQUACAST_WQ_FEED_RATE", data_dir / "wq_feed_rate.json"),
        scenarios_path=_path_from_env("AQUACAST_WQ_SCENARIOS", data_dir / "wq_scenarios.json"),
        scenario_name=scenario_name,
    )
    RequestHandler.backend = backend
    return ThreadingHTTPServer((host, port), RequestHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Aquacast local water-quality computation backend")
    parser.add_argument("--env-file", default=os.environ.get("AQUACAST_BACKEND_ENV_FILE", str(Path(__file__).with_name("aquacast-backend.env"))))
    parser.add_argument("--host", default=os.environ.get("AQUACAST_BACKEND_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AQUACAST_BACKEND_PORT", DEFAULT_PORT)))
    parser.add_argument("--scenario", default=os.environ.get("AQUACAST_WQ_SCENARIO", "baseline"))
    parser.add_argument("--db-path", default=str(aquacast_db.db_path_from_env()))
    parser.add_argument("--db-drop", action="store_true", help="DROP Aquacast SQLite tables before creating them")
    args = parser.parse_args()

    _load_env_file(Path(args.env_file))
    host = os.environ.get("AQUACAST_BACKEND_HOST", args.host)
    port = int(os.environ.get("AQUACAST_BACKEND_PORT", args.port))
    scenario = os.environ.get("AQUACAST_WQ_SCENARIO", args.scenario)
    db_path = Path(os.environ.get("AQUACAST_DB_PATH", args.db_path))
    db_drop = args.db_drop or aquacast_db._truthy(os.environ.get("AQUACAST_DB_DROP"))
    aquacast_db.init_db(db_path, drop=db_drop)
    db_action = "dropped and created" if db_drop else "created if missing"
    print(f"Aquacast SQLite schema {db_action}: {db_path}", flush=True)
    server = build_server(host, port, scenario)
    print(f"Aquacast water-quality backend listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            RequestHandler.backend.kafka.close()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
