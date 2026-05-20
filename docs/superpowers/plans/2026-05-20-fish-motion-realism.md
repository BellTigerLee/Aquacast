# Fish Motion Realism Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the fish in Aquacast's composer extension look like individuals — each with its own cruise speed varying over time, preferred depth band, decorrelated vertical bob, and roll into turns — without altering the boids math or introducing animation rigs.

**Architecture:** Extend `FishSwimController` in `main.py` in place. Pure-math (sine, clamp, atan2, seeded sampling) is extracted to a new sibling module `fish_dynamics.py` so it can be unit-tested with plain `pytest`. The Gf/USD-touching pieces (transform writes, frame loop) stay in `main.py`. One toggle `ENABLE_REALISM_DYNAMICS` (read once per frame in `_on_update`) selects new vs legacy behavior.

**Tech Stack:** Python 3.12, NVIDIA Omniverse Kit (`carb`, `omni.kit.app`, `omni.usd`), USD/Gf (`pxr`), `pytest` for unit tests.

**Reference Spec:** `docs/superpowers/specs/2026-05-20-fish-motion-realism-design.md`

---

## File Structure

| Path | Status | Responsibility |
|------|--------|----------------|
| `extensions/aquacast.aquacast_composer/fish_dynamics.py` | Create | Pure-math helpers + seeded trait sampling. No Kit/USD imports. |
| `extensions/aquacast.aquacast_composer/tests/__init__.py` | Create | Make `tests/` a package (empty file). |
| `extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py` | Create | Plain `pytest` unit tests for `fish_dynamics`. |
| `extensions/aquacast.aquacast_composer/global_variable.py` | Modify | Add 11 new configuration constants for the realism dynamics. |
| `extensions/aquacast.aquacast_composer/main.py` | Modify | Wire trait sampling, per-fish speed, depth attraction, banking. |

> Note: there is already a Kit test directory at `extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/tests/`. We intentionally place the new pure-math tests at a different path (`extensions/aquacast.aquacast_composer/tests/`) so they run with plain pytest and do not get pulled into the Kit test harness.

---

## Working directory for all commands

```bash
cd /home/netai-sys/cs-project/Aquacast
```

All paths in this plan are relative to that directory unless absolute.

---

### Task 1: Pure-math helpers — `wrap_to_pi` and `yaw_from_direction`

These two small helpers are foundational for the banking computation in Task 6. Group them in one task because each is three lines.

**Files:**
- Create: `extensions/aquacast.aquacast_composer/fish_dynamics.py`
- Create: `extensions/aquacast.aquacast_composer/tests/__init__.py` (empty)
- Create: `extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py`

- [ ] **Step 1: Create the empty `tests/__init__.py`**

```bash
touch extensions/aquacast.aquacast_composer/tests/__init__.py
```

- [ ] **Step 2: Write failing tests**

Create `extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py`:

```python
"""Plain-pytest unit tests for fish_dynamics pure-math helpers."""
import math
import sys
from pathlib import Path

# Add the extension root (sibling of this tests directory) to sys.path so we
# can import fish_dynamics without needing a Kit environment.
EXTENSION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXTENSION_ROOT))

import fish_dynamics  # noqa: E402


def test_wrap_to_pi_passes_value_in_range_through():
    assert fish_dynamics.wrap_to_pi(0.0) == 0.0
    assert fish_dynamics.wrap_to_pi(1.0) == 1.0
    assert math.isclose(fish_dynamics.wrap_to_pi(-1.0), -1.0)


def test_wrap_to_pi_wraps_above_pi():
    assert math.isclose(fish_dynamics.wrap_to_pi(math.pi + 0.5),
                        -math.pi + 0.5, abs_tol=1e-9)


def test_wrap_to_pi_wraps_below_negative_pi():
    assert math.isclose(fish_dynamics.wrap_to_pi(-math.pi - 0.5),
                        math.pi - 0.5, abs_tol=1e-9)


def test_yaw_from_direction_pointing_minus_x_is_zero():
    # The existing main.py convention: yaw = atan2(-y, -x).
    # Direction (-1, 0) => yaw = atan2(0, 1) = 0.
    assert math.isclose(fish_dynamics.yaw_from_direction(-1.0, 0.0), 0.0)


def test_yaw_from_direction_pointing_minus_y_is_pi_over_2():
    # Direction (0, -1) => yaw = atan2(1, 0) = pi/2.
    assert math.isclose(fish_dynamics.yaw_from_direction(0.0, -1.0),
                        math.pi / 2.0, abs_tol=1e-9)
```

- [ ] **Step 3: Run the tests; expect them to fail because `fish_dynamics` does not exist yet**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: collection error with `ModuleNotFoundError: No module named 'fish_dynamics'`.

- [ ] **Step 4: Create the module with the two helpers**

Create `extensions/aquacast.aquacast_composer/fish_dynamics.py`:

