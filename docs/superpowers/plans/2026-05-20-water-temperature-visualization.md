# Water Temperature Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live water-temperature simulation and color visualization on the `Isosurface` prim of the indoor RAS tank, with a Kit menu toggle to switch inflow ON/OFF during simulation. See `docs/superpowers/specs/2026-05-20-water-temperature-visualization-design.md`.

**Architecture:** A new `WaterTempController` singleton in `main.py` runs a closed-form Newton lumped heat balance per frame (`thermal_dynamics.py` pure-math module), maps the current temperature to RGB via a piecewise-linear color ramp, and writes the result to `primvars:displayColor` on the Isosurface prim through the USD session layer. The `extension.py` adds the checkable `Aquacast > Water Inflow` menu item whose `ticked_fn`/`onclick_fn` talk to the controller.

**Tech Stack:** Python 3, USD (`pxr.Usd`, `pxr.UsdGeom`, `pxr.Sdf`, `pxr.Vt`, `pxr.Gf`), Omniverse Kit (`omni.kit.app`, `omni.usd`, `omni.kit.menu.utils`), `carb`, pytest (pure-math tests only).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `extensions/aquacast.aquacast_composer/thermal_dynamics.py` | **Create** | Pure-math: `step_temperature`, `equilibrium_temperature`, `temperature_to_rgb`. Zero Omniverse imports. |
| `extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py` | **Create** | Plain pytest unit tests for the three functions above. Mirrors `tests/test_fish_dynamics.py`. |
| `extensions/aquacast.aquacast_composer/global_variable.py` | **Modify** | Append water-temperature configuration block (toggle, thermodynamic constants, color stops, log interval, prim path). |
| `extensions/aquacast.aquacast_composer/main.py` | **Modify** | Add `_water_temp_controller` singleton, `start/stop_water_temp_controller`, menu-callback module functions, and the `WaterTempController` class itself. |
| `extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/extension.py` | **Modify** | Start/stop the new controller alongside the fish controller; register `Aquacast > Water Inflow` checkable menu item; clean up on shutdown. |

Both files in `main.py` and `extension.py` follow patterns already established by `FishSwimController` and the Help menu registration (`extension.py:210-217`).

---

## Pre-flight check

- [ ] **Confirm pytest works for the existing module**

Run: `pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py -v`
Expected: All tests pass. If this fails, fix the environment before continuing — the new tests use the same harness.

---

## Task 1: `thermal_dynamics.step_temperature`

**Files:**
- Create: `extensions/aquacast.aquacast_composer/thermal_dynamics.py`
- Create: `extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py`

- [ ] **Step 1: Write the failing tests**

Create `extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py`:

```python
"""Plain pytest unit tests for thermal_dynamics pure-math helpers."""

import math
import sys
from pathlib import Path


EXTENSION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXTENSION_ROOT))

import thermal_dynamics  # noqa: E402


# Reused defaults that match global_variable.py choices.
T_ROOM = 22.0
T_INLET = 14.0
K_ROOM = 0.012
K_INFLOW = 0.022


def _equilibrium(k_room, k_inflow, T_room, T_inlet, inflow_enabled):
    k_inflow_eff = k_inflow if inflow_enabled else 0.0
    b = k_room + k_inflow_eff
    if b == 0.0:
        return None
    return (k_room * T_room + k_inflow_eff * T_inlet) / b


def test_step_no_heat_transfer_keeps_temp():
    # Both coefficients zero -> no change regardless of dt.
    result = thermal_dynamics.step_temperature(
        15.0, 100.0,
        T_room=T_ROOM, T_inlet=T_INLET,
        k_room=0.0, k_inflow=0.0,
        inflow_enabled=True,
    )
    assert result == 15.0


def test_step_inflow_off_zero_k_inflow_acts_like_room_only():
    # inflow_enabled=False must equal k_inflow=0 with inflow_enabled=True.
    a = thermal_dynamics.step_temperature(
        14.0, 5.0,
        T_room=T_ROOM, T_inlet=T_INLET,
        k_room=K_ROOM, k_inflow=K_INFLOW,
        inflow_enabled=False,
    )
    b = thermal_dynamics.step_temperature(
        14.0, 5.0,
        T_room=T_ROOM, T_inlet=T_INLET,
        k_room=K_ROOM, k_inflow=0.0,
        inflow_enabled=True,
    )
    assert math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def test_step_inflow_off_approaches_room_temp():
    T = 14.0
    for _ in range(2000):
        T = thermal_dynamics.step_temperature(
            T, 1.0,
            T_room=T_ROOM, T_inlet=T_INLET,
            k_room=K_ROOM, k_inflow=K_INFLOW,
            inflow_enabled=False,
        )
    assert math.isclose(T, T_ROOM, abs_tol=1e-3)


def test_step_inflow_on_approaches_mix_equilibrium():
    expected_eq = _equilibrium(K_ROOM, K_INFLOW, T_ROOM, T_INLET, True)
    T = 14.0
    for _ in range(2000):
        T = thermal_dynamics.step_temperature(
            T, 1.0,
            T_room=T_ROOM, T_inlet=T_INLET,
            k_room=K_ROOM, k_inflow=K_INFLOW,
            inflow_enabled=True,
        )
    assert math.isclose(T, expected_eq, abs_tol=1e-3)


def test_step_is_stable_for_large_dt():
    # dt=10s with k=0.034 means b*dt = 0.34, well-defined; exp closed form must
    # produce a value monotonically between T and T_eq, never overshooting.
    eq = _equilibrium(K_ROOM, K_INFLOW, T_ROOM, T_INLET, True)
    T_prev = 14.0
    for _ in range(50):
        T_next = thermal_dynamics.step_temperature(
            T_prev, 10.0,
            T_room=T_ROOM, T_inlet=T_INLET,
            k_room=K_ROOM, k_inflow=K_INFLOW,
            inflow_enabled=True,
        )
        assert T_prev <= T_next <= eq + 1e-9
        T_prev = T_next


def test_step_continuous_at_toggle_boundary():
    # Same starting T, the first step after toggling ON->OFF must not jump:
    # it must equal one step computed directly with inflow_enabled=False.
    T_now = 16.0
    direct_off = thermal_dynamics.step_temperature(
        T_now, 0.5,
        T_room=T_ROOM, T_inlet=T_INLET,
        k_room=K_ROOM, k_inflow=K_INFLOW,
        inflow_enabled=False,
    )
    assert math.isfinite(direct_off)
    # And from the new state another step must be smooth (delta on first step
    # is roughly proportional to (T_room - T_now) * k_room * dt).
    expected_delta = (T_ROOM - T_now) * K_ROOM * 0.5
    actual_delta = direct_off - T_now
    assert math.isclose(actual_delta, expected_delta, rel_tol=0.05)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py -v`
