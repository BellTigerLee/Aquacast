# Thermal Model v2 — Physics-First Heat Transfer in the Backend — Design

**Date:** 2026-05-27
**Branch:** `temp-v2`
**Component:** `backend/water_quality_backend.py` + `extensions/aquacast.aquacast_composer_extensions`
(physics integration owned by `water_quality_model.WaterQualityModel`; pure-math upgraded in
`thermal_dynamics.py`; constants added to `data/wq_constants.json`; extension `WaterTempController`
demoted to a pure consumer)
**Status:** Approved scope; pending implementation plan
**Builds on:**
- `2026-05-20-water-temperature-visualization-design.md` (the temperature feature this generalizes)
- `2026-05-26-water-quality-simulation-design.md` (the backend CSTR engine this hooks into)

---

## 1. Background & Motivation

The scene is an indoor land-based RAS (recirculating aquaculture system) tank for North Pacific Chum
salmon, grow-out stage. Water temperature drives almost every water-quality response already modeled
in the backend (`do_saturation`, the `Q10` metabolic term, `nh3_fraction`), so temperature is the
single most leveraged state variable in the simulation.

### 1.1 What exists today

- **Bulk temperature is a 0-D lumped Newton heat balance** (`thermal_dynamics.step_temperature`):

  ```
  dT/dt = k_room·(T_room − T) + k_inflow·(T_inlet − T)
  T(t+dt) = T_eq + (T − T_eq)·exp(−(k_room + k_inflow)·dt)
  ```

  with abstract rate constants `THERMAL_K_ROOM = 0.012/h`, `THERMAL_K_INFLOW = 0.022/h`.

- **The backend already owns this step.** `WaterQualityModel._advance_one_substep` calls
  `thermal_dynamics.step_temperature(...)` on the shared `substep_h` / `time_scale` clock **when no
  external temperature is supplied** — but the Kit extension currently computes its own temperature
  and passes it to `/advance` as `temperature_c`, overriding the backend value.

- **Per-particle temperature is a static spatial pattern**, not diffusion:
  `T_i = bulk_T + heat_delta · weight_i · spread`, where `weight_i` is a fixed geometric weight and
  `spread = 1 − exp(−elapsed·rate)` is a global fade-in. There is no neighbor coupling.

### 1.2 Why it is not "physical"

1. `k_room`, `k_inflow` are **tuning constants**, not derived from tank geometry, wall U-value, free
   surface area, or makeup flow. Change the tank and the thermal response does not change.
2. `k_room` silently lumps **three different physical paths** (free-surface evaporation, free-surface
   convection, longwave radiation) plus wall conduction into one number.
3. The current heater hook is **dimensionally wrong**: `T_room + heater_power` adds watts to a
   temperature (`water_quality_model.py:264`).
4. The particle field does not transport heat — it cannot show heat entering at a wall, a cold inlet
   jet, or a heater and spreading through the bulk over time.

Meanwhile the water-*quality* side of the same model is already physics-first (Benson-Krause DO
saturation, carbonate pH, first-order nitrification). Temperature is the outlier. This design closes
that gap.

## 2. Goals / Non-Goals

**Goals**
- G1. Move temperature computation **fully into the backend**; the extension becomes a pure consumer.
- G2. Replace the lumped balance with a **mechanistic energy balance with nonlinear free-surface
  fluxes** (evaporation + sensible convection + longwave radiation, the `T⁴` term kept nonlinear),
  integrated with **RK4 on the existing fixed substep clock**.
- G3. Drive all tank geometry and heat-transfer coefficients from **backend constants managed via the
  existing `wq_constants.json` + `AQUACAST_WQ_*` environment-variable mechanism**. The backend never
  touches USD.
- G4. Make the particle field a **real diffusion field computed in the backend**: neighbor-coupled
  discrete Laplacian over the particle cloud, with boundary heat sources, mean-locked to the bulk
  temperature.
- G5. Promote the heater to a proper `Q_heater [W]` source.

**Non-Goals**
- 1-D vertical stratification or any spatial PDE beyond the particle-cloud diffusion.
- Solar shortwave (indoor scene; out of scope).
- Recirculation-pump heat-of-dissipation (not selected; the additive structure leaves room to add it
  later as another `Q` term).
- Changing the color ramp location — `temperature_to_rgb` stays extension-side (presentation).