```python
"""Pure-math helpers for fish motion dynamics.

This module is intentionally free of `carb`, `omni`, and `pxr` imports so that
its contents can be unit-tested with plain `pytest` outside of an Omniverse
Kit environment.
"""

from __future__ import annotations

import math


_TWO_PI = 2.0 * math.pi


def wrap_to_pi(angle: float) -> float:
    """Wrap an angle in radians to the half-open interval (-pi, pi]."""
    wrapped = (angle + math.pi) % _TWO_PI - math.pi
    return wrapped


def yaw_from_direction(dx: float, dy: float) -> float:
    """Yaw (rotation around world Z) implied by a horizontal direction vector.

    Uses the same convention as main.py's `_local_direction_to_rotate_xyz`:
    yaw = atan2(-dy, -dx).
    """
    return math.atan2(-dy, -dx)
```

- [ ] **Step 5: Run the tests; expect all five to pass**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: `5 passed`.

- [ ] **Step 6: Commit**

```bash
git add extensions/aquacast.aquacast_composer/fish_dynamics.py \
        extensions/aquacast.aquacast_composer/tests/__init__.py \
        extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py
git commit -m "feat(aquacast): add fish_dynamics module with wrap_to_pi and yaw_from_direction"
```

---

### Task 2: `intrinsic_speed_factor`

Pure-math multiplier on cruise speed driven by a low-frequency sine. Floored by a minimum fraction so fish never stall.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/fish_dynamics.py`
- Modify: `extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py`

- [ ] **Step 1: Append failing tests**

Append to `extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py`:

```python
def test_intrinsic_speed_factor_centred_on_one_at_zero_phase():
    # sin(0) = 0  =>  swing = 1.0
    factor = fish_dynamics.intrinsic_speed_factor(
        now=0.0, amplitude=0.25, freq_hz=0.1, phase=0.0, min_fraction=0.4
    )
    assert math.isclose(factor, 1.0, abs_tol=1e-9)


def test_intrinsic_speed_factor_floored_by_min_fraction():
    # With phase = -pi/2 the sine evaluates to -1, so swing = 1 - 1 = 0.
    # The min_fraction floor must apply.
    factor = fish_dynamics.intrinsic_speed_factor(
        now=0.0, amplitude=1.0, freq_hz=0.1, phase=-math.pi / 2.0,
        min_fraction=0.4,
    )
    assert math.isclose(factor, 0.4)


def test_intrinsic_speed_factor_swings_above_one():
    # phase = +pi/2 => sin = +1, swing = 1.25.
    factor = fish_dynamics.intrinsic_speed_factor(
        now=0.0, amplitude=0.25, freq_hz=0.1, phase=math.pi / 2.0,
        min_fraction=0.4,
    )
    assert math.isclose(factor, 1.25)
```

- [ ] **Step 2: Run; expect three new failures**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: 5 pass, 3 fail with `AttributeError: module 'fish_dynamics' has no attribute 'intrinsic_speed_factor'`.

- [ ] **Step 3: Implement the function**

Append to `extensions/aquacast.aquacast_composer/fish_dynamics.py`:

```python
def intrinsic_speed_factor(
    now: float,
    amplitude: float,
    freq_hz: float,
    phase: float,
    min_fraction: float,
) -> float:
    """Slow sine-based multiplier on a fish's cruise speed.

    Returns 1 + amplitude * sin(2 pi f t + phi), floored by `min_fraction`
    so fish never stall.
    """
    swing = 1.0 + amplitude * math.sin(_TWO_PI * freq_hz * now + phase)
    if swing < min_fraction:
        return min_fraction
    return swing
```

- [ ] **Step 4: Run; expect all eight to pass**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/fish_dynamics.py \
        extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py
git commit -m "feat(aquacast): add intrinsic_speed_factor helper"
```

---

### Task 3: `depth_attraction_strength`

Pure-math scalar in [-1, 1] indicating how strongly to pull a fish toward its preferred depth band.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/fish_dynamics.py`
- Modify: `extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py`

- [ ] **Step 1: Append failing tests**

```python
def test_depth_attraction_zero_inside_band_centre():
    # position_z == preferred_z => strength 0.
    s = fish_dynamics.depth_attraction_strength(
        position_z=5.0, preferred_z=5.0, band_half=1.0,
    )
    assert s == 0.0


def test_depth_attraction_pulls_up_when_below():
    # Fish at z=3, preferred z=5, half=1.0 => delta=+2, clipped to +1.
    s = fish_dynamics.depth_attraction_strength(
        position_z=3.0, preferred_z=5.0, band_half=1.0,
    )
    assert math.isclose(s, 1.0)


def test_depth_attraction_pulls_down_when_above():
    s = fish_dynamics.depth_attraction_strength(
        position_z=7.0, preferred_z=5.0, band_half=1.0,
    )
    assert math.isclose(s, -1.0)


def test_depth_attraction_linear_inside_band():
    # half = 2.0, fish 1.0 below preferred -> strength = 0.5.
    s = fish_dynamics.depth_attraction_strength(
        position_z=4.0, preferred_z=5.0, band_half=2.0,
    )
    assert math.isclose(s, 0.5)
