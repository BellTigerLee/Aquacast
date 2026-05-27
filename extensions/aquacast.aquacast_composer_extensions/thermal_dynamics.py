"""Pure-math helpers for water temperature visualization."""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np


STEFAN_BOLTZMANN = 5.670374419e-8


def equilibrium_temperature(
    *,
    T_room: float,
    T_inlet: float,
    k_room: float,
    k_inflow: float,
    inflow_enabled: bool,
) -> float | None:
    """Asymptotic equilibrium of the lumped heat balance."""
    k_inflow_eff = k_inflow if inflow_enabled else 0.0
    b = k_room + k_inflow_eff
    if b <= 0.0:
        return None
    return (k_room * T_room + k_inflow_eff * T_inlet) / b


def step_temperature(
    T: float,
    dt: float,
    *,
    T_room: float,
    T_inlet: float,
    k_room: float,
    k_inflow: float,
    inflow_enabled: bool,
) -> float:
    """Step a single bulk temperature with exact Newton heat-balance integration."""
    eq = equilibrium_temperature(
        T_room=T_room,
        T_inlet=T_inlet,
        k_room=k_room,
        k_inflow=k_inflow,
        inflow_enabled=inflow_enabled,
    )
    k_inflow_eff = k_inflow if inflow_enabled else 0.0
    b = k_room + k_inflow_eff
    if eq is None or dt <= 0.0:
        return T
    return eq + (T - eq) * math.exp(-b * dt)


def saturation_vapor_pressure_kpa(temp_c: float) -> float:
    """Tetens saturation vapor pressure over water, kPa."""
    theta = float(temp_c)
    return 0.6108 * math.exp(17.27 * theta / (theta + 237.3))


def tank_geometry(params: Mapping[str, Any]) -> dict[str, float]:
    """Return cylinder volume and heat-transfer areas in SI units."""
    radius = max(1e-6, float(params.get("tank_radius_m", 1.2)))
    height = max(1e-6, float(params.get("tank_water_height_m", 2.21)))
    volume_m3 = float(params.get("tank_volume_l", 10000.0)) / 1000.0
    if volume_m3 <= 0.0:
        volume_m3 = math.pi * radius * radius * height
    surface_area_m2 = math.pi * radius * radius
    wall_area_m2 = 2.0 * math.pi * radius * height + math.pi * radius * radius
    return {
        "radius_m": radius,
        "height_m": height,
        "volume_m3": volume_m3,
        "surface_area_m2": surface_area_m2,
        "wall_area_m2": wall_area_m2,
    }


def surface_heat_flux_w(
    water_temp_c: float,
    *,
    air_temp_c: float,
    room_temp_c: float,
    rel_humidity: float,
    air_speed_ms: float,
    evap_a_w_m2_kpa: float,
    evap_b_w_m2_kpa_per_ms: float,
    bowen_gamma_kpa_k: float,
    emissivity: float,
) -> dict[str, float]:
    """Free-surface heat flux terms. Positive q_net_w_m2 heats the water."""
    water_temp_c = float(water_temp_c)
    air_temp_c = float(air_temp_c)
    room_temp_c = float(room_temp_c)
    rh = min(1.0, max(0.0, float(rel_humidity)))
    transfer = max(0.0, float(evap_a_w_m2_kpa) + float(evap_b_w_m2_kpa_per_ms) * max(0.0, float(air_speed_ms)))
    e_surface = saturation_vapor_pressure_kpa(water_temp_c)
    e_air = rh * saturation_vapor_pressure_kpa(air_temp_c)
    h_evap = transfer * max(0.0, e_surface - e_air)
    h_conv = float(bowen_gamma_kpa_k) * transfer * (water_temp_c - air_temp_c)
    water_k = water_temp_c + 273.15
    room_k = room_temp_c + 273.15
    h_lw = float(emissivity) * STEFAN_BOLTZMANN * (room_k ** 4 - water_k ** 4)
    return {
        "evap_w_m2": h_evap,
        "conv_w_m2": h_conv,
        "longwave_w_m2": h_lw,
        "q_net_w_m2": h_lw - h_evap - h_conv,
    }


