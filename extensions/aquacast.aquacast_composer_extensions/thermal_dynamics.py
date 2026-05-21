"""Pure-math helpers for water temperature visualization."""

from __future__ import annotations

import math


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