```

- [ ] **Step 2: Run; expect four new failures**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: 8 pass, 4 fail.

- [ ] **Step 3: Implement the function**

Append to `fish_dynamics.py`:

```python
def depth_attraction_strength(
    position_z: float,
    preferred_z: float,
    band_half: float,
) -> float:
    """Signed strength (in [-1, 1]) of the pull toward the preferred depth.

    Positive means "pull up" (preferred is above current), negative means
    "pull down". Linear inside the band, clamped outside.
    """
    if band_half <= 1e-6:
        return 0.0
    delta = (preferred_z - position_z) / band_half
    if delta > 1.0:
        return 1.0
    if delta < -1.0:
        return -1.0
    return delta
```

- [ ] **Step 4: Run; expect all twelve to pass**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: `12 passed`.

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/fish_dynamics.py \
        extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py
git commit -m "feat(aquacast): add depth_attraction_strength helper"
```

---

### Task 4: `compute_target_roll`

Pure-math target roll angle (radians) from yaw rate, gains, and a clamp.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/fish_dynamics.py`
- Modify: `extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py`

- [ ] **Step 1: Append failing tests**

```python
def test_compute_target_roll_zero_when_no_yaw_change():
    assert fish_dynamics.compute_target_roll(
        yaw_rate=0.0, bank_gain=1.0, bank_gain_global=0.35,
        max_bank_radians=0.6,
    ) == 0.0


def test_compute_target_roll_sign_follows_yaw_rate():
    pos = fish_dynamics.compute_target_roll(
        yaw_rate=1.0, bank_gain=1.0, bank_gain_global=0.35,
        max_bank_radians=0.6,
    )
    neg = fish_dynamics.compute_target_roll(
        yaw_rate=-1.0, bank_gain=1.0, bank_gain_global=0.35,
        max_bank_radians=0.6,
    )
    assert pos > 0.0
    assert neg < 0.0
    assert math.isclose(pos, -neg)


def test_compute_target_roll_clamped_to_max():
    huge = fish_dynamics.compute_target_roll(
        yaw_rate=100.0, bank_gain=1.0, bank_gain_global=1.0,
        max_bank_radians=0.6,
    )
    assert math.isclose(huge, 0.6)


def test_compute_target_roll_clamped_to_negative_max():
    huge_neg = fish_dynamics.compute_target_roll(
        yaw_rate=-100.0, bank_gain=1.0, bank_gain_global=1.0,
        max_bank_radians=0.6,
    )
    assert math.isclose(huge_neg, -0.6)
```

- [ ] **Step 2: Run; expect four new failures**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: 12 pass, 4 fail.

- [ ] **Step 3: Implement the function**

Append to `fish_dynamics.py`:

```python
def compute_target_roll(
    yaw_rate: float,
    bank_gain: float,
    bank_gain_global: float,
    max_bank_radians: float,
) -> float:
    """Map a signed yaw rate (rad/s) to a target roll angle (radians)."""
    raw = yaw_rate * bank_gain * bank_gain_global
    if raw > max_bank_radians:
        return max_bank_radians
    if raw < -max_bank_radians:
        return -max_bank_radians
    return raw
```

- [ ] **Step 4: Run; expect all sixteen to pass**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: `16 passed`.

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/fish_dynamics.py \
        extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py
git commit -m "feat(aquacast): add compute_target_roll helper"
```

---

### Task 5: `sample_fish_traits`

Deterministic per-fish trait sampling driven by a stable string seed (name-based, not index-based). Returns a dict whose keys match what `_make_fish_state` will consume.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/fish_dynamics.py`
- Modify: `extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py`

- [ ] **Step 1: Append failing tests**

```python
# Single canonical range set used by these tests. The same dict shape will
# be built in main.py from global_variable.py constants at runtime.
_TEST_RANGES = {
    "cruise_speed_scale": (0.85, 1.15),
    "speed_noise_amplitude": (0.15, 0.35),
    "speed_noise_freq_hz": (0.05, 0.12),
    "depth_band_center_norm": (0.15, 0.85),
    "depth_band_half_width_norm": (0.08, 0.18),
    "vertical_wander_freq_hz": (0.07, 0.18),
    "bank_gain": (0.6, 1.0),
}


def test_sample_fish_traits_returns_all_expected_keys():
    traits = fish_dynamics.sample_fish_traits(
        prim_name="Fish_0", base_seed=1, ranges=_TEST_RANGES,
    )
    expected = set(_TEST_RANGES.keys()) | {"speed_noise_phase", "vertical_wander_phase"}
    assert set(traits.keys()) == expected


def test_sample_fish_traits_in_range():
    traits = fish_dynamics.sample_fish_traits(
        prim_name="Fish_0", base_seed=1, ranges=_TEST_RANGES,
    )
    for key, (low, high) in _TEST_RANGES.items():
        assert low <= traits[key] <= high, key
    # phases live in [0, 2*pi)
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
```

- [ ] **Step 2: Run; expect five new failures**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: 16 pass, 5 fail.

- [ ] **Step 3: Implement the function**

Append to `fish_dynamics.py`:

```python
import random


