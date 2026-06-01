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
from kafka_publisher import KafkaPublisher  # noqa: E402


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class WaterQualityBackend:
    def __init__(
        self,
        *,
        constants_path: Path,
        feed_rate_path: Path,
        scenarios_path: Path,
        scenario_name: str,
    ):
        self.constants_path = constants_path
        self.feed_rate_path = feed_rate_path
        self.scenarios_path = scenarios_path
        self.scenario_name = scenario_name
        self._lock = threading.RLock()
        self.model = load_model(constants_path, feed_rate_path, scenarios_path, scenario_name)
        self.kafka = KafkaPublisher()
        self.kafka.start()

    def reset(self, scenario_name: str | None = None) -> dict[str, Any]:
        with self._lock:
            name = scenario_name or self.scenario_name
            self.model = load_model(self.constants_path, self.feed_rate_path, self.scenarios_path, name)
            self.scenario_name = name
            snap = self.snapshot()
            self.kafka.publish_state(self)
            return snap

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self.model.snapshot())
            snap["status"] = "ok"
            snap["backend"] = "aquacast-water-quality"
            return snap

    def advance(self, real_dt_s: float, temperature_c: float | None = None) -> dict[str, Any]:
        with self._lock:
            self.model.advance(real_dt_s, temperature_c=temperature_c)
            snap = self.snapshot()
            self.kafka.publish_state(self)
            return snap

    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("type", "")).strip()
        with self._lock:
            if kind in {"feed", "apply_feed"}:
                self.model.apply_feed(float(payload.get("mass_kg", 0.0)))
            elif kind == "set_water_exchange":
                self.model.set_water_exchange(float(payload.get("q_lph", 0.0)))
            elif kind == "set_inflow":
                self.model.set_inflow(bool(payload.get("enabled", True)))
            elif kind == "set_heater":
                self.model.set_heater(float(payload.get("power", 0.0)))
            elif kind == "set_biofilter":
                self.model.set_biofilter(bool(payload.get("enabled", True)))
            elif kind == "set_stock":
                self.model.set_stock(float(payload.get("fish_count", payload.get("n", 0.0))), float(payload.get("fish_weight_kg", payload.get("w_kg", 1.0))))
            elif kind == "load_scenario":
                loaded = self.model.load_scenario(str(payload.get("name", "baseline")))
                if not loaded:
                    raise ValueError(f"unknown scenario: {payload.get('name')}")
            elif kind == "reset":
                return self.reset(str(payload.get("name", self.scenario_name)))
            else:
                raise ValueError(f"unknown action type: {kind}")
            return self.snapshot()

    def sensor(self, sensor_name: str) -> dict[str, Any]:
        with self._lock:
            name = sensor_name or "mixed_tank_outlet"
            reading = self.model.sensor_reading(name).as_dict()
            reading["status"] = "ok"
            return reading

    def all_sensors(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "readings": [self.model.sensor_reading(name).as_dict() for name in DEFAULT_SENSOR_NAMES],
            }

    def particle_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        heat_weights = payload.get("heat_weights") or []
        positions = payload.get("positions")
        with self._lock:
            return {
                "status": "ok",
                "values": self.model.particle_values(heat_weights, positions),
            }

    def register_particles(self, payload: dict[str, Any]) -> dict[str, Any]:
        positions = payload.get("positions") or []
        heat_weights = payload.get("heat_weights")
        tags = payload.get("tags")
        with self._lock:
            return self.model.register_particles(positions, heat_weights, tags)

    def registered_particle_values(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "values": self.model.registered_particle_values(),
            }


class RequestHandler(BaseHTTPRequestHandler):
    backend: WaterQualityBackend

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self._write_json({"status": "ok", "service": "aquacast-water-quality"})
            elif parsed.path == "/snapshot":
                self._write_json(self.backend.snapshot())
            elif parsed.path == "/sensor":
                self._write_json(self.backend.sensor((query.get("name") or ["mixed_tank_outlet"])[0]))
            elif parsed.path == "/sensors":
                self._write_json(self.backend.all_sensors())
            elif parsed.path == "/particles/values":
                self._write_json(self.backend.registered_particle_values())
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
            elif parsed.path == "/reset":
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
    args = parser.parse_args()

    _load_env_file(Path(args.env_file))
    host = os.environ.get("AQUACAST_BACKEND_HOST", args.host)
    port = int(os.environ.get("AQUACAST_BACKEND_PORT", args.port))
    scenario = os.environ.get("AQUACAST_WQ_SCENARIO", args.scenario)
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