def net_heat_w(
    water_temp_c: float,
    params: Mapping[str, Any],
    *,
    inflow_enabled: bool = True,
) -> dict[str, float]:
    """Mechanistic tank heat balance. Positive q_net_w heats the water."""
    geom = tank_geometry(params)
    rho = max(1e-6, float(params.get("water_density", 998.0)))
    cp = max(1e-6, float(params.get("water_cp", 4186.0)))
    t_room = float(params.get("room_temp_c", params.get("air_temp_c", 22.0)))
    t_air = float(params.get("air_temp_c", t_room))
    t_inlet = float(params.get("inlet_temp_c", 12.0))
    q_make_lph = float(params.get("q_makeup_lph", params.get("flow_lph", 0.0)))
    q_make_m3_s = max(0.0, q_make_lph) / 1000.0 / 3600.0 if inflow_enabled else 0.0
    q_adv = rho * cp * q_make_m3_s * (t_inlet - water_temp_c)
    q_wall = float(params.get("u_wall_w_m2k", 5.0)) * geom["wall_area_m2"] * (t_room - water_temp_c)
    surface = surface_heat_flux_w(
        water_temp_c,
        air_temp_c=t_air,
        room_temp_c=t_room,
        rel_humidity=float(params.get("rel_humidity", 0.60)),
        air_speed_ms=float(params.get("air_speed_ms", 0.2)),
        evap_a_w_m2_kpa=float(params.get("evap_a_w_m2_kpa", 18.0)),
        evap_b_w_m2_kpa_per_ms=float(params.get("evap_b_w_m2_kpa_per_ms", 12.0)),
        bowen_gamma_kpa_k=float(params.get("bowen_gamma_kpa_k", 0.066)),
        emissivity=float(params.get("emissivity", 0.96)),
    )
    q_surf = geom["surface_area_m2"] * surface["q_net_w_m2"]
    q_heater = float(params.get("heater_power_w", params.get("heater_power", 0.0)))
    q_net = q_adv + q_wall + q_surf + q_heater
    heat_capacity_j_k = rho * geom["volume_m3"] * cp
    return {
        "q_net_w": q_net,
        "q_adv_w": q_adv,
        "q_wall_w": q_wall,
        "q_surface_w": q_surf,
        "q_heater_w": q_heater,
        "heat_capacity_j_k": heat_capacity_j_k,
        **surface,
        **geom,
    }


def temperature_derivative_c_s(
    water_temp_c: float,
    params: Mapping[str, Any],
    *,
    inflow_enabled: bool = True,
) -> float:
    heat = net_heat_w(water_temp_c, params, inflow_enabled=inflow_enabled)
    return heat["q_net_w"] / max(1e-9, heat["heat_capacity_j_k"])


def step_temperature_rk4(
    T: float,
    dt_s: float,
    params: Mapping[str, Any],
    *,
    inflow_enabled: bool = True,
) -> float:
    """Step bulk temperature with RK4 over SI seconds."""
    dt_s = max(0.0, float(dt_s))
    if dt_s <= 0.0:
        return float(T)
    T = float(T)

    def f(temp: float) -> float:
        return temperature_derivative_c_s(temp, params, inflow_enabled=inflow_enabled)

    k1 = f(T)
    k2 = f(T + 0.5 * dt_s * k1)
    k3 = f(T + 0.5 * dt_s * k2)
    k4 = f(T + dt_s * k3)
    return T + (dt_s / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)