def sample_fish_traits(
    prim_name: str,
    base_seed: int,
    ranges: dict,
) -> dict:
    """Deterministically sample one fish's motion-dynamics traits.

    `prim_name` is the USD prim's name (e.g. "Fish_0"). It is combined with
    `base_seed` to seed a private `random.Random` instance so two fish with
    different names get different traits, and the same name reproduces the
    same traits across runs (Python's `random.Random` derives a stable hash
    from string seeds, independent of `PYTHONHASHSEED`).

    `ranges` keys: cruise_speed_scale, speed_noise_amplitude,
    speed_noise_freq_hz, depth_band_center_norm, depth_band_half_width_norm,
    vertical_wander_freq_hz, bank_gain. Each value is a (low, high) tuple.
    """
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
```

- [ ] **Step 4: Run; expect all 21 to pass**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: `21 passed`.

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/fish_dynamics.py \
        extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py
git commit -m "feat(aquacast): add sample_fish_traits with name-based deterministic seeding"
```

---

### Task 6: Add new configuration constants to `global_variable.py`

Pure config addition. No tests — this is a data file.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/global_variable.py`

- [ ] **Step 1: Append the new constants**

Append exactly this block to the end of `extensions/aquacast.aquacast_composer/global_variable.py`:

```python

# --- Realism dynamics (see docs/superpowers/specs/2026-05-20-fish-motion-realism-design.md) ---
ENABLE_REALISM_DYNAMICS = True

FISH_RNG_BASE_SEED = 1
FISH_MIN_SPEED_FRACTION = 0.4

FISH_CRUISE_SPEED_SCALE_RANGE = (0.85, 1.15)
FISH_SPEED_NOISE_AMPLITUDE_RANGE = (0.15, 0.35)
FISH_SPEED_NOISE_FREQ_HZ_RANGE = (0.05, 0.12)

FISH_DEPTH_BAND_CENTER_NORM_RANGE = (0.15, 0.85)
FISH_DEPTH_BAND_HALF_WIDTH_NORM_RANGE = (0.08, 0.18)
FISH_DEPTH_BAND_WEIGHT = 0.45

FISH_VERTICAL_WANDER_FREQ_HZ_RANGE = (0.07, 0.18)

FISH_BANK_GAIN_RANGE = (0.6, 1.0)
FISH_BANK_GAIN_GLOBAL = 0.35
FISH_MAX_BANK_RADIANS = 0.6
FISH_BANK_LERP_RATE = 3.0
```

- [ ] **Step 2: Verify the file parses as Python**

```bash
python -m py_compile extensions/aquacast.aquacast_composer/global_variable.py && echo OK
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add extensions/aquacast.aquacast_composer/global_variable.py
git commit -m "feat(aquacast): add realism-dynamics config constants"
```

---

### Task 7: Wire trait sampling into `_make_fish_state`

Import `fish_dynamics` from `main.py`, build the `ranges` dict from `global_variable.py` once at controller init, and gate the trait merge on `ENABLE_REALISM_DYNAMICS`.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

- [ ] **Step 1: Add `fish_dynamics` import**

Open `extensions/aquacast.aquacast_composer/main.py`. After line 8 (`from pathlib import Path`), add:

```python

import fish_dynamics  # sibling module; pure-math helpers for realism dynamics.
```

Then add the import of `fish_dynamics` to the same location. The block of imports near the top should now look like:

```python
import asyncio
import importlib.util
import json
import math
import re
import sys
import time
from pathlib import Path

import fish_dynamics  # sibling module; pure-math helpers for realism dynamics.

import carb
import omni.kit.app
import omni.usd
from pxr import Gf, Usd, UsdGeom
```

> Note: `main.py` is currently loaded via `importlib.util.spec_from_file_location` from `extension.py`, which sets `sys.modules[spec.name]` for `aquacast_extensions_main` but does not add `main.py`'s directory to `sys.path`. To make `import fish_dynamics` resolvable, we must add that directory in the next step.

- [ ] **Step 2: Make the sibling module discoverable**

Replace the import block we just added so that we register the directory on `sys.path` *before* the `import fish_dynamics` call. The block becomes:

```python
import asyncio
import importlib.util
import json
import math
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import fish_dynamics  # noqa: E402  sibling module; pure-math helpers.

import carb  # noqa: E402
import omni.kit.app  # noqa: E402
import omni.usd  # noqa: E402
from pxr import Gf, Usd, UsdGeom  # noqa: E402
```

- [ ] **Step 3: Add a helper that reads trait ranges from `global_variable.py`**

Find the `FishSwimController` class (starts around line 275 with `class FishSwimController`). Inside the class, immediately before `def _make_fish_state(self, prim, index):` (currently around line 444), insert this method:

```python
    def _get_trait_ranges(self):
        return {
            "cruise_speed_scale": tuple(get_global_config(
                "FISH_CRUISE_SPEED_SCALE_RANGE", (0.85, 1.15))),
            "speed_noise_amplitude": tuple(get_global_config(
                "FISH_SPEED_NOISE_AMPLITUDE_RANGE", (0.15, 0.35))),
            "speed_noise_freq_hz": tuple(get_global_config(
                "FISH_SPEED_NOISE_FREQ_HZ_RANGE", (0.05, 0.12))),
            "depth_band_center_norm": tuple(get_global_config(
                "FISH_DEPTH_BAND_CENTER_NORM_RANGE", (0.15, 0.85))),
            "depth_band_half_width_norm": tuple(get_global_config(
                "FISH_DEPTH_BAND_HALF_WIDTH_NORM_RANGE", (0.08, 0.18))),
            "vertical_wander_freq_hz": tuple(get_global_config(
                "FISH_VERTICAL_WANDER_FREQ_HZ_RANGE", (0.07, 0.18))),
            "bank_gain": tuple(get_global_config(
                "FISH_BANK_GAIN_RANGE", (0.6, 1.0))),
        }