Expected: All six tests FAIL with `ModuleNotFoundError: No module named 'thermal_dynamics'`.

- [ ] **Step 3: Implement `step_temperature`**

Create `extensions/aquacast.aquacast_composer/thermal_dynamics.py`:

```python
"""Pure-math helpers for the water temperature visualization.

Zero Omniverse / USD imports. Safe to unit-test under plain pytest.
"""

from __future__ import annotations

import math


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
    """Newton lumped heat balance with optional inflow term.

    Models a single bulk temperature whose dynamics are:

        dT/dt = k_room * (T_room - T) + k_inflow_eff * (T_inlet - T)

    where k_inflow_eff = k_inflow if inflow_enabled else 0.0.

    Returns T after stepping forward by `dt` seconds using the closed-form
    exact solution. Stable for arbitrary non-negative dt.
    """
    k_inflow_eff = k_inflow if inflow_enabled else 0.0
    b = k_room + k_inflow_eff
    if b <= 0.0 or dt <= 0.0:
        return T
    T_eq = (k_room * T_room + k_inflow_eff * T_inlet) / b
    return T_eq + (T - T_eq) * math.exp(-b * dt)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py -v`
Expected: All six tests PASS.

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/thermal_dynamics.py extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py
git commit -m "feat(aquacast): add thermal_dynamics.step_temperature with closed-form Newton heat balance"
```

---

## Task 2: `thermal_dynamics.equilibrium_temperature`

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/thermal_dynamics.py`
- Modify: `extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py`

- [ ] **Step 1: Write the failing tests**

Append to `extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py`:

```python
def test_equilibrium_inflow_on_matches_formula():
    expected = (K_ROOM * T_ROOM + K_INFLOW * T_INLET) / (K_ROOM + K_INFLOW)
    got = thermal_dynamics.equilibrium_temperature(
        T_room=T_ROOM, T_inlet=T_INLET,
        k_room=K_ROOM, k_inflow=K_INFLOW,
        inflow_enabled=True,
    )
    assert math.isclose(got, expected)


def test_equilibrium_inflow_off_is_t_room():
    got = thermal_dynamics.equilibrium_temperature(
        T_room=T_ROOM, T_inlet=T_INLET,
        k_room=K_ROOM, k_inflow=K_INFLOW,
        inflow_enabled=False,
    )
    assert math.isclose(got, T_ROOM)


def test_equilibrium_returns_none_when_no_transfer():
    assert thermal_dynamics.equilibrium_temperature(
        T_room=T_ROOM, T_inlet=T_INLET,
        k_room=0.0, k_inflow=0.0,
        inflow_enabled=True,
    ) is None
    # Same when inflow is off and k_room is zero.
    assert thermal_dynamics.equilibrium_temperature(
        T_room=T_ROOM, T_inlet=T_INLET,
        k_room=0.0, k_inflow=K_INFLOW,
        inflow_enabled=False,
    ) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py -v -k equilibrium`
Expected: Three new tests FAIL with `AttributeError: module 'thermal_dynamics' has no attribute 'equilibrium_temperature'`.

- [ ] **Step 3: Implement `equilibrium_temperature`**

Append to `extensions/aquacast.aquacast_composer/thermal_dynamics.py`:

