"""Pure helpers for runtime dynamic fish spawning."""

from __future__ import annotations

import math
from pathlib import Path
import random
import re
from typing import Iterable


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return int(default)
    return int(float(str(value).strip()))


def _parse_float(value: str | None, default: float) -> float:
    if value is None:
        return float(default)
    return float(str(value).strip())


def resolve_count(env_value: str | None, default: int) -> int:
    try:
        return max(0, _parse_int(env_value, default))
    except (TypeError, ValueError):
        return max(0, int(default))


def resolve_scale(env_value: str | None, default: float) -> float:
    try:
        return max(0.0, _parse_float(env_value, default))
    except (TypeError, ValueError):
        return max(0.0, float(default))


def resolve_mix_ratio(env_value: str | None, default: float) -> float:
    try:
        value = _parse_float(env_value, default)
    except (TypeError, ValueError):
        value = float(default)
    return max(0.0, min(1.0, value))


def is_random_seed(value) -> bool:
    return value is not None and str(value).strip().lower() == "random"


def clamp_add_count(requested: int, current_total: int, max_total: int) -> int:
    requested = max(0, int(requested))
    current_total = max(0, int(current_total))
    max_total = max(0, int(max_total))
    return max(0, min(requested, max_total - current_total))


def clamp_remove_count(requested: int, available: int) -> int:
    requested = max(0, int(requested))
    available = max(0, int(available))
    return max(0, min(requested, available))


def resolve_seed(env_value: str | None, default: int | str, random_seed_factory=None) -> int:
    factory = random_seed_factory or (lambda: random.SystemRandom().randrange(0, 2**63))

    value = default if env_value is None else env_value
    if is_random_seed(value):
        return int(factory())
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        if is_random_seed(default):
            return int(factory())
        return int(float(str(default).strip()))


def resolve_asset_path(env_value: str | None, default: str) -> str:
    value = str(env_value if env_value is not None else default).strip()
    return str(Path(value).expanduser().resolve())


def next_fish_indices(existing_names: Iterable[str], count: int, prefix: str = "Fish_") -> list[int]:
    count = max(0, int(count))
    if count <= 0:
        return []
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    max_index = 0
    for name in existing_names:
        match = pattern.match(str(name))
        if match:
            max_index = max(max_index, int(match.group(1)))
    return list(range(max_index + 1, max_index + count + 1))


def assign_assets(count: int, salmon_1_ratio: float, seed: int) -> list[int]:
    count = max(0, int(count))
    if count <= 0:
        return []
    ratio = max(0.0, min(1.0, float(salmon_1_ratio)))
    salmon_1_count = int(math.floor(count * ratio))
    choices = [0] * salmon_1_count + [1] * (count - salmon_1_count)
    random.Random(int(seed)).shuffle(choices)
    return choices


def sample_positions(
    count: int,
    water_radius: float,
    water_min_z: float,
    water_max_z: float,
    seed: int,
) -> list[tuple[float, float, float]]:
    count = max(0, int(count))
    radius = max(0.0, float(water_radius))
    min_z = float(min(water_min_z, water_max_z))
    max_z = float(max(water_min_z, water_max_z))
    rng = random.Random(int(seed))
    positions = []
    for _ in range(count):
        radial = radius * math.sqrt(rng.random())
        theta = math.tau * rng.random()
        z = rng.uniform(min_z, max_z) if max_z > min_z else min_z
        positions.append((radial * math.cos(theta), radial * math.sin(theta), z))
    return positions


def sample_yaws(count: int, seed: int) -> list[float]:
    rng = random.Random(int(seed))
    return [rng.uniform(0.0, 360.0) for _ in range(max(0, int(count)))]