```

- [ ] **Step 4: Extend `_make_fish_state` to merge in traits when the flag is on**

Locate the current `_make_fish_state` (around lines 444–457). Replace it with:

```python
    def _make_fish_state(self, prim, index):
        animation_prim = _get_animation_target_prim(prim)
        position = _xform_translation(animation_prim)
        angle = index * math.tau / max(1, 3)
        initial_direction = _normalized(Gf.Vec3d(-math.cos(angle), -math.sin(angle), 0.08 * math.sin(index + 1)))
        state = {
            "root_prim": prim,
            "prim": animation_prim,
            "position": self._clamp_position(position, initial_direction),
            "direction": initial_direction,
            "target_direction": initial_direction,
            "phase": index * 1.618,
            "head_length": self._estimate_head_length(animation_prim),
            # populated below when realism dynamics are enabled
            "prev_direction": initial_direction,
            "roll": 0.0,
        }

        if bool(get_global_config("ENABLE_REALISM_DYNAMICS", True)):
            base_seed = int(get_global_config("FISH_RNG_BASE_SEED", 1))
            traits = fish_dynamics.sample_fish_traits(
                prim_name=prim.GetName(),
                base_seed=base_seed,
                ranges=self._get_trait_ranges(),
            )
            state.update(traits)
            # Precompute the absolute preferred depth and half-width once.
            water_height = max(1e-6, self._water_max_z - self._water_min_z)
            state["preferred_z"] = (
                self._water_min_z + water_height * state["depth_band_center_norm"]
            )
            state["band_half"] = water_height * state["depth_band_half_width_norm"]

        return state
```

- [ ] **Step 5: Verify the file parses**

```bash
python -m py_compile extensions/aquacast.aquacast_composer/main.py && echo OK
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add extensions/aquacast.aquacast_composer/main.py
git commit -m "feat(aquacast): sample per-fish realism traits in _make_fish_state"
```

---

### Task 8: Replace constant speed with intrinsic speed in `_on_update`

Read the realism flag once per frame and switch between today's constant speed and the new per-fish intrinsic speed. Also capture `prev_direction` before rotating.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

- [ ] **Step 1: Replace `_on_update`'s per-fish loop**

Locate `_on_update` (currently around lines 474–500). The relevant lines are the per-fish loop at the bottom. Replace the entire `_on_update` method with:

```python
    def _on_update(self, _event):
        if not bool(get_global_config("ENABLE_FISH_SWIMMING", False)):
            return
        if not self._initialized:
            now = time.monotonic()
            if now >= self._next_init_retry_time:
                self.initialize()
            return

        now = time.monotonic()
        dt = 1.0 / 60.0 if self._last_update_time is None else min(0.05, max(0.0, now - self._last_update_time))
        self._last_update_time = now
        if dt <= 0.0:
            return

        realism_on = bool(get_global_config("ENABLE_REALISM_DYNAMICS", True))
        base_speed = self._water_radius * float(get_global_config(
            "FISH_SWIM_SPEED_RADIUS_PER_SECOND", 0.12))
        min_speed_fraction = float(get_global_config("FISH_MIN_SPEED_FRACTION", 0.4))
        direction_lerp_rate = float(get_global_config("FISH_DIRECTION_LERP_RATE", 4.0))
        direction_lerp_t = _lerp_alpha(direction_lerp_rate, dt)
        max_turn = float(get_global_config("FISH_MAX_TURN_RADIANS_PER_SECOND", 1.8)) * dt

        for fish in self._fish:
            desired = self._desired_direction(fish, now, realism_on)
            fish["target_direction"] = _lerp_direction(fish["target_direction"], desired, direction_lerp_t)
            fish["prev_direction"] = fish["direction"]
            fish["direction"] = _rotate_toward(fish["direction"], fish["target_direction"], max_turn)

            if realism_on:
                factor = fish_dynamics.intrinsic_speed_factor(
                    now=now,
                    amplitude=fish["speed_noise_amplitude"],
                    freq_hz=fish["speed_noise_freq_hz"],
                    phase=fish["speed_noise_phase"],
                    min_fraction=min_speed_fraction,
                )
                speed = base_speed * fish["cruise_speed_scale"] * factor
            else:
                speed = base_speed

            next_position = fish["position"] + fish["direction"] * speed * dt
            fish["position"] = self._clamp_position(next_position, fish["direction"], fish["head_length"])
            _set_fish_transform(fish["prim"], fish["position"], fish["direction"],
                                fish=fish, dt=dt, realism_on=realism_on)
