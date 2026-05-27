import math

import numpy as np

import thermal_dynamics as t


def _params(**overrides):
    params = {
        "tank_radius_m": 1.2,
        "tank_water_height_m": 2.21,
        "tank_volume_l": 10000.0,
        "water_density": 998.0,
        "water_cp": 4186.0,
        "u_wall_w_m2k": 5.0,
        "emissivity": 0.96,
        "air_temp_c": 22.0,
        "room_temp_c": 22.0,
        "rel_humidity": 0.60,
        "air_speed_ms": 0.2,
        "evap_a_w_m2_kpa": 18.0,
        "evap_b_w_m2_kpa_per_ms": 12.0,
        "bowen_gamma_kpa_k": 0.066,
        "q_makeup_lph": 220.0,
        "inlet_temp_c": 12.0,
        "heater_power_w": 0.0,
    }
    params.update(overrides)
    return params


def test_saturation_vapor_pressure_anchor_and_monotone():
    assert t.saturation_vapor_pressure_kpa(10.0) < t.saturation_vapor_pressure_kpa(20.0)
    assert math.isclose(t.saturation_vapor_pressure_kpa(20.0), 2.34, rel_tol=0.02)


def test_surface_heat_flux_signs():
    flux = t.surface_heat_flux_w(
        24.0,
        air_temp_c=22.0,
        room_temp_c=22.0,
        rel_humidity=0.60,
        air_speed_ms=0.2,
        evap_a_w_m2_kpa=18.0,
        evap_b_w_m2_kpa_per_ms=12.0,
        bowen_gamma_kpa_k=0.066,
        emissivity=0.96,
    )
    assert flux["q_net_w_m2"] < 0.0
    equal_temp = t.surface_heat_flux_w(
        22.0,
        air_temp_c=22.0,
        room_temp_c=22.0,
        rel_humidity=0.60,
        air_speed_ms=0.2,
        evap_a_w_m2_kpa=18.0,
        evap_b_w_m2_kpa_per_ms=12.0,
        bowen_gamma_kpa_k=0.066,
        emissivity=0.96,
    )
    assert equal_temp["evap_w_m2"] > 0.0
    assert equal_temp["q_net_w_m2"] < 0.0


def test_rk4_matches_legacy_linear_case():
    params = _params(
        evap_a_w_m2_kpa=0.0,
        evap_b_w_m2_kpa_per_ms=0.0,
        emissivity=0.0,
        q_makeup_lph=220.0,
    )
    geom = t.tank_geometry(params)
    heat_capacity = params["water_density"] * geom["volume_m3"] * params["water_cp"]
    wall_area = geom["wall_area_m2"]
    params["u_wall_w_m2k"] = heat_capacity * (0.012 / 3600.0) / wall_area

    rk = t.step_temperature_rk4(14.0, 600.0, params)
    legacy = t.step_temperature(
        14.0,
        600.0 / 3600.0,
        T_room=22.0,
        T_inlet=12.0,
        k_room=0.012,
        k_inflow=0.022,
        inflow_enabled=True,
    )
    assert math.isclose(rk, legacy, rel_tol=1e-5, abs_tol=1e-5)


def test_heater_raises_steady_state_direction():
    cold = t.temperature_derivative_c_s(15.0, _params(heater_power_w=0.0))
    heated = t.temperature_derivative_c_s(15.0, _params(heater_power_w=500.0))
    assert heated > cold


def test_diffuse_step_mean_lock_and_propagation():
    positions = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
        ],
        dtype=np.float64,
    )
    graph = t.build_knn_graph(positions, k=2)
    temps = np.array([20.0, 10.0, 10.0])
    updated = t.diffuse_step(
        temps,
        bulk_temp_c=14.0,
        graph=graph,
        dt_s=1.0,
        diffusion_d=0.2,
        bulk_relax_lambda=0.0,
    )
    assert math.isclose(float(np.mean(updated)), 14.0, abs_tol=1e-9)
    assert updated[1] > 10.0
