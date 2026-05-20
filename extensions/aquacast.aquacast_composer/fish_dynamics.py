"""Pure-math helpers for fish motion dynamics.

This module intentionally avoids Kit, USD, and Gf imports so it can be tested
with plain pytest outside Omniverse.
"""

from __future__ import annotations

import math
import random


_TWO_PI = 2.0 * math.pi


def wrap_to_pi(angle: float) -> float:
    """Wrap an angle in radians to the half-open interval [-pi, pi)."""
    return (angle + math.pi) % _TWO_PI - math.pi


def yaw_from_direction(dx: float, dy: float) -> float:
    """Yaw implied by a horizontal direction using the fish -X-forward convention."""
    return math.atan2(-dy, -dx)


def intrinsic_speed_factor(
    now: float,
    amplitude: float,
    freq_hz: float,
    phase: float,
    min_fraction: float,
) -> float:
    """Slow sine-based multiplier on a fish's cruise speed."""
    swing = 1.0 + amplitude * math.sin(_TWO_PI * freq_hz * now + phase)
    return max(min_fraction, swing)


def depth_attraction_strength(
    position_z: float,
    preferred_z: float,
    band_half: float,
) -> float:
    """Signed pull strength in [-1, 1] toward a preferred depth band."""
    if band_half <= 1e-6:
        return 0.0
    return max(-1.0, min(1.0, (preferred_z - position_z) / band_half))


def compute_target_roll(
    yaw_rate: float,
    bank_gain: float,
    bank_gain_global: float,
    max_bank_radians: float,
) -> float:
    """Map signed yaw rate in radians/s to a clamped roll angle in radians."""
    raw = yaw_rate * bank_gain * bank_gain_global
    return max(-max_bank_radians, min(max_bank_radians, raw))


def sample_fish_traits(
    prim_name: str,
    base_seed: int,
    ranges: dict,
) -> dict:
    """Deterministically sample one fish's motion-dynamics traits."""
    rng = random.Random(f"{base_seed}:{prim_name}")

    def _uniform(key):
        low, high = ranges[key]
        return rng.uniform(low, high)

    return {
        "cruise_speed_scale": _uniform("cruise_speed_scale"),
        "speed_noise_amplitude": _uniform("speed_noise_amplitude"),
        "speed_noise_freq_hz": _uniform("speed_noise_freq_hz"),
        "speed_noise_phase": rng.uniform(0.0, _TWO_PI),
        "depth_band_center_norm": _uniform("depth_band_center_norm"),
        "depth_band_half_width_norm": _uniform("depth_band_half_width_norm"),
        "vertical_wander_freq_hz": _uniform("vertical_wander_freq_hz"),
        "vertical_wander_phase": rng.uniform(0.0, _TWO_PI),
        "bank_gain": _uniform("bank_gain"),
    }