```python
def equilibrium_temperature(
    *,
    T_room: float,
    T_inlet: float,
    k_room: float,
    k_inflow: float,
    inflow_enabled: bool,
) -> float | None:
    """Asymptotic equilibrium of the heat balance ODE.

    Returns None when no heat exchange occurs (b == 0).
    """
    k_inflow_eff = k_inflow if inflow_enabled else 0.0
    b = k_room + k_inflow_eff
    if b <= 0.0:
        return None
    return (k_room * T_room + k_inflow_eff * T_inlet) / b
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py -v`
Expected: All tests PASS (9 total now).

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/thermal_dynamics.py extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py
git commit -m "feat(aquacast): add thermal_dynamics.equilibrium_temperature"
```

---

## Task 3: `thermal_dynamics.temperature_to_rgb`

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/thermal_dynamics.py`
- Modify: `extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py`

- [ ] **Step 1: Write the failing tests**

Append to `extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py`:

```python
_STOPS = [
    (10.0, (0.05, 0.25, 1.00)),
    (14.0, (0.00, 0.75, 0.75)),
    (18.0, (0.90, 0.55, 0.20)),
    (25.0, (1.00, 0.12, 0.12)),
]


def _close_rgb(a, b, tol=1e-9):
    return all(math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b))


def test_color_clamps_below_first_stop():
    assert _close_rgb(thermal_dynamics.temperature_to_rgb(5.0, _STOPS), _STOPS[0][1])
    assert _close_rgb(thermal_dynamics.temperature_to_rgb(10.0, _STOPS), _STOPS[0][1])


def test_color_clamps_above_last_stop():
    assert _close_rgb(thermal_dynamics.temperature_to_rgb(30.0, _STOPS), _STOPS[-1][1])
    assert _close_rgb(thermal_dynamics.temperature_to_rgb(25.0, _STOPS), _STOPS[-1][1])


def test_color_at_exact_stop_returns_stop_color():
    for temp, color in _STOPS:
        assert _close_rgb(thermal_dynamics.temperature_to_rgb(temp, _STOPS), color)


def test_color_lerp_at_midpoint_between_14_and_18():
    # midpoint between (14, c14) and (18, c18) -> 0.5 lerp componentwise.
    c14 = _STOPS[1][1]
    c18 = _STOPS[2][1]
    expected = tuple(0.5 * (a + b) for a, b in zip(c14, c18))
    assert _close_rgb(thermal_dynamics.temperature_to_rgb(16.0, _STOPS), expected)


def test_color_stops_unsorted_input_yields_same_result():
    shuffled = [_STOPS[2], _STOPS[0], _STOPS[3], _STOPS[1]]
    for T in (5.0, 10.0, 12.0, 14.0, 16.0, 18.0, 22.0, 30.0):
        a = thermal_dynamics.temperature_to_rgb(T, _STOPS)
        b = thermal_dynamics.temperature_to_rgb(T, shuffled)
        assert _close_rgb(a, b)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py -v -k color`
Expected: Five new tests FAIL with `AttributeError: module 'thermal_dynamics' has no attribute 'temperature_to_rgb'`.

- [ ] **Step 3: Implement `temperature_to_rgb`**

Append to `extensions/aquacast.aquacast_composer/thermal_dynamics.py`:

```python
def temperature_to_rgb(
    T: float,
    stops: list[tuple[float, tuple[float, float, float]]],
) -> tuple[float, float, float]:
    """Piecewise-linear color ramp lookup.

    `stops` is a list of (temperature, (r, g, b)) pairs in any order. Returns
    the linear RGB triple in [0,1]^3 corresponding to T. Clamps at endpoints.
    """
    if not stops:
        return (0.0, 0.0, 0.0)
    ordered = sorted(stops, key=lambda s: s[0])
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py -v`
Expected: All 14 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/thermal_dynamics.py extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py
git commit -m "feat(aquacast): add thermal_dynamics.temperature_to_rgb piecewise color ramp"
```

---

## Task 4: Add configuration block to `global_variable.py`

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/global_variable.py`

- [ ] **Step 1: Append the configuration block**

Append the following lines at the end of `extensions/aquacast.aquacast_composer/global_variable.py` (exactly as written; keep the trailing newline):

```python

# ── Water temperature visualization ────────────────────────────────────────
ENABLE_WATER_TEMP_VIS = True

# Target prim (3-tier resolution like fish)
ISOSURFACE_PRIM_PATH = "/Root/Aquarium/AquariumComponents/FishTank/InWater/Components/ParticleSystem/Isosurface"
TEMP_VIS_USE_STAGE_TOPOLOGY_JSON = True
TEMP_VIS_INIT_RETRY_SECONDS = 1.0

# Thermodynamics (units: °C, 1/s)
INITIAL_WATER_TEMP_C   = 14.0
INLET_WATER_TEMP_C     = 14.0
ROOM_TEMP_C            = 22.0
THERMAL_K_ROOM         = 0.012   # OFF: τ ≈ 83 s, asymptote = T_room
THERMAL_K_INFLOW       = 0.022   # ON:  τ ≈ 29 s, asymptote ≈ 16.82 °C
INFLOW_ENABLED_DEFAULT = True

# Color ramp (linear RGB in [0,1])
TEMP_COLOR_STOPS = [
    (10.0, (0.05, 0.25, 1.00)),
    (14.0, (0.00, 0.75, 0.75)),
    (18.0, (0.90, 0.55, 0.20)),
    (25.0, (1.00, 0.12, 0.12)),
]

# Diagnostic logging
TEMP_VIS_LOG_INTERVAL_SECONDS = 5.0
```