def build_knn_graph(positions: Any, k: int = 8, sigma: float | None = None) -> dict[str, Any]:
    """Build a dense KNN graph for a static particle cloud."""
    arr = np.asarray(positions, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != 3 or len(arr) == 0:
        return {
            "positions": np.empty((0, 3), dtype=np.float64),
            "neighbors": np.empty((0, 0), dtype=np.int64),
            "weights": np.empty((0, 0), dtype=np.float64),
            "row_sums": np.empty((0,), dtype=np.float64),
            "sigma": 1.0,
        }
    count = len(arr)
    k_eff = max(0, min(int(k), count - 1))
    if k_eff == 0:
        return {
            "positions": arr,
            "neighbors": np.empty((count, 0), dtype=np.int64),
            "weights": np.empty((count, 0), dtype=np.float64),
            "row_sums": np.zeros(count, dtype=np.float64),
            "sigma": 1.0,
        }

    diff = arr[:, None, :] - arr[None, :, :]
    dist2 = np.sum(diff * diff, axis=2)
    np.fill_diagonal(dist2, np.inf)
    neighbors = np.argpartition(dist2, kth=k_eff - 1, axis=1)[:, :k_eff]
    neighbor_dist2 = np.take_along_axis(dist2, neighbors, axis=1)
    order = np.argsort(neighbor_dist2, axis=1)
    neighbors = np.take_along_axis(neighbors, order, axis=1)
    neighbor_dist2 = np.take_along_axis(neighbor_dist2, order, axis=1)
    if sigma is None or sigma <= 0.0:
        finite = neighbor_dist2[np.isfinite(neighbor_dist2)]
        sigma = math.sqrt(float(np.median(finite))) if finite.size else 1.0
    sigma = max(1e-9, float(sigma))
    weights = np.exp(-neighbor_dist2 / (2.0 * sigma * sigma))
    weights = np.where(np.isfinite(weights), weights, 0.0)
    return {
        "positions": arr,
        "neighbors": neighbors.astype(np.int64),
        "weights": weights.astype(np.float64),
        "row_sums": np.sum(weights, axis=1),
        "sigma": sigma,
    }


def diffuse_step(
    temperatures: Any,
    *,
    bulk_temp_c: float,
    graph: Mapping[str, Any],
    dt_s: float,
    diffusion_d: float,
    bulk_relax_lambda: float,
    source_c_s: Any | None = None,
) -> np.ndarray:
    """Explicit neighbor diffusion with stability clamp and mean-lock to bulk."""
    temps = np.asarray(temperatures, dtype=np.float64)
    if temps.size == 0:
        return temps.copy()
    neighbors = np.asarray(graph.get("neighbors"), dtype=np.int64)
    weights = np.asarray(graph.get("weights"), dtype=np.float64)
    row_sums = np.asarray(graph.get("row_sums"), dtype=np.float64)
    dt_s = max(0.0, float(dt_s))
    d_eff = max(0.0, float(diffusion_d))
    if row_sums.size:
        max_row = max(1e-12, float(np.max(row_sums)))
        d_eff = min(d_eff, 0.5 / max(1e-12, dt_s * max_row)) if dt_s > 0.0 else d_eff

    lap = np.zeros_like(temps)
    if neighbors.size and weights.size:
        lap = np.sum(weights * (temps[neighbors] - temps[:, None]), axis=1)
    source = np.zeros_like(temps) if source_c_s is None else np.asarray(source_c_s, dtype=np.float64)
    if source.shape != temps.shape:
        source = np.zeros_like(temps)
    updated = temps + dt_s * (
        d_eff * lap
        + max(0.0, float(bulk_relax_lambda)) * (float(bulk_temp_c) - temps)
        + source
    )
    updated += float(bulk_temp_c) - float(np.mean(updated))
    return updated


def temperature_to_rgb(
    T: float,
    stops: list[tuple[float, tuple[float, float, float]]],
) -> tuple[float, float, float]:
    """Piecewise-linear color ramp lookup, clamped at endpoints."""
    if not stops:
        return (0.0, 0.0, 0.0)

    ordered = sorted(stops, key=lambda stop: stop[0])
    if T <= ordered[0][0]:
        return tuple(ordered[0][1])
    if T >= ordered[-1][0]:
        return tuple(ordered[-1][1])

    for (t_lo, c_lo), (t_hi, c_hi) in zip(ordered, ordered[1:]):
        if t_lo <= T <= t_hi:
            span = t_hi - t_lo
            if span <= 0.0:
                return tuple(c_hi)
            alpha = (T - t_lo) / span
            return (
                c_lo[0] + (c_hi[0] - c_lo[0]) * alpha,
                c_lo[1] + (c_hi[1] - c_lo[1]) * alpha,
                c_lo[2] + (c_hi[2] - c_lo[2]) * alpha,
            )

    return tuple(ordered[-1][1])
