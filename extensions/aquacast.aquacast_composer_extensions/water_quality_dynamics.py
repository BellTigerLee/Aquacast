"""Pure water-quality equations for Aquacast.

Units are mg/L for concentrations, kg for feed/biomass, and hours for time.
The runtime layer owns USD writes; this module is intentionally Omniverse-free.
"""

from __future__ import annotations

import math
from typing import Mapping

import numpy as np


EPS = 1e-12


def clamp(value: float, low: float, high: float) -> float:
    return float(np.clip(value, low, high))


def mo2_base(
    temp_c: float,
    fish_weight_kg: float,
    *,
    a: float = 83.0,
    w_exp: float = -0.14,
    q10: float = 2.5,
    t_ref: float = 10.0,
) -> float:
    """Base fish oxygen consumption in mg O2/kg biomass/h."""
    weight = max(EPS, float(fish_weight_kg))
    q10_term = max(EPS, float(q10)) ** ((float(temp_c) - float(t_ref)) / 10.0)
    return max(0.0, float(a)) * (weight ** float(w_exp)) * q10_term


def do_saturation(temp_c: float) -> float:
    """Freshwater DO saturation fit, mg/L, monotone decreasing over tank temperatures."""
    tk = float(temp_c) + 273.15
    tk = max(200.0, tk)
    ln_do = (
        -139.34411
        + 1.575701e5 / tk
        - 6.642308e7 / (tk * tk)
        + 1.243800e10 / (tk * tk * tk)
        - 8.621949e11 / (tk * tk * tk * tk)
    )
    return max(0.0, float(np.exp(ln_do)))


def appetite_factor(do_mg_l: float, *, do_zero: float = 3.0, do_maxFI: float = 7.0) -> float:
    span = max(EPS, float(do_maxFI) - float(do_zero))
    return clamp((float(do_mg_l) - float(do_zero)) / span, 0.0, 1.0)


def nitrification_rate(
    tan_mg_l: float,
    *,
    k_nitrif: float = 0.8,
    vtr_max: float = 5.0,
    biofilter_on: bool = True,
    temp_c: float | None = None,
    do_mg_l: float | None = None,
    theta: float = 1.07,
    t_ref_c: float = 20.0,
    k_o2_mg_l: float = 1.0,
) -> float:
    """First-order TAN oxidation capped by biofilter volumetric capacity, mg TAN/L/h.

    Optional temperature (Arrhenius/theta) and dissolved-oxygen (Monod) limitation
    factors are applied when ``temp_c`` / ``do_mg_l`` are supplied. Nitrifiers slow in
    cold water and stall without oxygen; omitting both arguments leaves the bare
    first-order rate (backward compatible).
    """
    if not biofilter_on:
        return 0.0
    base = clamp(float(k_nitrif) * max(0.0, float(tan_mg_l)), 0.0, max(0.0, float(vtr_max)))
    f_temp = float(theta) ** (float(temp_c) - float(t_ref_c)) if temp_c is not None else 1.0
    if do_mg_l is None:
        f_do = 1.0
    else:
        do = max(0.0, float(do_mg_l))
        f_do = do / (max(EPS, float(k_o2_mg_l)) + do)
    return clamp(base * f_temp * f_do, 0.0, max(0.0, float(vtr_max)))


def tan_production(feed_rate_kg_h: float, *, protein_content: float = 0.45, tan_per_feed: float = 0.092) -> float:
    """TAN production from metabolized feed, kg TAN/h."""
    return max(0.0, float(feed_rate_kg_h)) * max(0.0, float(protein_content)) * max(0.0, float(tan_per_feed))


def ph_from_carbonate(co2_mg_l: float, alk_mg_l_as_caco3: float, *, pk1: float = 6.35) -> float:
    alk_mol = max(EPS, float(alk_mg_l_as_caco3) / 50000.0)
    co2_mol = max(EPS, float(co2_mg_l) / 44000.0)
    return clamp(float(pk1) + math.log10(alk_mol / co2_mol), 4.0, 10.0)


def nh3_fraction(temp_c: float, ph: float) -> float:
    pka = 0.09018 + 2729.92 / max(1.0, float(temp_c) + 273.15)
    return clamp(1.0 / (1.0 + 10.0 ** (pka - float(ph))), 0.0, 1.0)