- [ ] **Step 2: Verify the file still parses**

Run: `python -c "import importlib.util, sys; sys.dont_write_bytecode = True; spec = importlib.util.spec_from_file_location('cfg', 'extensions/aquacast.aquacast_composer/global_variable.py'); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.ENABLE_WATER_TEMP_VIS, m.ROOM_TEMP_C, len(m.TEMP_COLOR_STOPS))"`
Expected: Prints `True 22.0 4`.

- [ ] **Step 3: Commit**

```bash
git add extensions/aquacast.aquacast_composer/global_variable.py
git commit -m "feat(aquacast): add water temperature visualization config block"
```

---

## Task 5: Singleton lifecycle and `WaterTempController` skeleton

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

This task creates the singleton, lifecycle functions, and a skeleton controller class. No USD writes, no temperature math integration yet — only event subscriptions and logging stubs.

- [ ] **Step 1: Add singleton + module-level start/stop functions**

In `extensions/aquacast.aquacast_composer/main.py`, find the section near the existing fish-controller singleton (around line 22). Add a third singleton variable next to `_fish_swim_controller`:

```python
_stage_structure_cache = None
_fish_swim_controller = None
_water_temp_controller = None
```

Add the new start/stop functions immediately after `stop_fish_swim_controller` (around line 82):

```python
def start_water_temp_controller():
    global _water_temp_controller
    if _water_temp_controller is None:
        if not bool(get_global_config("ENABLE_WATER_TEMP_VIS", False)):
            return None
        _water_temp_controller = WaterTempController()
        _water_temp_controller.start()
    return _water_temp_controller


def stop_water_temp_controller():
    global _water_temp_controller
    if _water_temp_controller is not None:
        _water_temp_controller.stop()
        _water_temp_controller = None
```

- [ ] **Step 2: Add menu-callback module functions**

Add these after `stop_water_temp_controller` (used by `extension.py` later for the menu item):

```python
def water_temp_controller_inflow_state() -> bool:
    if _water_temp_controller is None:
        return False
    return _water_temp_controller.is_inflow_enabled()


def toggle_water_temp_controller_inflow() -> None:
    if _water_temp_controller is None:
        return
    _water_temp_controller.toggle_inflow()
```

- [ ] **Step 3: Import `thermal_dynamics` at the top of `main.py`**

Find the existing `import fish_dynamics  # noqa: E402` line (line 14). Add immediately below it:

```python
import thermal_dynamics  # noqa: E402
```

- [ ] **Step 4: Add the skeleton `WaterTempController` class**

Append the class at the **end of `main.py`** (after `StageStructureCache`):

```python
class WaterTempController:
    """Drive a single bulk water temperature and color the Isosurface prim."""

    def __init__(self):
        self._stage_event_sub = None
        self._update_sub = None
        self._initialized = False
        self._isosurface_prim = None
        self._display_color_attr = None
        self._T = float(get_global_config("INITIAL_WATER_TEMP_C", 14.0))
        self._inflow_enabled = bool(get_global_config("INFLOW_ENABLED_DEFAULT", True))
        _inlet = float(get_global_config("INLET_WATER_TEMP_C", 14.0))
        _room = float(get_global_config("ROOM_TEMP_C", 22.0))
        if _inlet > _room:
            carb.log_warn(
                f"[Aquacast Temp] INLET_WATER_TEMP_C ({_inlet}) > "
                f"ROOM_TEMP_C ({_room}); inflow will heat rather than cool"
            )
        self._last_update_time = None
        self._last_log_time = 0.0
        self._next_init_retry_time = 0.0
        self._warned_missing_isosurface = False
        self._color_stops_cached = None
        self._color_stops_sorted = None
        self._prev_rgb = None

    def start(self):
        usd_context = omni.usd.get_context()
        self._stage_event_sub = usd_context.get_stage_event_stream().create_subscription_to_pop(
            self._on_stage_event,
            name="aquacast_water_temp_stage",
        )
        self._update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update,
            name="aquacast_water_temp_update",
        )
        asyncio.ensure_future(self._initialize_after_frames(3))

    def stop(self):
        self._stage_event_sub = None
        self._update_sub = None
        self._isosurface_prim = None
        self._display_color_attr = None
        self._initialized = False

    async def _initialize_after_frames(self, frames=1):
        app = omni.kit.app.get_app()
        for _ in range(frames):
            await app.next_update_async()
        self._initialize()

    def _initialize(self):
        # Body filled in by Task 6.
        self._initialized = True

    def is_inflow_enabled(self) -> bool:
        return self._inflow_enabled

    def toggle_inflow(self) -> None:
        self._inflow_enabled = not self._inflow_enabled
        carb.log_info(
            f"[Aquacast Temp] Inflow toggled -> {'ON' if self._inflow_enabled else 'OFF'}"
        )

    def _on_update(self, _event):
        # Body filled in by Task 8.
        pass

    def _on_stage_event(self, event):
        # Body filled in by Task 9.
        pass
```