## 3. Locked Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Backend is the **sole owner** of `temperature_c`. | Matches "all calculation in the backend." |
| D2 | `thermal_dynamics.py` **stays in the extension dir** as pure-math; the backend imports it via the existing `sys.path` insert. | Same precedent as `water_quality_dynamics.py`; no import churn. |
| D3 | Bulk physics = **nonlinear surface fluxes + RK4** fixed substep. | Chosen fidelity tier; `T⁴` radiation needs a nonlinear integrator. |
| D4 | Particle diffusion **runs in the backend**. Extension sends positions once; backend builds the KNN graph and owns per-particle temperature state. | "All calculation in the backend." |
| D5 | Geometry/coefficients are **backend constants** (`wq_constants.json`, env-overridable). | Backend has no USD access; user-requested. |
| D6 | Heater = proper **`Q_heater [W]`** source; remove the `T_room + heater_power` hack. | Dimensional correctness. |
| D7 | `/advance` `temperature_c` parameter is **deprecated** — retained only as an optional debug override. | D1. |
| D8 | Geometry source of truth for **physics** = config; the extension keeps placing particles from the USD water bbox and **warns** when USD bounds and config geometry diverge. | Visual must follow the rendered water; physics must be deterministic and USD-free. |

## 4. Architecture & Data Flow

```
 Kit extension (main.py)                         Backend process (water_quality_backend.py)
 ────────────────────────                        ───────────────────────────────────────────
 WaterTempController  ── (a) register particles ─► POST /particles/register
   - reads USD water bbox      positions[N], tags        builds KNN graph, inits T_i = T_bulk
   - authors UsdGeom.Points                               caches graph keyed by position-hash
   - warns if bbox≠config geom
                        ── (b) each frame ────────► POST /advance {real_dt_s}
                                                     WaterQualityModel.advance():
                                                       for each substep_h:
                                                         T_bulk = thermal_dynamics.step_temperature_rk4(...)
                                                         particle field ← diffuse_step(T_bulk, graph)
                                                         (then existing water-quality substep)
                        ◄─ snapshot {temperature_c,...}
                        ── (c) fetch field ───────► GET  /particles/values
                        ◄─ {temperature:[N], ...}  (cheap read of cached state, no recompute)
   - maps temp→displayColor (temperature_to_rgb, extension-side)
   - writes primvars to session layer
```

- `WaterQualityModel` gains an internal `ThermalState` (bulk `T`) and a `ParticleField` (positions,
  KNN graph, per-particle `T_i`, boundary source masks). Both advance on the **same substep loop** as
  the water-quality ODEs, so temperature, quality, and the particle field stay time-consistent.
- The extension stops calling `thermal_dynamics.step_temperature` and stops sending `temperature_c`.
  It reads `temperature_c` from the snapshot and the per-particle array from `/particles/values`.

## 5. Bulk Energy Balance (the equations)

Single well-mixed bulk temperature `T` [°C]. Net heat into the water `Q_net` [W]:

```
ρ·V·c_p · dT/dt = Q_adv + Q_wall + Q_surf + Q_heater

ρ   = WATER_DENSITY            (998 kg/m³ @ ~20 °C)
c_p = WATER_CP                 (4186 J/kg·K)
V   = π r² h   (or tank_volume_l/1000; see §7 consistency)
A_s = π r²                     free water surface (open top)
A_w = 2π r h + π r²            wetted walls + bottom (top excluded — it is the free surface)
```

**Advective makeup/exchange inflow** (exact for a CSTR; uses *makeup* flow, not the recirculation loop):
```
Q_adv = ρ · c_p · Q_make · (T_inlet − T)        [W]      inflow_enabled == False → Q_make = 0
```

**Wall + bottom conduction to room air:**
```
Q_wall = U_wall · A_w · (T_room − T)            [W]
```

**Free-surface fluxes** (per-area, W/m²; indoor → no shortwave):
```
e_s(θ) = 0.6108 · exp(17.27·θ / (θ + 237.3))            saturation vapor pressure [kPa], θ in °C
e_air  = RH · e_s(T_air)                                 air vapor pressure [kPa]

H_evap = (a_e + b_e·u_air) · (e_s(T) − e_air)            evaporative (latent) LOSS  [W/m²]
H_conv = γ · (a_e + b_e·u_air) · (T − T_air)             sensible LOSS, Bowen-coupled [W/m²]
H_lw   = ε·σ·[(T_room + 273.15)⁴ − (T + 273.15)⁴]        net longwave exchange w/ room [W/m²]

Q_surf = A_s · ( H_lw − H_evap − H_conv )                [W]
```
- `(a_e + b_e·u_air)` is the aerodynamic transfer function [W/m²/kPa]; `u_air` is indoor air speed.
  This pair is the **primary calibration knob**.
- Sensible heat is tied to evaporation through the Bowen ratio constant `γ ≈ 0.066 kPa/K` so the two
  surface terms stay physically consistent (same transfer function).
- `ε ≈ 0.96` (water), `σ = 5.670e-8 W/m²K⁴`. `H_lw` is the nonlinear `T⁴` term.