```

> Note: `_desired_direction` and `_set_fish_transform` now take additional arguments. They are updated in Tasks 9 and 10. Until then, the module will not import; commit only after Task 10 in this group. Mark this task complete after Step 1; the parse/launch checks are deferred to Task 10's verification.

- [ ] **Step 2: Confirm the file still tokenises (a non-import compile check would fail because the helpers below don't have the new signatures yet)**

We intentionally skip `py_compile` here because Python compiles fine; we just don't want to launch Kit yet until Tasks 9 and 10 also land. Continue to Task 9 without committing.

---

### Task 9: Add depth attraction + decorrelated vertical wander in `_desired_direction` and `_wander_vector`

`_desired_direction` gains a `realism_on` parameter and (when on) adds a depth-attraction term. `_wander_vector` gains the same parameter and substitutes a per-fish decorrelated vertical sine for the shared one.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

- [ ] **Step 1: Replace `_desired_direction`**

Locate `_desired_direction` (currently around lines 502–535). Replace the entire method with:

```python
    def _desired_direction(self, fish, now, realism_on=True):
        position = fish["position"]
        direction = fish["direction"]

        separation = Gf.Vec3d(0.0, 0.0, 0.0)
        alignment = Gf.Vec3d(0.0, 0.0, 0.0)
        cohesion_center = Gf.Vec3d(0.0, 0.0, 0.0)
        neighbor_count = 0
        separation_radius = self._water_radius * float(get_global_config("FISH_SEPARATION_RADIUS_RATIO", 0.18))

        for other in self._fish:
            if other is fish:
                continue
            offset = position - other["position"]
            distance = _length(offset)
            if distance <= 1e-6 or distance > separation_radius:
                continue
            separation += _normalized(offset) * (1.0 - distance / separation_radius)
            alignment += other["direction"]
            cohesion_center += other["position"]
            neighbor_count += 1

        flock = Gf.Vec3d(0.0, 0.0, 0.0)
        if neighbor_count:
            cohesion = _normalized((cohesion_center / neighbor_count) - position, direction)
            alignment = _normalized(alignment / neighbor_count, direction)
            separation = _normalized(separation, direction)
            flock += cohesion * float(get_global_config("FISH_COHESION_WEIGHT", 0.18))
            flock += alignment * float(get_global_config("FISH_ALIGNMENT_WEIGHT", 0.25))
            flock += separation * float(get_global_config("FISH_SEPARATION_WEIGHT", 0.42))

        wander = self._wander_vector(fish, now, realism_on) * float(get_global_config("FISH_WANDER_WEIGHT", 0.20))
        boundary = self._boundary_steering(fish) * float(get_global_config("FISH_BOUNDARY_WEIGHT", 1.35))

        depth = Gf.Vec3d(0.0, 0.0, 0.0)
        if realism_on and "preferred_z" in fish:
            strength = fish_dynamics.depth_attraction_strength(
                position_z=fish["position"][2],
                preferred_z=fish["preferred_z"],
                band_half=fish["band_half"],
            )
            depth = Gf.Vec3d(0.0, 0.0, strength) * float(get_global_config(
                "FISH_DEPTH_BAND_WEIGHT", 0.45))

        return _normalized(direction + flock + wander + boundary + depth, direction)
```

- [ ] **Step 2: Replace `_wander_vector`**

Locate `_wander_vector` (currently around lines 537–541). Replace with:

```python
    def _wander_vector(self, fish, now, realism_on=True):
        phase = fish["phase"]
        horizontal = Gf.Vec3d(math.cos(now * 0.7 + phase), math.sin(now * 0.9 + phase * 1.7), 0.0)
        if realism_on and "vertical_wander_freq_hz" in fish:
            vertical_z = math.sin(
                2.0 * math.pi * fish["vertical_wander_freq_hz"] * now
                + fish["vertical_wander_phase"]
            )
        else:
            vertical_z = math.sin(now * 0.55 + phase)
        vertical = Gf.Vec3d(0.0, 0.0, vertical_z)
        return _normalized(horizontal + vertical * float(get_global_config("FISH_VERTICAL_WANDER_WEIGHT", 0.12)))