- [ ] **Step 5: Verify `main.py` still parses**

Run: `python -c "import ast; ast.parse(open('extensions/aquacast.aquacast_composer/main.py').read()); print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 6: Confirm the existing tests still pass**

Run: `pytest extensions/aquacast.aquacast_composer/tests/ -v`
Expected: All thermal_dynamics tests (14) and all fish_dynamics tests pass.

- [ ] **Step 7: Commit**

```bash
git add extensions/aquacast.aquacast_composer/main.py
git commit -m "feat(aquacast): add WaterTempController singleton and lifecycle skeleton"
```

---

## Task 6: Implement Isosurface prim resolution (3-tier)

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

- [ ] **Step 1: Replace the stubbed `_initialize` body**

In `WaterTempController`, replace the existing `_initialize` method (the one with just `self._initialized = True`) with the version below:

```python
    def _initialize(self):
        if not bool(get_global_config("ENABLE_WATER_TEMP_VIS", False)):
            self._initialized = False
            self._isosurface_prim = None
            self._display_color_attr = None
            return

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            self._schedule_init_retry()
            return

        isosurface_prim = self._find_isosurface_prim(stage)
        if not isosurface_prim or not isosurface_prim.IsValid():
            self._warn_missing_isosurface_once()
            self._initialized = False
            self._isosurface_prim = None
            self._display_color_attr = None
            self._schedule_init_retry()
            return

        self._warned_missing_isosurface = False
        self._isosurface_prim = isosurface_prim
        # Display color attribute binding happens in Task 7.
        self._initialized = True
        carb.log_info(
            f"[Aquacast Temp] Bound to Isosurface at {isosurface_prim.GetPath()}; "
            f"T={self._T:.2f}°C, inflow={'ON' if self._inflow_enabled else 'OFF'}"
        )

    def _schedule_init_retry(self):
        retry = float(get_global_config("TEMP_VIS_INIT_RETRY_SECONDS", 1.0))
        self._next_init_retry_time = time.time() + max(0.1, retry)

    def _warn_missing_isosurface_once(self):
        if self._warned_missing_isosurface:
            return
        carb.log_warn(
            "[Aquacast Temp] Isosurface prim not found; will retry "
            f"(configured path = {get_global_config('ISOSURFACE_PRIM_PATH', '')!r})"
        )
        self._warned_missing_isosurface = True

    def _find_isosurface_prim(self, stage):
        # Tier 1: configured path.
        configured = str(get_global_config("ISOSURFACE_PRIM_PATH", "") or "").strip()
        if configured:
            prim = stage.GetPrimAtPath(configured)
            if prim and prim.IsValid():
                return prim

        # Tier 2: stage_topology.json cache.
        if bool(get_global_config("TEMP_VIS_USE_STAGE_TOPOLOGY_JSON", True)):
            for path in _get_topology_paths_by_name("Isosurface"):
                prim = stage.GetPrimAtPath(path)
                if prim and prim.IsValid():
                    return prim

        # Tier 3: stage traversal.
        for prim in stage.TraverseAll():
            if prim.GetName() == "Isosurface":
                return prim
        return None
```

- [ ] **Step 2: Verify `main.py` still parses**

Run: `python -c "import ast; ast.parse(open('extensions/aquacast.aquacast_composer/main.py').read()); print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 3: Confirm pytest still passes**

Run: `pytest extensions/aquacast.aquacast_composer/tests/ -v`
Expected: All tests still pass.

- [ ] **Step 4: Commit**

```bash
git add extensions/aquacast.aquacast_composer/main.py
git commit -m "feat(aquacast): resolve Isosurface prim with 3-tier fallback"
```

---

## Task 7: Bind `displayColor` primvar and add throttled write helper

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

- [ ] **Step 1: Add `Sdf` and `Vt` to the existing `pxr` import**

Find the existing import line (around line 19):

```python
from pxr import Gf, Usd, UsdGeom  # noqa: E402
```

Replace it with:

```python
from pxr import Gf, Sdf, Usd, UsdGeom, Vt  # noqa: E402
```

- [ ] **Step 2: Bind the displayColor primvar after a successful resolve**

In the `_initialize` method (Task 6), find the line:

```python
        self._isosurface_prim = isosurface_prim
        # Display color attribute binding happens in Task 7.
```

Replace those two lines with:

```python
        self._isosurface_prim = isosurface_prim
        self._display_color_attr = self._bind_display_color_primvar(stage, isosurface_prim)
        if self._display_color_attr is None:
            carb.log_warn(
                "[Aquacast Temp] Failed to bind displayColor primvar on "
                f"{isosurface_prim.GetPath()}; color updates will be skipped"
            )
```

- [ ] **Step 3: Add helper methods for binding and writing**

Add the following methods inside `WaterTempController` (after `_find_isosurface_prim`):