**Heater source:**
```
Q_heater = heater_power_W                       [W]      (set via set_heater action; 0 by default)
```

### 5.1 Integration

- `thermal_dynamics.step_temperature_rk4(T, dt_s, *, params...)` does one **RK4** step over `dt_s`
  **seconds**, evaluating `dT/dt = Q_net(T) / (ρ V c_p)`.
- `WaterQualityModel` keeps its existing `substep_h` loop; for each substep it passes
  `dt_s = substep_h · 3600`. Units live in SI seconds inside `thermal_dynamics`; the model owns the
  hours↔seconds conversion. (This is the seam where the old `T_room + heater_power` unit bug lived —
  the new code keeps fluxes in W and temperatures in °C, never mixing.)
- At substep sizes of ~1 min and tank time constants of tens of hours, RK4 is comfortably stable and
  accurate for the `T⁴` term; no implicit solve is needed.
- The old `equilibrium_temperature` / linear `step_temperature` remain for the legacy/linear path and
  for analytic-equivalence unit tests, but are no longer on the runtime path.

## 6. Particle Diffusion Field (backend)

Replaces `T_i = bulk + weight_i·spread`. For `N` static points with positions `x_i`:

```
dT_i/dt = D · Σ_{j ∈ KNN(i)} w_ij·(T_j − T_i)   +   λ·(T_bulk − T_i)   +   S_i
```

- **Graph:** k-nearest-neighbor graph (`KNN_K` neighbors), weights `w_ij = exp(−|x_i−x_j|²/2σ²)`
  (or `1/d²`), built **once** per position set and cached by a hash of the positions. Rebuilt only
  when the extension re-registers a different cloud (stage reopen / particle count change).
- **Bulk relaxation `λ·(T_bulk − T)`:** keeps the field's mean tracking the physical bulk `T`. After
  each step the field is **mean-locked**: `T_i += (T_bulk − mean(T_i))` so the visualization can never
  drift from the energy-balance temperature (energy consistency).
- **Boundary sources `S_i`:** derived backend-side from positions + geometry (reusing the existing
  `_particle_features` radial/vertical normals):
  - wall band (high `radial_norm`) pulled toward `T_room` (or toward whichever side is hotter),
  - inlet jet region (near `INLET_LOCATION`) pulled toward `T_inlet`,
  - heater region (near `HEATER_LOCATION`, if `heater_power > 0`) injected proportional to `Q_heater`.
- **Stability:** explicit step requires `D · dt_s · max_i Σ_j w_ij ≤ 0.5`. The backend clamps the
  effective `D` to satisfy this, optionally taking a few internal sub-iterations per substep. (Jacobi
  smoothing is the unconditionally-stable fallback if we later raise `D`.)
- `/particles/values` returns the **current cached `T_i`** without recomputing — `/advance` already
  stepped them — so the per-frame read is cheap. Other quality variables continue to come from the
  existing `particle_values` reconstruction unless/until they get their own fields.

## 7. Configuration (backend constants)

All new keys go in `data/wq_constants.json` (loaded by `load_model`, overridable by
`AQUACAST_WQ_CONSTANTS`). Geometry is **physics source of truth**; the extension validates against the
USD water bbox at registration and logs `[Aquacast Temp]` warn on mismatch (D8).

| Key | Unit | Default | Meaning |
|-----|------|---------|---------|
| `tank_radius_m` | m | 1.2 | water cylinder radius → `A_s`, `A_w`, `V` |
| `tank_water_height_m` | m | 2.21 | water depth → `A_w`, `V` |
| `water_density` | kg/m³ | 998.0 | ρ |
| `water_cp` | J/kg·K | 4186.0 | c_p |
| `u_wall_w_m2k` | W/m²K | 5.0 | overall wall/bottom heat-transfer coeff |
| `emissivity` | – | 0.96 | water longwave emissivity |
| `air_temp_c` | °C | 22.0 | indoor air temp (assumed = `room_temp_c`) |
| `rel_humidity` | – | 0.60 | indoor relative humidity (fraction) |
| `air_speed_ms` | m/s | 0.2 | air speed over the surface |
| `evap_a_w_m2_kpa` | W/m²/kPa | *calib* | evaporation transfer intercept `a_e` |
| `evap_b_w_m2_kpa_per_ms` | W/m²/kPa/(m/s) | *calib* | evaporation transfer slope `b_e` |
| `bowen_gamma_kpa_k` | kPa/K | 0.066 | psychrometric/Bowen constant `γ` |
| `q_makeup_lph` | L/h | 220 | makeup/exchange cold-water flow `Q_make` |
| `inlet_temp_c` | °C | 12.0 | makeup water temp (exists) |
| `room_temp_c` | °C | 22.0 | room/ambient (exists) |
| `heater_power_w` | W | 0.0 | `Q_heater` (set via `set_heater`) |
| `particle_diffusion_d` | 1/s·(weight) | *calib* | diffusion coefficient `D` |
| `particle_knn_k` | – | 8 | neighbors per particle |
| `particle_bulk_relax_lambda` | 1/s | *calib* | `λ` bulk relaxation |
| `inlet_location` | [x,y,z] | – | inlet jet center (tank-local) for `S_i` |
| `heater_location` | [x,y,z] | – | heater center for `S_i` |

