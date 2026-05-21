# Water Temperature Visualization — Design

**Date:** 2026-05-20
**Component:** `extensions/aquacast.aquacast_composer` (new `WaterTempController` in `main.py`, new `thermal_dynamics.py`, additions to `extension.py` and `global_variable.py`)
**Status:** Approved scope; pending implementation plan

## 1. Background

The Aquacast composer scene contains an indoor salmon RAS (recirculating aquaculture system) tank with:

- A PhysX `ParticleSystem` and rendered `Isosurface` at `/Root/Aquarium/AquariumComponents/FishTank/InWater/Components/ParticleSystem/Isosurface`.
- A water inlet (`/Root/Aquarium/AquariumComponents/FishTank/inlet/Inlet_Trace_Source`) that continuously supplies fresh ~14 °C water.
- An ambient indoor environment whose temperature is warmer than the inlet (e.g. ~22 °C).

The scene currently has no thermal model and no temperature-driven visualization. Salmon stay healthy in cold water; understanding when the water deviates from its target temperature is operationally important. A live color visualization of bulk water temperature — driven by the balance between room heating and continuous cold-water inflow — gives the viewer immediate insight into tank state without numeric overlays.

## 2. Goals

- **G1.** Bulk water temperature starts at 14 °C and evolves toward a lumped-mass equilibrium between room heat gain and inlet cooling, expressed as a single ODE.
- **G2.** Visualize the current temperature on the `Isosurface` prim via a color ramp **Blue (< 14 °C) → Teal (14 °C) → Amber → Red (warmer)**.
- **G3.** Demo time scale: time constant `τ ≈ 30 s` with inflow ON. All time/temperature parameters are tunable through `global_variable.py` hot reload.
- **G4.** Deterministic: same parameters and same `dt` sequence ⇒ bit-identical results. No RNG.
- **G5.** Session-layer USD writes only — color changes never persist to disk. Same convention as `FishSwimController`.
- **G6.** `ENABLE_WATER_TEMP_VIS = False` toggle restores prior behavior bit-for-bit (no controller, no menu item).
- **G7.** Real-time inflow ON/OFF toggle via Kit menu `Aquacast > Water Inflow`. With inflow ON the temperature approaches the mixing equilibrium (~17 °C with default knobs); with inflow OFF it continues rising toward room temperature.

## 3. Non-Goals

- Temperature-driven effects on fish behavior (stress, mortality, locomotion changes).
- Spatial / 2-zone / per-particle temperature fields. Bulk temperature only.
- HUD text overlay or any numeric on-screen readout.
- Direct writes to material shader inputs (Approach 2 fallback). Deferred to v2 if `primvars:displayColor` proves insufficient.
- Dynamic flow rate or inlet temperature scheduling. `THERMAL_K_INFLOW` and `INLET_WATER_TEMP_C` are scalar constants.
- External slider UI for room temp or coefficients. All non-toggle tuning happens by editing `global_variable.py`.
- Promotion of the new controller's state into a class shared with `FishSwimController`. Each controller stays self-contained.

## 4. Approach Overview

Three new pieces, mirroring the `FishSwimController` / `fish_dynamics.py` split:

| Responsibility           | Where it lives                                        | Notes                                         |
|--------------------------|-------------------------------------------------------|-----------------------------------------------|
| Pure thermal math        | new `extensions/aquacast.aquacast_composer/thermal_dynamics.py` | Zero Omniverse imports. Plain pytest.         |
| Runtime controller       | new `WaterTempController` singleton in `main.py`      | Holds `T`, `inflow_enabled`, prim handles.    |
| Tuning constants         | additions to `global_variable.py`                     | Hot-reloaded every frame.                     |
| Menu toggle              | new `MenuItemDescription` in `extension.py`           | Checkable; talks to controller via module fns. |

`extension.py` gains three things: (1) a `start_water_temp_controller()` / `stop_water_temp_controller()` call alongside the existing fish controller lifecycle, (2) registration of the `Aquacast > Water Inflow` checkable menu when `ENABLE_WATER_TEMP_VIS` is true, (3) menu callbacks delegating to module-level functions in `main.py`. The runtime controller is the single source of truth for `inflow_enabled`; the menu's `ticked_fn` reads from it.

## 5. Design Detail

### 5.1 Pure-math module `thermal_dynamics.py`

Three functions, no state, no Omniverse imports.