```python
    def _bind_display_color_primvar(self, stage, prim):
        try:
            gprim = UsdGeom.Gprim(prim)
            primvar = gprim.CreatePrimvar(
                "displayColor",
                Sdf.ValueTypeNames.Color3fArray,
                UsdGeom.Tokens.constant,
            )
            attr = primvar.GetAttr()
        except Exception as exc:
            carb.log_warn(f"[Aquacast Temp] CreatePrimvar failed: {exc}")
            return None
        # Move edit target to the session layer so writes never persist.
        session_layer = stage.GetSessionLayer()
        if session_layer is not None:
            stage.SetEditTarget(session_layer)
        return attr

    def _write_color(self, stage, r, g, b):
        if self._display_color_attr is None:
            return
        rgb = (max(0.0, min(1.0, r)),
               max(0.0, min(1.0, g)),
               max(0.0, min(1.0, b)))
        if self._prev_rgb is not None and all(
            abs(a - b_) <= (0.5 / 255.0) for a, b_ in zip(rgb, self._prev_rgb)
        ):
            return
        try:
            with Usd.EditContext(stage, stage.GetSessionLayer()):
                self._display_color_attr.Set(
                    Vt.Vec3fArray([Gf.Vec3f(*rgb)])
                )
            self._prev_rgb = rgb
        except Exception as exc:
            carb.log_warn(f"[Aquacast Temp] Failed to write displayColor: {exc}")

    def _sorted_stops(self, stops):
        if stops is not self._color_stops_cached:
            self._color_stops_cached = stops
            try:
                self._color_stops_sorted = sorted(stops, key=lambda s: s[0])
            except Exception:
                self._color_stops_sorted = []
        return self._color_stops_sorted
```

- [ ] **Step 4: Verify `main.py` still parses**

Run: `python -c "import ast; ast.parse(open('extensions/aquacast.aquacast_composer/main.py').read()); print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/main.py
git commit -m "feat(aquacast): bind Isosurface displayColor primvar and add throttled write helper"
```

---

## Task 8: Wire per-frame temperature evolution + color write

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

- [ ] **Step 1: Replace `_on_update` body**

In `WaterTempController`, replace the existing stub:

```python
    def _on_update(self, _event):
        # Body filled in by Task 8.
        pass
```

With:

```python
    def _on_update(self, _event):
        now = time.time()

        # Retry init if we haven't bound the prim yet.
        if not self._initialized or self._isosurface_prim is None:
            if now >= self._next_init_retry_time:
                self._initialize()
            self._last_update_time = now
            return

        if self._last_update_time is None:
            self._last_update_time = now
            return
        dt = min(now - self._last_update_time, 0.25)
        self._last_update_time = now
        if dt <= 0.0:
            return

        T_room  = float(get_global_config("ROOM_TEMP_C", 22.0))
        T_inlet = float(get_global_config("INLET_WATER_TEMP_C", 14.0))
        k_room  = float(get_global_config("THERMAL_K_ROOM", 0.012))
        k_inflow = float(get_global_config("THERMAL_K_INFLOW", 0.022))

        self._T = thermal_dynamics.step_temperature(
            self._T, dt,
            T_room=T_room, T_inlet=T_inlet,
            k_room=k_room, k_inflow=k_inflow,
            inflow_enabled=self._inflow_enabled,
        )

        stops = get_global_config("TEMP_COLOR_STOPS", [])
        sorted_stops = self._sorted_stops(stops)
        if sorted_stops:
            r, g, b = thermal_dynamics.temperature_to_rgb(self._T, sorted_stops)
            stage = omni.usd.get_context().get_stage()
            if stage is not None:
                self._write_color(stage, r, g, b)

        self._maybe_log(now, T_room, T_inlet, k_room, k_inflow)

    def _maybe_log(self, now, T_room, T_inlet, k_room, k_inflow):
        interval = float(get_global_config("TEMP_VIS_LOG_INTERVAL_SECONDS", 5.0))
        if interval <= 0.0:
            return
        if now - self._last_log_time < interval:
            return
        self._last_log_time = now
        eq = thermal_dynamics.equilibrium_temperature(
            T_room=T_room, T_inlet=T_inlet,
            k_room=k_room, k_inflow=k_inflow,
            inflow_enabled=self._inflow_enabled,
        )
        eq_str = f"{eq:.2f}°C" if eq is not None else "n/a"
        carb.log_info(
            f"[Aquacast Temp] T={self._T:.2f}°C, eq={eq_str}, "
            f"inflow={'ON' if self._inflow_enabled else 'OFF'}"
        )
```

- [ ] **Step 2: Verify `main.py` still parses**

Run: `python -c "import ast; ast.parse(open('extensions/aquacast.aquacast_composer/main.py').read()); print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 3: Confirm pytest still passes**

Run: `pytest extensions/aquacast.aquacast_composer/tests/ -v`
Expected: All tests still pass (controller code isn't covered by pytest, just verifying we didn't break the pure-math tests).

- [ ] **Step 4: Commit**

```bash
git add extensions/aquacast.aquacast_composer/main.py
git commit -m "feat(aquacast): step temperature each frame and write color to Isosurface"
```

---

## Task 9: Wire stage event hooks

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/main.py`

- [ ] **Step 1: Replace `_on_stage_event` body**

In `WaterTempController`, replace the existing stub:

```python
    def _on_stage_event(self, event):
        # Body filled in by Task 9.
        pass
```

With:

