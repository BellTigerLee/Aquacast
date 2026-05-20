"""Plain pytest unit tests for fish_dynamics pure-math helpers."""

import math
import sys
from pathlib import Path


EXTENSION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXTENSION_ROOT))

import fish_dynamics  # noqa: E402


_TEST_RANGES = {
    "cruise_speed_scale": (0.85, 1.15),
    "speed_noise_amplitude": (0.15, 0.35),
    "speed_noise_freq_hz": (0.05, 0.12),
    "depth_band_center_norm": (0.15, 0.85),
    "depth_band_half_width_norm": (0.08, 0.18),
    "vertical_wander_freq_hz": (0.07, 0.18),
    "bank_gain": (0.6, 1.0),
}


def test_wrap_to_pi_passes_value_in_range_through():
    assert fish_dynamics.wrap_to_pi(0.0) == 0.0
    assert fish_dynamics.wrap_to_pi(1.0) == 1.0
    assert math.isclose(fish_dynamics.wrap_to_pi(-1.0), -1.0)


def test_wrap_to_pi_wraps_above_pi():
    assert math.isclose(fish_dynamics.wrap_to_pi(math.pi + 0.5), -math.pi + 0.5)


def test_wrap_to_pi_wraps_below_negative_pi():
    assert math.isclose(fish_dynamics.wrap_to_pi(-math.pi - 0.5), math.pi - 0.5)


def test_yaw_from_direction_pointing_minus_x_is_zero():
    assert math.isclose(fish_dynamics.yaw_from_direction(-1.0, 0.0), 0.0)


def test_yaw_from_direction_pointing_minus_y_is_pi_over_2():
    assert math.isclose(fish_dynamics.yaw_from_direction(0.0, -1.0), math.pi / 2.0)


def test_intrinsic_speed_factor_centred_on_one_at_zero_phase():
    factor = fish_dynamics.intrinsic_speed_factor(
        now=0.0,
        amplitude=0.25,
        freq_hz=0.1,
        phase=0.0,
        min_fraction=0.4,
    )
    assert math.isclose(factor, 1.0)


def test_intrinsic_speed_factor_floored_by_min_fraction():
    factor = fish_dynamics.intrinsic_speed_factor(
        now=0.0,
        amplitude=1.0,
        freq_hz=0.1,
        phase=-math.pi / 2.0,
        min_fraction=0.4,
    )
    assert math.isclose(factor, 0.4)


def test_intrinsic_speed_factor_swings_above_one():
    factor = fish_dynamics.intrinsic_speed_factor(
        now=0.0,
        amplitude=0.25,
        freq_hz=0.1,
        phase=math.pi / 2.0,
        min_fraction=0.4,
    )
    assert math.isclose(factor, 1.25)


def test_depth_attraction_zero_inside_band_centre():
    assert fish_dynamics.depth_attraction_strength(5.0, 5.0, 1.0) == 0.0


def test_depth_attraction_pulls_up_when_below():
    assert math.isclose(fish_dynamics.depth_attraction_strength(3.0, 5.0, 1.0), 1.0)


def test_depth_attraction_pulls_down_when_above():
    assert math.isclose(fish_dynamics.depth_attraction_strength(7.0, 5.0, 1.0), -1.0)


def test_depth_attraction_linear_inside_band():
    assert math.isclose(fish_dynamics.depth_attraction_strength(4.0, 5.0, 2.0), 0.5)


def test_compute_target_roll_zero_when_no_yaw_change():
    assert fish_dynamics.compute_target_roll(0.0, 1.0, 0.35, 0.6) == 0.0


def test_compute_target_roll_sign_follows_yaw_rate():
    pos = fish_dynamics.compute_target_roll(1.0, 1.0, 0.35, 0.6)
    neg = fish_dynamics.compute_target_roll(-1.0, 1.0, 0.35, 0.6)
    assert pos > 0.0
    assert neg < 0.0
    assert math.isclose(pos, -neg)


def test_compute_target_roll_clamped_to_max():
    assert math.isclose(fish_dynamics.compute_target_roll(100.0, 1.0, 1.0, 0.6), 0.6)


def test_compute_target_roll_clamped_to_negative_max():
    assert math.isclose(fish_dynamics.compute_target_roll(-100.0, 1.0, 1.0, 0.6), -0.6)


def test_sample_fish_traits_returns_all_expected_keys():
    traits = fish_dynamics.sample_fish_traits("Fish_0", base_seed=1, ranges=_TEST_RANGES)
    expected = set(_TEST_RANGES.keys()) | {"speed_noise_phase", "vertical_wander_phase"}
    assert set(traits.keys()) == expected


def test_sample_fish_traits_in_range():
    traits = fish_dynamics.sample_fish_traits("Fish_0", base_seed=1, ranges=_TEST_RANGES)
    for key, (low, high) in _TEST_RANGES.items():
        assert low <= traits[key] <= high, key
    assert 0.0 <= traits["speed_noise_phase"] < 2.0 * math.pi
    assert 0.0 <= traits["vertical_wander_phase"] < 2.0 * math.pi


def test_sample_fish_traits_deterministic():
    a = fish_dynamics.sample_fish_traits("Fish_0", base_seed=7, ranges=_TEST_RANGES)
    b = fish_dynamics.sample_fish_traits("Fish_0", base_seed=7, ranges=_TEST_RANGES)
    assert a == b


def test_sample_fish_traits_distinct_for_different_names():
    a = fish_dynamics.sample_fish_traits("Fish_0", base_seed=7, ranges=_TEST_RANGES)
    b = fish_dynamics.sample_fish_traits("Fish_1", base_seed=7, ranges=_TEST_RANGES)
    assert a != b


def test_sample_fish_traits_distinct_for_different_base_seeds():
    a = fish_dynamics.sample_fish_traits("Fish_0", base_seed=1, ranges=_TEST_RANGES)
    b = fish_dynamics.sample_fish_traits("Fish_0", base_seed=2, ranges=_TEST_RANGES)
    assert a != b