def derivatives(state: Mapping[str, float], params: Mapping[str, float | bool]) -> dict[str, float]:
    """Return CSTR derivatives for DO/TAN/CO2/Alk in mg/L/h."""
    temp_c = float(state.get("temperature_c", params.get("temperature_c", 14.0)))
    do = float(state.get("dissolved_oxygen_mg_l", state.get("do_mg_l", 9.0)))
    tan = max(0.0, float(state.get("tan_mg_l", 0.0)))
    co2 = max(0.0, float(state.get("co2_mg_l", 0.0)))
    alk = max(0.0, float(state.get("alkalinity_mg_l_as_caco3", 120.0)))
    feed_pool_kg = max(0.0, float(state.get("feed_pool_kg", 0.0)))

    volume_l = max(EPS, float(params.get("tank_volume_l", 10000.0)))
    q_lph = max(0.0, float(params.get("flow_lph", 2000.0))) if bool(params.get("inflow_enabled", True)) else 0.0
    fish_count = max(0.0, float(params.get("fish_count", 200.0)))
    fish_weight_kg = max(EPS, float(params.get("fish_weight_kg", 1.0)))
    biomass_kg = fish_count * fish_weight_kg
    tau_feed_h = max(EPS, float(params.get("tau_feed_h", 4.0)))
    feed_rate_kg_h = feed_pool_kg / tau_feed_h

    fish_o2_mg_h = biomass_kg * mo2_base(
        temp_c,
        fish_weight_kg,
        a=float(params.get("mo2_a", 83.0)),
        w_exp=float(params.get("mo2_w_exp", -0.14)),
        q10=float(params.get("mo2_q10", 2.5)),
        t_ref=float(params.get("mo2_t_ref", 10.0)),
    )
    feed_o2_mg_h = float(params.get("o2_per_feed", 0.225)) * feed_rate_kg_h * 1e6
    total_o2_mg_h = fish_o2_mg_h + feed_o2_mg_h

    r_nitrif = nitrification_rate(
        tan,
        k_nitrif=float(params.get("k_nitrif_h", params.get("k_nitrif", 0.8))),
        vtr_max=float(params.get("vtr_max_mg_l_h", params.get("vtr_max", 5.0))),
        biofilter_on=bool(params.get("biofilter_on", True)),
        temp_c=temp_c,
        do_mg_l=do,
        theta=float(params.get("nitrif_theta", 1.07)),
        t_ref_c=float(params.get("nitrif_t_ref_c", 20.0)),
        k_o2_mg_l=float(params.get("nitrif_k_o2_mg_l", 1.0)),
    )
    p_tan_kg_h = tan_production(
        feed_rate_kg_h,
        protein_content=float(params.get("protein_content", 0.45)),
        tan_per_feed=float(params.get("tan_per_feed", 0.092)),
    )

    q_over_v = q_lph / volume_l
    d_do = (
        float(params.get("kla_o2_h", 2.0)) * (do_saturation(temp_c) - do)
        + q_over_v * (float(params.get("do_in", do_saturation(temp_c))) - do)
        - total_o2_mg_h / volume_l
        - float(params.get("o2_per_tan", 4.57)) * r_nitrif
    )
    d_tan = p_tan_kg_h * 1e6 / volume_l - q_over_v * tan - r_nitrif
    d_co2 = (
        float(params.get("co2_per_o2", 1.375)) * total_o2_mg_h / volume_l
        - float(params.get("kla_co2_h", 1.5)) * (co2 - float(params.get("co2_eq", 0.5)))
        - q_over_v * co2
    )
    d_alk = (
        -float(params.get("alk_per_tan", 7.14)) * r_nitrif
        + q_over_v * (float(params.get("alk_in", 120.0)) - alk)
    )
    return {
        "dissolved_oxygen_mg_l": float(d_do),
        "tan_mg_l": float(d_tan),
        "co2_mg_l": float(d_co2),
        "alkalinity_mg_l_as_caco3": float(d_alk),
        "r_nitrif_mg_l_h": float(r_nitrif),
        "feed_rate_kg_h": float(feed_rate_kg_h),
        "fish_o2_mg_h": float(fish_o2_mg_h),
        "total_o2_mg_h": float(total_o2_mg_h),
    }


# Backward-compatible aliases used by early smoke tests and old call sites.
unionized_ammonia_fraction = nh3_fraction
oxygen_saturation_mg_l = do_saturation