```python
def step_temperature(T: float, dt: float, *,
                     T_room: float, T_inlet: float,
                     k_room: float, k_inflow: float,
                     inflow_enabled: bool) -> float:
    """
    Newton-style lumped heat balance with optional inflow term.
    Exact integration over dt assuming constants:
        dT/dt = k_room * (T_room - T) + k_inflow_eff * (T_inlet - T)
    where k_inflow_eff = k_inflow if inflow_enabled else 0.
    Closed form:
        b = k_room + k_inflow_eff
        if b == 0: return T
        T_eq = (k_room*T_room + k_inflow_eff*T_inlet) / b
        return T_eq + (T - T_eq) * exp(-b * dt)
    """

def equilibrium_temperature(*, T_room, T_inlet, k_room, k_inflow,
                            inflow_enabled) -> float | None:
    """Equilibrium of the same ODE. Returns None when b == 0 (no heat exchange)."""

def temperature_to_rgb(T: float,
                       stops: list[tuple[float, tuple[float, float, float]]]
                       ) -> tuple[float, float, float]:
    """
    Piecewise-linear interpolation between sorted (temp, (r,g,b)) stops.
    Clamps at endpoints. Returns linear RGB in [0,1]^3.
    Accepts unsorted input (sorts internally).
    """
```

**Why exact integration instead of forward Euler.** Forward Euler (`T += dt * dT_dt`) can overshoot or diverge when `dt` is large — for example after a frame drop or a pause / resume. The closed-form expression is stable for any `dt ≥ 0` because `exp(-b·dt)` is bounded in `(0, 1]`.

**Why the `inflow_enabled` flag lives inside the math function.** Keeps the controller's update loop branchless and keeps the toggle semantics in one testable place. The function returns `T` unchanged when both `k_room` and the effective `k_inflow` are zero, which is the natural edge case.

### 5.2 Color ramp

Default stops in `global_variable.py`, linear RGB in `[0,1]`:

```python
TEMP_COLOR_STOPS = [
    (10.0, (0.05, 0.25, 1.00)),   # deep blue
    (14.0, (0.00, 0.75, 0.75)),   # teal (baseline)
    (18.0, (0.90, 0.55, 0.20)),   # amber transition
    (25.0, (1.00, 0.12, 0.12)),   # red (warning)
]
```

`temperature_to_rgb`:

1. `T <= stops[0].temp` → first color (clamp).
2. `T >= stops[-1].temp` → last color (clamp).
3. Otherwise: locate the bracketing pair `(T_lo, c_lo)` and `(T_hi, c_hi)`, interpolate with `α = (T - T_lo) / (T_hi - T_lo)` componentwise.

Four stops (rather than two) because 14 → 25 °C is only 11 °C wide; a 2-stop ramp would jump to nearly red immediately. The amber midpoint at 18 °C gives the viewer a visible "warming up" stage before saturating toward red.

### 5.3 USD write — `primvars:displayColor` on `Isosurface`

```python
from pxr import Sdf, Usd, UsdGeom, Vt, Gf

# Bind once, after prim resolution:
gprim = UsdGeom.Gprim(isosurface_prim)
display_color_primvar = gprim.CreatePrimvar(
    "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.constant
)
self._display_color_attr = display_color_primvar.GetAttr()

# Per frame, inside session-layer edit context:
with Usd.EditContext(stage, stage.GetSessionLayer()):
    self._display_color_attr.Set(Vt.Vec3fArray([Gf.Vec3f(r, g, b)]))
```

- **Session layer only.** `FishSwimController._on_update` already sets the edit target to the session layer; `WaterTempController.start()` does the same once at controller startup. Color changes do not persist when the stage is saved.
- **Constant interpolation.** A single color across the whole surface (`Vt.Vec3fArray` of length 1).
- **Write throttling.** Only call `Set(...)` when the new RGB differs from the previous by more than `0.5/255` in any channel, to avoid unnecessary USD notifications on frames where the color is visually identical.

### 5.4 Prim resolution (3-tier)

Same pattern as `FishSwimController._resolve_water_prim`. In `_resolve_isosurface_prim`, in order:

1. **Configured path:** if `ISOSURFACE_PRIM_PATH` resolves to a valid prim, use it.
2. **Topology JSON cache:** if `TEMP_VIS_USE_STAGE_TOPOLOGY_JSON` is true, walk `stage_topology.json` and pick the first entry with `name == "Isosurface"`.
3. **Stage traversal:** iterate the stage and pick the first prim with `GetName() == "Isosurface"`.
4. If all three fail, emit a `[Aquacast Temp]` warn log and retry every `TEMP_VIS_INIT_RETRY_SECONDS`. This mirrors how `FishSwimController` handles assets that are still streaming in.

### 5.5 Per-frame update

