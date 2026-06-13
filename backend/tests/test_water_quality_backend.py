from __future__ import annotations

from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from water_quality_backend import WaterQualityBackend  # noqa: E402


def _backend(monkeypatch):
    monkeypatch.setenv("AQUACAST_DB_DISABLED", "1")
    monkeypatch.delenv("AQUACAST_KAFKA_ENABLED", raising=False)
    data_dir = PROJECT_ROOT / "extensions" / "aquacast.aquacast_composer_extensions" / "data"
    return WaterQualityBackend(
        constants_path=data_dir / "wq_constants.json",
        feed_rate_path=data_dir / "wq_feed_rate.json",
        scenarios_path=data_dir / "wq_scenarios.json",
        scenario_name="baseline",
    )


def test_tank_scoped_temperature_action_does_not_change_other_snapshots(monkeypatch):
    backend = _backend(monkeypatch)
    try:
        tank_1 = "/World/Fishtank_1/Water"
        tank_2 = "/World/Fishtank_2/Water"
        baseline = backend.snapshot()["temperature_c"]

        result = backend.action({"type": "set_temperature", "temperature_c": 30.0, "tank_path": tank_2})

        assert result["status"] == "ok"
        assert result["tank_path"] == tank_2
        assert result["temperature_c"] == 30.0
        assert backend.snapshot(tank_2)["temperature_c"] == 30.0
        assert backend.snapshot(tank_1)["temperature_c"] == baseline
        assert backend.snapshot()["temperature_c"] == baseline
    finally:
        backend.kafka.close()


def test_tank_scoped_particle_registration_keeps_temperatures_independent(monkeypatch):
    backend = _backend(monkeypatch)
    try:
        tank_1 = "/World/Fishtank_1/Water"
        tank_2 = "/World/Fishtank_2/Water"
        positions = [[0.0, 0.0, 0.0], [0.0, 0.5, 0.0], [0.2, 1.0, 0.0]]
        heat_weights = [0.0, 0.5, 1.0]

        backend.register_particles({"positions": positions, "heat_weights": heat_weights, "tank_path": tank_1})
        backend.register_particles({"positions": positions, "heat_weights": heat_weights, "tank_path": tank_2})
        baseline_values = backend.registered_particle_values(tank_1)["values"]["temperature"]

        backend.action({"type": "set_temperature", "temperature_c": 30.0, "tank_path": tank_2})

        tank_1_values = backend.registered_particle_values(tank_1)["values"]["temperature"]
        tank_2_values = backend.registered_particle_values(tank_2)["values"]["temperature"]
        assert tank_1_values == baseline_values
        assert all(value == 30.0 for value in tank_2_values)
    finally:
        backend.kafka.close()
