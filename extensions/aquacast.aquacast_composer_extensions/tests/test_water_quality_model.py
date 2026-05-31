from pathlib import Path

from water_quality_model import DEFAULT_SENSOR_NAMES, WaterQualityModel, load_model


DATA = Path(__file__).resolve().parents[1] / "data"


def test_model_steps_and_keeps_state_nonnegative():
    model = load_model(DATA / "wq_constants.json", DATA / "wq_feed_rate.json", DATA / "wq_scenarios.json", "baseline")
    state = model.advance(1.0, temperature_c=14.0)
    assert state.tan_mg_l >= 0.0
    assert state.dissolved_oxygen_mg_l >= 0.0
    assert state.co2_mg_l >= 0.0
    assert 4.0 <= state.ph <= 10.0


def test_all_named_sensors_return_readings():
    model = WaterQualityModel(
        {"tank_volume_l": 1000.0},
        {"feed_g_s": 0.0},
        {"initial_state": {}},
    )
    for name in DEFAULT_SENSOR_NAMES:
        reading = model.sensor_reading(name)
        assert reading.sensor_name == name
        assert "tan_mg_l" in reading.as_dict()


def test_particle_values_match_particle_count():
    model = WaterQualityModel({}, {}, {"initial_state": {}})
    fields = model.particle_values([0.0, 0.5, 1.0])
    assert set(fields) == {"temperature", "dissolved_oxygen", "tan", "co2", "alkalinity", "ph", "nh3"}
    assert all(len(values) == 3 for values in fields.values())


def test_time_scale_maps_real_to_sim_hours():
    model = WaterQualityModel({"time_scale": 2.0, "substep_h": 0.5}, {}, {"initial_state": {}})
    model.advance(3.0, temperature_c=14.0)
    assert model.state.sim_time_h == 6.0
    assert model.last_substep_count == 12


def test_reproducible_under_same_inputs():
    model_a = load_model(DATA / "wq_constants.json", DATA / "wq_feed_rate.json", DATA / "wq_scenarios.json", "baseline")
    model_b = load_model(DATA / "wq_constants.json", DATA / "wq_feed_rate.json", DATA / "wq_scenarios.json", "baseline")
    for model in (model_a, model_b):
        model.apply_feed(1.0)
        model.set_biofilter(False)
        model.advance(0.25, temperature_c=14.0)
        model.advance(0.75, temperature_c=14.0)
    assert model_a.snapshot() == model_b.snapshot()


def test_load_scenario_resets_state():
    model = load_model(DATA / "wq_constants.json", DATA / "wq_feed_rate.json", DATA / "wq_scenarios.json", "baseline")
    assert model.load_scenario("overfeed")
    assert model.state.feed_pool_kg == 2.5


def test_backend_owned_temperature_evolves_without_override():
    model = load_model(DATA / "wq_constants.json", DATA / "wq_feed_rate.json", DATA / "wq_scenarios.json", "baseline")
    initial = model.state.temperature_c
    model.advance(60.0)
    assert model.state.temperature_c != initial


def test_heater_action_raises_temperature_response():
    base = WaterQualityModel({"time_scale": 1.0, "substep_h": 0.0167}, {}, {"initial_state": {"temperature_c": 14.0}})
    heated = WaterQualityModel({"time_scale": 1.0, "substep_h": 0.0167}, {}, {"initial_state": {"temperature_c": 14.0}})
    heated.set_heater(1000.0)
    base.advance(60.0)
    heated.advance(60.0)
    assert heated.state.temperature_c > base.state.temperature_c


def test_register_advance_particle_values_mean_matches_bulk_temperature():
    model = WaterQualityModel({"time_scale": 1.0, "substep_h": 0.0167}, {}, {"initial_state": {"temperature_c": 14.0}})
    result = model.register_particles(
        [
            [-1.0, 0.2, 0.0],
            [0.0, 0.8, 0.0],
            [1.0, 1.4, 0.0],
            [0.0, 2.0, 0.8],
        ]
    )
    assert result["count"] == 4
    model.advance(60.0)
    values = model.registered_particle_values()
    assert len(values["temperature"]) == 4
    mean_temp = sum(values["temperature"]) / len(values["temperature"])
    assert abs(mean_temp - model.snapshot()["temperature_c"]) < 1e-9