```python
    def _on_stage_event(self, event):
        event_type = event.type
        if event_type in (
            int(omni.usd.StageEventType.OPENED),
            int(omni.usd.StageEventType.ASSETS_LOADED),
        ):
            self._initialized = False
            self._isosurface_prim = None
            self._display_color_attr = None
            self._prev_rgb = None
            self._T = float(get_global_config("INITIAL_WATER_TEMP_C", 14.0))
            self._last_update_time = None
            self._next_init_retry_time = 0.0
            self._warned_missing_isosurface = False
            asyncio.ensure_future(self._initialize_after_frames(3))
        elif event_type == int(omni.usd.StageEventType.CLOSED):
            self._initialized = False
            self._isosurface_prim = None
            self._display_color_attr = None
            self._prev_rgb = None
            self._last_update_time = None
            self._next_init_retry_time = 0.0
            self._warned_missing_isosurface = False
```

`self._inflow_enabled` is intentionally **not** reset here — the user's menu choice persists across stage reopens.

- [ ] **Step 2: Verify `main.py` still parses**

Run: `python -c "import ast; ast.parse(open('extensions/aquacast.aquacast_composer/main.py').read()); print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add extensions/aquacast.aquacast_composer/main.py
git commit -m "feat(aquacast): reset water temp controller on stage open/close"
```

---

## Task 10: Hook controller lifecycle into `extension.py`

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/extension.py`

- [ ] **Step 1: Initialize the controller field in `on_startup`**

In `extension.py`, find these lines (around line 77-79):

```python
        self._stage_structure_cache = None
        self._fish_swim_controller = None
        self._aquacast_main = None
```

Replace with:

```python
        self._stage_structure_cache = None
        self._fish_swim_controller = None
        self._water_temp_controller = None
        self._aquacast_main = None
```

- [ ] **Step 2: Start the controller alongside the others**

In the same method, find these lines (around line 85-89):

```python
        try:
            aquacast_main = _load_aquacast_main_module()
            self._aquacast_main = aquacast_main
            self._stage_structure_cache = aquacast_main.start_stage_structure_cache()
            self._fish_swim_controller = aquacast_main.start_fish_swim_controller()
        except Exception as exc:
            carb.log_error(f"[Aquacast] Failed to start Aquacast runtime: {exc}")
```

Replace with:

```python
        try:
            aquacast_main = _load_aquacast_main_module()
            self._aquacast_main = aquacast_main
            self._stage_structure_cache = aquacast_main.start_stage_structure_cache()
            self._fish_swim_controller = aquacast_main.start_fish_swim_controller()
            self._water_temp_controller = aquacast_main.start_water_temp_controller()
        except Exception as exc:
            carb.log_error(f"[Aquacast] Failed to start Aquacast runtime: {exc}")
```

- [ ] **Step 3: Stop the controller in `on_shutdown`**

Find the shutdown block (around line 489-498):

```python
    def on_shutdown(self):
        """Clean up the extension"""
        if getattr(self, "_aquacast_main", None):
            if self._fish_swim_controller:
                self._aquacast_main.stop_fish_swim_controller()
                self._fish_swim_controller = None
        if self._stage_structure_cache and getattr(self, "_aquacast_main", None):
            self._aquacast_main.stop_stage_structure_cache()
            self._aquacast_main = None
            self._stage_structure_cache = None
```

Replace with:

```python
    def on_shutdown(self):
        """Clean up the extension"""
        if getattr(self, "_aquacast_main", None):
            if self._water_temp_controller:
                self._aquacast_main.stop_water_temp_controller()
                self._water_temp_controller = None
            if self._fish_swim_controller:
                self._aquacast_main.stop_fish_swim_controller()
                self._fish_swim_controller = None
        if self._stage_structure_cache and getattr(self, "_aquacast_main", None):
            self._aquacast_main.stop_stage_structure_cache()
            self._aquacast_main = None
            self._stage_structure_cache = None