```

- [ ] **Step 3: Continue to Task 10 (still no commit yet — the file does not parse cleanly until `_set_fish_transform` is updated)**

---

### Task 10: Banking via `_compute_orientation`; rewire `_set_fish_transform`

Replace the module-level `_local_direction_to_rotate_xyz` with `_compute_orientation`, which returns roll-pitch-yaw degrees. Update `_set_fish_transform` to accept the optional `fish`, `dt`, and `realism_on` arguments and use the new orientation function when realism is on.

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

- [ ] **Step 1: Add `_compute_orientation` next to `_local_direction_to_rotate_xyz`**

Locate `_local_direction_to_rotate_xyz` (currently around lines 197–202). Leave the existing function in place (it is still used in the legacy code path) and add this new function immediately after it:

```python
def _compute_orientation(direction, fish, prev_direction, dt):
    """Return a Gf.Vec3f(roll, pitch, yaw) in degrees with banking applied.

    Pitch and yaw are computed the same way as `_local_direction_to_rotate_xyz`.
    Roll is derived from yaw rate: when the fish is turning sharply, it banks
    into the turn. The roll is low-passed via `_lerp_alpha` so it lags the
    yaw, and is clamped to FISH_MAX_BANK_RADIANS.
    """
    direction = _normalized(direction)
    horizontal = math.sqrt(direction[0] * direction[0] + direction[1] * direction[1])
    yaw = math.degrees(math.atan2(-direction[1], -direction[0]))
    pitch = math.degrees(math.atan2(direction[2], max(horizontal, 1e-6)))

    cur_yaw = fish_dynamics.yaw_from_direction(direction[0], direction[1])
    prev_yaw = fish_dynamics.yaw_from_direction(prev_direction[0], prev_direction[1])
    yaw_delta = fish_dynamics.wrap_to_pi(cur_yaw - prev_yaw)
    yaw_rate = yaw_delta / max(dt, 1e-4)

    target_roll = fish_dynamics.compute_target_roll(
        yaw_rate=yaw_rate,
        bank_gain=fish.get("bank_gain", 0.0),
        bank_gain_global=float(get_global_config("FISH_BANK_GAIN_GLOBAL", 0.35)),
        max_bank_radians=float(get_global_config("FISH_MAX_BANK_RADIANS", 0.6)),
    )
    bank_lerp = _lerp_alpha(
        float(get_global_config("FISH_BANK_LERP_RATE", 3.0)),
        dt,
    )
    current_roll = float(fish.get("roll", 0.0))
    new_roll = current_roll + (target_roll - current_roll) * bank_lerp
    fish["roll"] = new_roll

    return Gf.Vec3f(math.degrees(new_roll), float(pitch), float(yaw))
```

- [ ] **Step 2: Update `_set_fish_transform` signature and body**

Locate `_set_fish_transform` (currently around lines 251–272). Replace with:

```python
def _set_fish_transform(prim, position, direction, *, fish=None, dt=None, realism_on=False):
    stage = prim.GetStage()
    previous_edit_target = stage.GetEditTarget() if stage else None
    if stage and stage.GetSessionLayer():
        stage.SetEditTarget(stage.GetSessionLayer())

    xformable = UsdGeom.Xformable(prim)
    try:
        translate_op = _find_xform_op(xformable, UsdGeom.XformOp.TypeTranslate)
        if translate_op is None:
            translate_op = xformable.AddTranslateOp(precision=UsdGeom.XformOp.PrecisionDouble)
        translate_op.Set(_world_to_parent_local_position(prim, position))

        rotate_op = _find_xform_op(xformable, UsdGeom.XformOp.TypeRotateXYZ)
        if rotate_op is None:
            rotate_op = xformable.AddRotateXYZOp(precision=UsdGeom.XformOp.PrecisionFloat)
        _set_compatible_fish_xform_order(xformable, translate_op, rotate_op)
        local_direction = _world_to_parent_local_direction(prim, direction)
        if realism_on and fish is not None and dt is not None:
            prev_direction = fish.get("prev_direction", direction)
            local_prev = _world_to_parent_local_direction(prim, prev_direction)
            rotate_op.Set(_compute_orientation(local_direction, fish, local_prev, dt))
        else:
            rotate_op.Set(_local_direction_to_rotate_xyz(local_direction))
    finally:
        if stage and previous_edit_target is not None:
            stage.SetEditTarget(previous_edit_target)