```python
def _on_update(self, e):
    if not self._enabled or self._isosurface_prim is None:
        return
    now = time.time()
    dt = min(now - self._last_time, 0.25)   # cap dt as FishSwimController does
    self._last_time = now

    cfg = self._read_config()               # hot-reload via importlib
    self._T = thermal_dynamics.step_temperature(
        self._T, dt,
        T_room=cfg.T_room, T_inlet=cfg.T_inlet,
        k_room=cfg.k_room, k_inflow=cfg.k_inflow,
        inflow_enabled=self._inflow_enabled,
    )
    r, g, b = thermal_dynamics.temperature_to_rgb(self._T, self._sorted_stops(cfg.stops))
    self._maybe_write_color(r, g, b)
    self._maybe_log(now, cfg)
```

Controller state:

- `self._T` — current bulk temperature (°C).
- `self._inflow_enabled` — bool; flipped by menu, read by `_on_update`.
- `self._last_time`, `self._last_log_time`.
- `self._isosurface_prim`, `self._display_color_attr` — bound after first successful resolution.
- `self._color_stops_cached`, `self._color_stops_sorted` — list-identity memoization (see Section 5.7).
- `self._prev_rgb` — for write throttling.

Stage event hooks (mirroring `FishSwimController`):

- `OPENED` → re-resolve the Isosurface prim and reset `self._T = INITIAL_WATER_TEMP_C`. `self._inflow_enabled` is **not** reset — the user's menu choice persists across stage reopens.
- `CLOSED` → drop prim handles.

### 5.6 Menu toggle

`main.py` exposes two module-level functions:

```python
def water_temp_controller_inflow_state() -> bool:
    return bool(_water_temp_controller and _water_temp_controller.is_inflow_enabled())

def toggle_water_temp_controller_inflow() -> None:
    if _water_temp_controller is not None:
        _water_temp_controller.toggle_inflow()
```

`extension.py`, only when `ENABLE_WATER_TEMP_VIS` is true at startup:

```python
self._inflow_menu_items = [
    MenuItemDescription(
        name="Water Inflow",
        ticked=True,
        ticked_fn=water_temp_controller_inflow_state,
        onclick_fn=toggle_water_temp_controller_inflow,
    )
]
omni.kit.menu.utils.add_menu_items(self._inflow_menu_items, name="Aquacast")
```

Menu state is derived from the controller (`ticked_fn`); the controller is the single source of truth. The exact `MenuItemDescription` checkable API is verified at implementation time — if `ticked_fn` is not the right field, fall back to refreshing the menu after each toggle via `omni.kit.menu.utils.refresh_menu_items`.

### 5.7 Hot reload and config caching

`global_variable.py` is re-read every frame via `get_global_config()` — the same pattern `FishSwimController` already uses. Scalars (`T_room`, `T_inlet`, `k_room`, `k_inflow`, etc.) are cheap to use directly.

`TEMP_COLOR_STOPS` is memoized by list identity:

```python
def _sorted_stops(self, stops):
    if stops is not self._color_stops_cached:
        self._color_stops_cached = stops
        self._color_stops_sorted = sorted(stops, key=lambda s: s[0])
    return self._color_stops_sorted
```

When the user edits and re-saves `global_variable.py`, importlib returns a new list object, identity changes, and the sort runs once.

## 6. Configuration (`global_variable.py` additions)

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

Default `THERMAL_K_*` values satisfy two constraints simultaneously: (a) `τ ≈ 30 s` with inflow ON for the demo, and (b) inflow ON equilibrium ≈ 17 °C (clearly cooler than room temp). Verify on first launch via the `[Aquacast Temp]` log line.

## 7. Backward Compatibility

`ENABLE_WATER_TEMP_VIS = False` selects a path with strictly no side effects:

- `start_water_temp_controller()` returns early before constructing the controller.
- `extension.py` skips the `add_menu_items(...)` registration for the inflow toggle (no `Aquacast > Water Inflow` entry).
- No USD writes anywhere — Isosurface's `displayColor` stays at its USD-authored value.

The flag is read once, in `start_water_temp_controller()` and in the menu registration block. Toggling the flag at runtime requires an extension reload, matching the existing `ENABLE_FISH_SWIMMING` constraint.

`INFLOW_ENABLED_DEFAULT` is read once at controller `start()` to seed `self._inflow_enabled`. After startup the menu is authoritative — edits to `INFLOW_ENABLED_DEFAULT` in `global_variable.py` are intentionally ignored at runtime.

## 8. Determinism

- All time evolution is closed-form (`exp`) and color is deterministic linear interpolation. No RNG anywhere in this feature.
- The only non-determinism source is the `dt` sequence (Kit frame pacing), same constraint as `FishSwimController`. `dt` is capped at 0.25 s.
- Same launch + same toggle sequence + same `dt` sequence ⇒ bit-identical results.
- Unit tests in `tests/test_thermal_dynamics.py` pass `dt` as input, removing time dependence entirely.

