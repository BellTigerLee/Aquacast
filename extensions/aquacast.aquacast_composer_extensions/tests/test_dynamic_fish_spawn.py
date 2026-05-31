import math
from pathlib import Path
import sys

EXT_ROOT = Path(__file__).resolve().parents[1]
if str(EXT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXT_ROOT))

import dynamic_fish_spawn as spawn


def test_resolve_count_clamps_and_falls_back():
    assert spawn.resolve_count(None, 3) == 3
    assert spawn.resolve_count("5", 3) == 5
    assert spawn.resolve_count("-2", 3) == 0
    assert spawn.resolve_count("abc", 3) == 3


def test_resolve_scale_clamps_and_falls_back():
    assert spawn.resolve_scale(None, 10.0) == 10.0
    assert spawn.resolve_scale("2.5", 10.0) == 2.5
    assert spawn.resolve_scale("-1", 10.0) == 0.0
    assert spawn.resolve_scale("bad", 10.0) == 10.0


def test_resolve_mix_ratio_clamps():
    assert spawn.resolve_mix_ratio(None, 0.5) == 0.5
    assert spawn.resolve_mix_ratio("1.2", 0.5) == 1.0
    assert spawn.resolve_mix_ratio("-0.2", 0.5) == 0.0
    assert spawn.resolve_mix_ratio("bad", 0.5) == 0.5


def test_resolve_seed_supports_numeric_default_and_random():
    assert not spawn.is_random_seed(None)
    assert not spawn.is_random_seed(42)
    assert spawn.is_random_seed("random")
    assert spawn.is_random_seed(" RANDOM ")
    assert spawn.resolve_seed(None, 42) == 42
    assert spawn.resolve_seed("99", 42) == 99
    assert spawn.resolve_seed("12.0", 42) == 12
    assert spawn.resolve_seed("bad", 42) == 42
    assert spawn.resolve_seed(None, "random", random_seed_factory=lambda: 11) == 11
    assert spawn.resolve_seed("bad", "random", random_seed_factory=lambda: 13) == 13
    assert spawn.resolve_seed("random", 42, random_seed_factory=lambda: 123456) == 123456
    assert spawn.resolve_seed(" RANDOM ", 42, random_seed_factory=lambda: 7) == 7


def test_resolve_asset_path_expands_home():
    resolved = spawn.resolve_asset_path("~/cs-project/assets/salmon_1.usd", "unused")
    assert resolved == str((Path.home() / "cs-project/assets/salmon_1.usd").resolve())


def test_next_fish_indices_start_after_existing_matches():
    assert spawn.next_fish_indices([], 3) == [1, 2, 3]
    assert spawn.next_fish_indices(["Fish_03", "Fish_07", "Other_99"], 2) == [8, 9]


def test_assign_assets_respects_ratio_and_seed():
    assert spawn.assign_assets(4, 1.0, 1) == [0, 0, 0, 0]
    assert spawn.assign_assets(4, 0.0, 1) == [1, 1, 1, 1]
    choices = spawn.assign_assets(10, 0.5, 42)
    assert choices.count(0) == 5
    assert choices.count(1) == 5
    assert choices == spawn.assign_assets(10, 0.5, 42)
    assert choices != spawn.assign_assets(10, 0.5, 43)


def test_sample_positions_stay_inside_cylinder_and_are_reproducible():
    positions = spawn.sample_positions(1000, 2.0, -1.0, 3.0, 42)
    assert positions == spawn.sample_positions(1000, 2.0, -1.0, 3.0, 42)
    for x, y, z in positions:
        assert x * x + y * y <= 4.0 + 1e-9
        assert -1.0 <= z <= 3.0


def test_sample_positions_cover_cylinder_mean_near_center():
    positions = spawn.sample_positions(10000, 2.0, -1.0, 3.0, 7)
    mean_x = sum(pos[0] for pos in positions) / len(positions)
    mean_y = sum(pos[1] for pos in positions) / len(positions)
    mean_z = sum(pos[2] for pos in positions) / len(positions)
    assert abs(mean_x) < 0.04
    assert abs(mean_y) < 0.04
    assert math.isclose(mean_z, 1.0, abs_tol=0.04)


def test_clamp_add_count_respects_capacity():
    assert spawn.clamp_add_count(5, 10, 30) == 5
    assert spawn.clamp_add_count(8, 27, 30) == 3
    assert spawn.clamp_add_count(2, 30, 30) == 0
    assert spawn.clamp_add_count(-4, 10, 30) == 0
    assert spawn.clamp_add_count(5, -3, 2) == 2


def test_clamp_remove_count_respects_available():
    assert spawn.clamp_remove_count(3, 10) == 3
    assert spawn.clamp_remove_count(8, 5) == 5
    assert spawn.clamp_remove_count(2, 0) == 0
    assert spawn.clamp_remove_count(-1, 5) == 0
    assert spawn.clamp_remove_count(3, -2) == 0