```

- [ ] **Step 3: Confirm the whole `main.py` parses**

```bash
python -m py_compile extensions/aquacast.aquacast_composer/main.py && echo OK
```

Expected: `OK`.

- [ ] **Step 4: Re-run pure-math tests to confirm they still pass (they should — fish_dynamics didn't change)**

```bash
python -m pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v
```

Expected: `21 passed`.

- [ ] **Step 5: Single commit covering Tasks 8–10**

The intermediate states between Tasks 8, 9 and 10 do not run because helper signatures change. Bundle them into one commit:

```bash
git add extensions/aquacast.aquacast_composer/main.py
git commit -m "feat(aquacast): per-fish speed, depth attraction, and banking in FishSwimController"
```

---

### Task 11: Manual verification in Omniverse Kit

This is the binding correctness check. There is no automated visual test; you must launch the app and watch the fish.

**Files:** none — read-only verification.

- [ ] **Step 1: Confirm `ENABLE_REALISM_DYNAMICS = True` in `global_variable.py`**

```bash
grep -n ENABLE_REALISM_DYNAMICS extensions/aquacast.aquacast_composer/global_variable.py
```

Expected: one line showing `ENABLE_REALISM_DYNAMICS = True`.

- [ ] **Step 2: Launch in composer mode**

```bash
./start_aquacast.sh --composer
```

Wait for the viewport to load with the tank scene.

- [ ] **Step 3: Visual check — individuality (≥30 s of observation)**

Watch the fish and confirm by eye:
- Fish occupy visibly different depths (some hang higher in the tank, some lower).
- Fish bob with visibly different rhythms (not in lockstep).
- Fish move at visibly different speeds at any given moment.

If all three are true, this step passes.

- [ ] **Step 4: Visual check — banking**

Watch when a fish executes a sharp turn near the tank wall:
- The fish rolls into the turn (its body tilts toward the inside of the curve).
- During straight cruising, roll returns toward zero (no permanent list).

If the roll appears in the *wrong* direction (fish rolls *away* from the turn), record the issue. Mitigation: in `extensions/aquacast.aquacast_composer/global_variable.py`, change `FISH_BANK_GAIN_GLOBAL = 0.35` to `FISH_BANK_GAIN_GLOBAL = -0.35`, relaunch, and re-verify.

- [ ] **Step 5: Check the carb log for new warnings/errors**

The log is typically printed to stdout from the launch script. Look for any `[Error]`, `[Warning]`, or stack traces that mention `aquacast`, `fish`, or `FishSwimController` that were not present before this change. Expected: none.

- [ ] **Step 6: A/B test the legacy path**

Stop the running app (Ctrl-C in the launch terminal). Edit `extensions/aquacast.aquacast_composer/global_variable.py` and set:

```python
ENABLE_REALISM_DYNAMICS = False
```

Relaunch:

```bash
./start_aquacast.sh --composer
```

Confirm:
- Fish look like the pre-change behavior: uniform speed, no banking, choreographed bob.
- No errors related to missing `preferred_z`, `bank_gain`, or other realism-only keys.

Restore the file:

```bash
sed -i 's/ENABLE_REALISM_DYNAMICS = False/ENABLE_REALISM_DYNAMICS = True/' \
    extensions/aquacast.aquacast_composer/global_variable.py
```

- [ ] **Step 7: Seed variation test**

Edit `extensions/aquacast.aquacast_composer/global_variable.py` and change `FISH_RNG_BASE_SEED = 1` to `FISH_RNG_BASE_SEED = 42`. Relaunch and confirm the fish look distinctly *different* from the previous launch (different depth distribution, different rhythms). Restore the seed to `1` afterwards:

```bash
sed -i 's/FISH_RNG_BASE_SEED = 42/FISH_RNG_BASE_SEED = 1/' \
    extensions/aquacast.aquacast_composer/global_variable.py
```

- [ ] **Step 8: Commit any tuning adjustments**

If you flipped `FISH_BANK_GAIN_GLOBAL` or made any other tuning changes in Step 4, commit them:

```bash
git add extensions/aquacast.aquacast_composer/global_variable.py
git commit -m "tune(aquacast): adjust realism-dynamics tuning after visual verification"
```

If no tuning was needed, this step is a no-op.

---

## Self-Review checklist (already applied)

**Spec coverage.** Every section of the design spec has a task:

| Spec section | Plan task(s) |
|--------------|--------------|
| §5.1 Per-fish trait sampling | Task 5 (helper), Task 7 (integration) |
| §5.2 Intrinsic speed variation | Task 2 (helper), Task 8 (integration) |
| §5.3 Depth behavior | Task 3 (attraction helper), Task 9 (integration + decorrelated wander) |
| §5.4 Banking | Tasks 1, 4 (helpers), Task 10 (integration) |
| §5.5 `_on_update` composition | Task 8 |
| §6 New globals | Task 6 |
| §7 Backward compatibility / flag dispatch | Tasks 7, 8, 9, 10 (flag read once in `_on_update`, propagated as `realism_on` arg) |
| §8 Determinism | Task 5 (string-seeded `random.Random`) |
| §9 Verification | Task 11 |

**Placeholder scan.** No "TBD", no "implement appropriate error handling", no "similar to Task N". Each code step contains the actual code.

**Type consistency.** Function names and signatures match across tasks:
- `sample_fish_traits(prim_name, base_seed, ranges)` defined in Task 5, called the same way in Task 7.
- `intrinsic_speed_factor(now, amplitude, freq_hz, phase, min_fraction)` consistent across Task 2 and Task 8.
- `depth_attraction_strength(position_z, preferred_z, band_half)` consistent across Task 3 and Task 9.
- `compute_target_roll(yaw_rate, bank_gain, bank_gain_global, max_bank_radians)` consistent across Task 4 and Task 10.
- `_compute_orientation(direction, fish, prev_direction, dt)` and `_set_fish_transform(..., fish=, dt=, realism_on=)` consistent across Tasks 10 and 8.
- Trait dict keys (`cruise_speed_scale`, `speed_noise_amplitude`, `speed_noise_freq_hz`, `speed_noise_phase`, `depth_band_center_norm`, `depth_band_half_width_norm`, `vertical_wander_freq_hz`, `vertical_wander_phase`, `bank_gain`) are produced in Task 5 and consumed identically in Tasks 7, 8, 9, 10.

**Risk acknowledgement.** The banking sign is a known unknown (spec §10). Task 11 Step 4 explicitly tells the engineer how to flip it if needed and where to commit the tuning.