## 9. Verification

### 9.1 Automated (`pytest`, no Kit required)

In `extensions/aquacast.aquacast_composer/tests/test_thermal_dynamics.py`:

| Test | Expectation |
|---|---|
| `test_step_no_heat_transfer_keeps_temp` | `k_room=0`, inflow_enabled=False ⇒ ΔT == 0 |
| `test_step_inflow_off_approaches_room` | Many steps with inflow=False ⇒ T → T_room within 1e-3 |
| `test_step_inflow_on_approaches_mix_eq` | Many steps with inflow=True ⇒ T → `(k_room*T_room+k_inflow*T_inlet)/(k_room+k_inflow)` |
| `test_step_is_stable_for_large_dt` | `dt=10` does not diverge; result is monotone toward equilibrium |
| `test_step_continuous_at_toggle_boundary` | Toggling `inflow_enabled` at the same T produces no T jump on the next step |
| `test_color_clamps_below_first_stop` | T=5 °C ⇒ exact first-stop color |
| `test_color_clamps_above_last_stop` | T=30 °C ⇒ exact last-stop color |
| `test_color_lerp_midpoint` | T=16 between (14, c14) and (18, c18) ⇒ componentwise 0.5 lerp |
| `test_color_at_exact_stop` | T equal to a stop's temp ⇒ exact stop color |
| `test_color_stops_unsorted_input` | Function tolerates unsorted stops and yields the same result as sorted |
| `test_equilibrium_returns_none_when_no_transfer` | `b == 0` ⇒ `equilibrium_temperature` returns None |

Run with:

```
pytest extensions/aquacast.aquacast_composer/tests/ -v
```

### 9.2 Manual visual verification (Kit launch)

`./start_aquacast.sh --composer`, then:

1. **t=0:** Isosurface renders teal (≈ `#00BFBF`). First `[Aquacast Temp]` log line shows `T=14.00°C, eq=16.82°C, inflow=ON`.
2. **Inflow ON, t≈90 s:** Surface drifts teal → light amber, stabilizes near 17 °C. Does **not** reach pure red.
3. **Toggle OFF via `Aquacast > Water Inflow`:** Color resumes climbing past amber toward red. By ~3 min, stabilizes near the 22 °C red tone.
4. **Toggle ON again:** Color reverses, drifting back toward the ~17 °C amber/teal zone. Recovery is faster initially (large `T - T_eq`) and slows near equilibrium.
5. **Stage reopen (same USD):** Color resets to teal (14 °C). The menu checkbox state is preserved.
6. **`ENABLE_WATER_TEMP_VIS = False`, restart Kit:** Isosurface stays at its USD-authored color, the `Aquacast > Water Inflow` menu item is absent.

### 9.3 Regression

- `tests/test_fish_dynamics.py` continues passing.
- `FishSwimController` behavior is untouched. The two controllers operate on disjoint prim sets (Fish controller writes `Fish_*` transforms and the Water cylinder, Temp controller writes Isosurface `displayColor`).
- Kit-hosted tests declared in `config/extension.toml` are not modified.

## 10. Risks & Mitigations

| Risk                                                                                  | Mitigation                                                                                                                                                                       |
|---------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Isosurface has a material binding that ignores `primvars:displayColor`                | Discovered on first run via 9.2 step 1. v2 fallback: also write to the material's `inputs:diffuse_color_constant` (Approach 2). Reserve `_apply_material_color()` as a no-op stub with TODO in v1. |
| Prim path changes in a future asset revision                                          | 3-tier resolution (configured → topology JSON → traversal) + retry timer; `[Aquacast Temp]` warn log surfaces the failure.                                                       |
| Non-physical configuration (`INLET_WATER_TEMP_C > ROOM_TEMP_C`)                       | Math still valid. Emit a one-shot `carb.log_warn` from `__init__` when detected; do not block.                                                                                   |
| Menu callback fires before the controller exists                                      | `toggle_water_temp_controller_inflow()` and `water_temp_controller_inflow_state()` both null-check the singleton and return safely (no-op / `False`). Menu is only registered when `ENABLE_WATER_TEMP_VIS=True`. |
| Per-frame `importlib` reload cost                                                     | `FishSwimController` already pays this cost. One additional reload per frame is negligible at Kit's 60 Hz target. Could be consolidated into a shared cache in a future refactor. |
| Per-frame `displayColor` writes inflate USD notifications                             | Throttle writes by RGB-delta threshold (`> 0.5/255`); skip writes when the new color rounds to the same 8-bit triple as the previous one.                                        |
| `main.py` grows further (currently ~850 lines; ~150 lines added)                       | Acceptable for this feature. A future per-controller file split is justified once a third controller appears.                                                                    |