```

- [ ] **Step 4: Verify `extension.py` still parses**

Run: `python -c "import ast; ast.parse(open('extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/extension.py').read()); print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/extension.py
git commit -m "feat(aquacast): start/stop water temp controller with extension lifecycle"
```

---

## Task 11: Register `Aquacast > Water Inflow` checkable menu item

**Files:**
- Modify: `extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/extension.py`

The exact `MenuItemDescription` API for checkable items differs slightly across Kit versions — sometimes it accepts a `ticked_fn` callback returning the current state, sometimes it requires re-registering after each toggle. We implement both: prefer `ticked_fn`; force a `refresh_menu_items` call on each toggle for robustness.

- [ ] **Step 1: Add the inflow-menu registration after the existing Help-menu block**

In `extension.py`, find the existing Help-menu registration (around line 210-217):

```python
        self._help_menu_items = [
            MenuItemDescription(
                name="Documentation",
                onclick_fn=show_documentation,
                appear_after=[omni.kit.menu.utils.MenuItemOrder.FIRST]
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._help_menu_items, name="Help")
```

Add the following **immediately after** that block (still inside the same method):

```python
        self._inflow_menu_items = []
        if (
            getattr(self, "_aquacast_main", None) is not None
            and self._water_temp_controller is not None
        ):
            aquacast_main = self._aquacast_main

            def _on_inflow_clicked(*_args):
                aquacast_main.toggle_water_temp_controller_inflow()
                try:
                    omni.kit.menu.utils.refresh_menu_items("Aquacast")
                except Exception:
                    pass

            self._inflow_menu_items = [
                MenuItemDescription(
                    name="Water Inflow",
                    ticked=True,
                    ticked_fn=aquacast_main.water_temp_controller_inflow_state,
                    onclick_fn=_on_inflow_clicked,
                )
            ]
            omni.kit.menu.utils.add_menu_items(self._inflow_menu_items, name="Aquacast")
```

- [ ] **Step 2: Remove the menu items on shutdown**

In `on_shutdown` (the method you edited in Task 10), find this block near the end:

```python
        for menu_dict in self._layout_menu_items:
            for group in menu_dict:
                omni.kit.menu.utils.remove_menu_items(menu_dict[group], group)
```

Add **immediately before** it:

```python
        if getattr(self, "_inflow_menu_items", None):
            try:
                omni.kit.menu.utils.remove_menu_items(self._inflow_menu_items, "Aquacast")
            except Exception:
                pass
            self._inflow_menu_items = None
```

- [ ] **Step 3: Verify `extension.py` still parses**

Run: `python -c "import ast; ast.parse(open('extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/extension.py').read()); print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/extension.py
git commit -m "feat(aquacast): add 'Aquacast > Water Inflow' checkable menu item"
```

---

## Task 12: Manual verification in Kit

This task runs no code itself. It exists to enforce the visual verification from spec §9.2 before declaring the feature done. Record outcomes inline by checking each box; if any step fails, file the specific symptom and fix forward.

**Pre-step:** make sure pytest is still green.

Run: `pytest extensions/aquacast.aquacast_composer/tests/ -v`
Expected: All thermal_dynamics + fish_dynamics tests pass.

- [ ] **Step 1: Launch the composer**

```bash
./start_aquacast.sh --composer
```

Expected: Kit launches; the scene loads; within a few seconds the Isosurface visibly turns teal (≈ `#00BFBF`). Carb log shows `[Aquacast Temp] Bound to Isosurface at /Root/Aquarium/.../Isosurface; T=14.00°C, inflow=ON` followed by periodic `[Aquacast Temp] T=...°C, eq=16.82°C, inflow=ON` lines every ~5 s.

- [ ] **Step 2: Confirm equilibrium with inflow ON**

Watch for ≈ 90 s. The Isosurface color drifts from teal toward light amber, then stabilizes. Logged `T` should approach ~16.8 °C and stop visibly climbing.

- [ ] **Step 3: Toggle inflow OFF via menu**

In the Kit menubar, open the `Aquacast` menu and uncheck `Water Inflow`. Expected:
- Log line `[Aquacast Temp] Inflow toggled -> OFF`.
- Subsequent log lines show `inflow=OFF`, `eq=22.00°C`, and `T` continuing to climb past 17 °C.
- Color continues warming toward red over the next 2–3 min.

- [ ] **Step 4: Toggle inflow back ON**

Re-check `Aquacast > Water Inflow`. Expected:
- Log line `[Aquacast Temp] Inflow toggled -> ON`.
- `T` reverses and drops back toward ~16.8 °C; recovery is fastest immediately after the toggle.

- [ ] **Step 5: Stage reopen resets temperature**

In Kit, reopen the same USD (`File > Open` of the same file). Expected:
- Color resets to teal.
- Log shows `T=14.00°C` again.
- The `Water Inflow` menu state (whatever you last set it to) is preserved.

- [ ] **Step 6: Disable the feature**

Edit `extensions/aquacast.aquacast_composer/global_variable.py` and set `ENABLE_WATER_TEMP_VIS = False`. Restart Kit (`./start_aquacast.sh --composer`). Expected:
- Isosurface uses its USD-authored color, no longer teal.
- `Aquacast > Water Inflow` menu item is absent.
- No `[Aquacast Temp]` log lines.

Revert the flag back to `True` once verified.

- [ ] **Step 7: Final pytest pass**

Run: `pytest extensions/aquacast.aquacast_composer/tests/ -v`
Expected: All 14 thermal_dynamics tests and all fish_dynamics tests pass.

- [ ] **Step 8: No commit unless code changed during verification.**

If you had to adjust any code to make the manual steps pass, commit those fixes with messages of the form `fix(aquacast): <symptom> in water temp controller`. Otherwise nothing to commit.

---

## Self-Review Checklist (for the implementer)

- [ ] Every spec section in `docs/superpowers/specs/2026-05-20-water-temperature-visualization-design.md` has a corresponding task in this plan.
- [ ] All pytest tests pass: `pytest extensions/aquacast.aquacast_composer/tests/ -v`.
- [ ] No new files outside the five listed under "File Structure".
- [ ] `FishSwimController` and its tests are untouched.
- [ ] All commits are scoped (one feature concern per commit, as listed).
- [ ] `ENABLE_WATER_TEMP_VIS = False` cleanly removes the feature (Task 12 Step 6).