**Initial calibration target:** pick `(a_e, b_e)` and `u_wall_w_m2k` so the early-time response
reproduces the current effective time constant (`k_room + k_inflow ≈ 0.034/h`, τ ≈ 29 h) for the
default geometry, then refine against literature (Edinger surface-exchange coefficient `K`).

## 8. API Changes

- `POST /advance {real_dt_s}` — `temperature_c` becomes an **optional debug override**; normal calls
  omit it and the backend computes temperature. `snapshot` continues to return `temperature_c`.
- `POST /particles/register {positions:[[x,y,z]...], count}` — **new**. Builds/caches the KNN graph,
  initializes `T_i = T_bulk`, returns `{status, count, graph_hash}`.
- `GET /particles/values` — returns current cached `{temperature:[N], ...}`. (Supersedes the per-call
  recompute in `POST /particle-values`, which may remain as a stateless fallback.)
- `set_heater` action now sets `heater_power_w` (watts) consumed by `Q_heater` (no longer added to
  `T_room`).

## 9. Testing Strategy

Pure-math, no Kit/USD — new `extensions/aquacast.aquacast_composer_extensions/tests/test_thermal_dynamics.py`
(run with `pytest`, alongside `test_water_quality_dynamics.py`):

- `saturation_vapor_pressure`: monotone increasing; known anchor (e_s(20 °C) ≈ 2.34 kPa).
- `surface_heat_flux`: sign discipline — warm water above air loses sensible+evaporative+longwave heat
  (`Q_surf < 0`); equal temps with `RH < 1` still evaporates (net loss).
- Steady state: solving `Q_net(T*) = 0` gives a `T*` strictly between `T_inlet` and `T_room`
  (for `heater = 0`, surface losses present).
- `step_temperature_rk4`: relaxes monotonically toward `T*`; with surface terms disabled and only the
  two linear couplings active, RK4 matches the analytic `equilibrium_temperature` solution to
  tolerance (regression bridge to the old model).
- Energy/units: doubling `V` halves `dT` for the same `Q_net·dt`; `Q_heater` raises `T*`.
- `diffuse_step`: mean is conserved (mean-lock holds `mean(T_i) == T_bulk`); a hot boundary source
  propagates inward over successive steps; stable at the clamped `D`.

Backend-level: extend `test_water_quality_model.py` — `/advance` without `temperature_c` evolves
temperature; `set_heater` raises steady-state temperature; register→advance→values round-trips a
field whose mean equals the snapshot `temperature_c`.

## 10. Open Assumptions (confirm or override)

These close the surface-flux model; defaults are in §7 and assumed unless changed:

1. `air_temp_c = room_temp_c = 22 °C` (indoor air ≈ room).
2. `rel_humidity = 0.60`.
3. `air_speed_ms = 0.2` (weak indoor circulation).
4. `q_makeup_lph ≈ 220` as the starting makeup flow (the recirculation 2000 L/h does **not** cool the
   tank), then calibrated.
5. Default geometry `r = 1.2 m`, `h = 2.21 m` (≈ 10 m³); **must be reconciled with the actual USD
   water cylinder** — the registration-time consistency warn (D8) surfaces drift.

## 11. Implementation Order (for the follow-up plan)

1. `thermal_dynamics.py`: add `saturation_vapor_pressure`, `surface_heat_flux`, `net_heat_w`,
   `step_temperature_rk4`, `diffuse_step` (+ KNN graph builder). Pure-math, fully unit-tested first
   (TDD).
2. `wq_constants.json`: add §7 keys; `water_quality_model`: own `ThermalState` + `ParticleField`,
   wire into `_advance_one_substep`, fix the heater term.
3. `water_quality_backend.py`: `/particles/register`, `/particles/values`; deprecate `temperature_c`.
4. `water_quality_backend_client.py` + `main.py`: register particles on init, stop sending
   `temperature_c`, fetch field from `/particles/values`, add the USD-vs-config geometry warn, keep
   `temperature_to_rgb` extension-side.
5. Calibrate `(a_e, b_e, u_wall)` to the legacy time constant, then sanity-check against literature.
