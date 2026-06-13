import math

import water_quality_dynamics as d


def test_do_sat_decreases_with_temperature():
    assert d.do_saturation(10.0) > d.do_saturation(20.0) > d.do_saturation(25.0)


def test_do_sat_decreases_with_salinity():
    assert d.do_saturation(15.0, salinity_ppt=0.0) > d.do_saturation(15.0, salinity_ppt=35.0)


def test_appetite_factor_clips():
    assert d.appetite_factor(2.0, do_zero=3.0, do_maxFI=7.0) == 0.0
    assert d.appetite_factor(7.5, do_zero=3.0, do_maxFI=7.0) == 1.0
    assert d.appetite_factor(5.0, do_zero=3.0, do_maxFI=7.0) == 0.5


def test_nh3_fraction_increases_with_ph_and_t():
    assert d.nh3_fraction(20.0, 8.0) > d.nh3_fraction(20.0, 7.0)
    assert d.nh3_fraction(24.0, 8.0) > d.nh3_fraction(12.0, 8.0)


def test_ph_drops_when_co2_rises():
    value = d.ph_from_carbonate(40.0, 20.0)
    assert math.isfinite(value)
    assert d.ph_from_carbonate(20.0, 120.0) < d.ph_from_carbonate(5.0, 120.0)
    assert d.ph_from_carbonate(5.0, 180.0) > d.ph_from_carbonate(5.0, 80.0)


def test_nitrification_zero_when_biofilter_off():
    assert d.nitrification_rate(1.0, k_nitrif=0.8, vtr_max=5.0, biofilter_on=False) == 0.0


def test_nitrification_capped_at_vtr_max():
    assert d.nitrification_rate(100.0, k_nitrif=0.8, vtr_max=5.0, biofilter_on=True) == 5.0


def test_nitrification_unchanged_without_temp_and_do():
    # Backward compat: with no temp/DO supplied, only the first-order rate applies.
    assert d.nitrification_rate(1.0, k_nitrif=0.8, vtr_max=5.0) == 0.8


def test_nitrification_slows_in_cold_water():
    cold = d.nitrification_rate(1.0, k_nitrif=0.8, vtr_max=5.0, temp_c=10.0)
    warm = d.nitrification_rate(1.0, k_nitrif=0.8, vtr_max=5.0, temp_c=20.0)
    assert 0.0 < cold < warm


def test_nitrification_monod_do_limitation():
    assert d.nitrification_rate(1.0, k_nitrif=0.8, vtr_max=5.0, do_mg_l=0.0) == 0.0
    low = d.nitrification_rate(1.0, k_nitrif=0.8, vtr_max=5.0, do_mg_l=1.0)
    high = d.nitrification_rate(1.0, k_nitrif=0.8, vtr_max=5.0, do_mg_l=10.0)
    assert 0.0 < low < high


def test_derivatives_nitrification_drops_at_low_do():
    base = {
        "temperature_c": 14.0,
        "tan_mg_l": 2.0,
        "co2_mg_l": 5.0,
        "alkalinity_mg_l_as_caco3": 120.0,
        "feed_pool_kg": 0.0,
    }
    params = {"tank_volume_l": 10000.0, "fish_count": 50, "fish_weight_kg": 1.0}
    low_do = d.derivatives({**base, "dissolved_oxygen_mg_l": 0.5}, params)
    high_do = d.derivatives({**base, "dissolved_oxygen_mg_l": 9.0}, params)
    assert low_do["r_nitrif_mg_l_h"] < high_do["r_nitrif_mg_l_h"]


def test_tan_production_scales_with_feed_and_pc():
    assert d.tan_production(2.0, protein_content=0.5, tan_per_feed=0.092) == 0.092


def test_fish_metabolic_loads_zero_without_fish_or_feed():
    loads = d.fish_metabolic_loads(14.0, 0.0, 1.0, 0.0, {})

    assert loads["total_o2_mg_h"] == 0.0
    assert loads["total_tan_kg_h"] == 0.0


def test_derivatives_units_signs():
    state = {
        "temperature_c": 14.0,
        "dissolved_oxygen_mg_l": 9.0,
        "tan_mg_l": 0.1,
        "co2_mg_l": 5.0,
        "alkalinity_mg_l_as_caco3": 120.0,
        "feed_pool_kg": 4.0
    }
    params = {"tank_volume_l": 10000.0, "fish_count": 200, "fish_weight_kg": 1.0, "tau_feed_h": 4.0}
    deriv = d.derivatives(state, params)
    assert deriv["dissolved_oxygen_mg_l"] < 0.0
    assert deriv["tan_mg_l"] > 0.0
    assert deriv["co2_mg_l"] > 0.0
    assert deriv["turbidity_ntu"] > 0.0


def test_derivatives_salinity_moves_toward_inlet():
    state = {
        "temperature_c": 14.0,
        "dissolved_oxygen_mg_l": 9.0,
        "tan_mg_l": 0.1,
        "co2_mg_l": 5.0,
        "alkalinity_mg_l_as_caco3": 120.0,
        "salinity_ppt": 1.0,
        "turbidity_ntu": 2.0,
        "feed_pool_kg": 0.0,
    }
    params = {"tank_volume_l": 1000.0, "flow_lph": 100.0, "salinity_in_ppt": 0.2}

    assert d.derivatives(state, params)["salinity_ppt"] < 0.0
