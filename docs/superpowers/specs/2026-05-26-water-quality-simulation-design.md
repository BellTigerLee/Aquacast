# Water Quality Simulation (Physics-First Rule Engine) — Design

**Date:** 2026-05-26
**Component:** `extensions/aquacast.aquacast_composer_extensions`
(new `water_quality_dynamics.py`, new `water_quality_model.py`, new JSON catalogs under `data/`,
generalization of `WaterTempController` → `WaterQualityController` in `main.py`, menu additions in
`aquacast/aquacast_composer_extensions/extension.py`, knob additions in `global_variable.py`)
**Status:** Approved scope; pending implementation plan
**Builds on:** `2026-05-20-water-temperature-visualization-design.md` (the temperature feature is
generalized, not replaced)

---

## 1. Background

The Aquacast composer scene is a land-based RAS (recirculating aquaculture system) tank for North
Pacific Chum salmon (*Oncorhynchus keta*), grow-out stage. Today it has:

- A bulk water **temperature** model (`thermal_dynamics.step_temperature`, lumped heat balance) driven
  by room heat gain vs. cold inlet inflow, with a real-time inflow ON/OFF toggle.
- A runtime-authored point cloud `TemperatureParticlesInsideWater` (`UsdGeom.Points`, ~8001 points,
  session layer) carrying two vertex primvars: `displayColor` and `temperature`.
- Physical anchors already present in the stage (`stage_topology.json`):
  `/Root/Group/Water` (the water cylinder), `/Root/Group/TemperatureParticlesInsideWater`,
  `.../FishTank/Feedings` (feed input), `.../FishTank/inlet/Inlet_Trace_Source` (water exchange
  source), and six sensors `.../InWater/Components/Sensor_01 .. Sensor_06`.

We want to extend this single-variable thermal model into a **deterministic, physics-first water
quality rule engine**: with no measured data, evolve a coupled system of mass-balance ODEs from
initial conditions + parameters, and visualize the result in the digital twin. The differentiating
value is multivariate **silent failure** — e.g. nitrification slowly consuming alkalinity until pH
drifts down, or a warm day lowering oxygen saturation into hypoxia — failures that single-threshold
alarms and dashboard numbers miss.

### 1.1 How temperature works today (the pattern we generalize)

Per-particle temperature is **not** a spatial diffusion. Each particle's value is
`bulk_T + heat_delta · weightᵢ · spread`, where `bulk_T` is a single scalar evolved by the heat ODE,
`weightᵢ` is a static geometric weight fixed at authoring time, and `spread = 1 − exp(−elapsed·rate)`
is a global temporal fade-in. There is no neighbor coupling. This is a **bulk scalar + static spatial
visualization pattern**, which matches the well-mixed CSTR assumption. This design reuses exactly that
mechanism for the additional water-quality variables.

## 2. Goals

- **G1.** Evolve a coupled bulk (well-mixed, single-tank CSTR) ODE system for `DO`, `TAN`, `CO2`,
  `Alk` (with `T` continuing to come from the existing heat balance), deriving `pH` and toxic `NH3`
  each step. NO₂⁻ is designed as an optional seam, not implemented in this pass.
- **G2.** Couple the variables physically so the textbook silent-failure chains emerge on their own:
  feed → DO↓ / TAN↑ / CO2↑ → pH↓; nitrification → Alk↓ → long-term pH↓; warm water → DO_sat↓ → hypoxia;
  same TAN at higher pH/T → toxic NH₃↑.
- **G3.** Provide a **What-If action API** (Python functions + Kit menu) that operators invoke to
  perturb the running simulation: feed pulse, water-exchange rate, heater, biofilter on/off, stocking,
  and scenario presets.
- **G4.** **Accelerated, deterministic time:** a `TIME_SCALE` knob maps real seconds → simulated hours;
  integration advances in fixed sub-steps (explicit Euler). Same (action sequence + real-`dt` sequence)
  ⇒ bit-identical state.
- **G5.** **Modular, testable core:** all physics and time-stepping live in Omniverse-free modules
  (`water_quality_dynamics.py`, `water_quality_model.py`) covered by plain `pytest`, mirroring the
  `thermal_dynamics.py` / `fish_dynamics.py` boundary. Coefficients live in JSON catalogs, not code.
- **G6.** **Visualize one selectable variable at a time** on the existing particle cloud via a
  per-variable color ramp, switchable from a menu; store all variables as primvars; report the full
  variable vector at each of the six sensors.
- **G7.** **Backward compatible:** `ENABLE_WATER_QUALITY = False` restores prior behavior bit-for-bit;
  with it on, the default view variable is Temperature, so the existing teal-cloud-tracks-T behavior is
  preserved when other variables sit at safe baselines.
- **G8.** Session-layer USD writes only — nothing persists to disk, same convention as the fish and
  temperature controllers.

## 3. Non-Goals

- **True spatial transport / diffusion.** No per-particle neighbor coupling, PDE, or plume solver. The
  physics is 0-D (bulk scalars); "spread" is a visualization fade-in only.
- **NO₂⁻ two-step nitrification cascade.** State slot and equation shape are reserved (§6.5) but left
  inactive.
- **Network / web-viewer action bridge.** The action API is Python + menu only. The function surface is
  documented as a seam (§7.4) for a future `omni.kit.livestream` / `carb.events` bridge; no messaging
  code is written now.
- **Fish-behavior coupling.** Water quality does not yet alter `FishSwimController` motion. (Appetite
  feeds back into the *feed/quality* loop, not into swimming.)
- **Solids/TSS turbidity rendering.** The solids production constant is carried in the catalog for
  completeness, but rendering water cloudiness from TSS is deferred.
- **On-screen numeric HUD.** Sensors return structured values for logging / future UI; no numeric
  overlay is added.
- **Sharing controller state with `FishSwimController`.** The two controllers stay self-contained on
  disjoint prim sets.

## 4. Approach Overview

Five layers, generalizing the existing `WaterTempController` / `thermal_dynamics.py` split. Internal
units are fixed at **mg · L · h**; only API inputs/outputs are converted (units are the single largest
bug source per the source spec).

| Responsibility | Where it lives | Omniverse dep | Tests |
|---|---|---|---|
| Pure water-quality math (ODE RHS, `DO_sat`, `nh3_fraction`, `ph_from_carbonate`, `appetite_factor`, `mo2_base`, conversions) | new `water_quality_dynamics.py` | none | `pytest` |
| Rule-engine core (state vector + action queue + accelerated clock + fixed-Δt sub-step integrator) | new `water_quality_model.py` (`WaterQualityModel`) | none | `pytest` |
| Coefficient catalogs (conversion constants / Chum feed-rate lookup / scenario presets) | new JSON under `data/` | none | — |
| Particle cloud authoring + per-variable visualization + sensor sampling | `WaterQualityController` in `main.py` (generalized from `WaterTempController`) | yes | Kit manual |
| What-If action API + menu wiring | module-level functions in `main.py`; `MenuItemDescription`s in `extension.py` | yes | Kit manual |
| Toggles, time scale, initial conditions, system parameters, view selection, per-variable color ramps | `global_variable.py` additions | — | — |

The temperature heat balance (`thermal_dynamics.step_temperature`, `temperature_to_rgb`) is **reused
unchanged** to avoid regressing the working feature; `WaterQualityModel` calls it for the `T` component
and `water_quality_dynamics` for the coupled variables.

## 5. File Layout

**New (Omniverse-free, unit-tested):**
- `extensions/aquacast.aquacast_composer_extensions/water_quality_dynamics.py`
- `extensions/aquacast.aquacast_composer_extensions/water_quality_model.py`
- `extensions/aquacast.aquacast_composer_extensions/data/wq_constants.json`
- `extensions/aquacast.aquacast_composer_extensions/data/wq_feed_rate.json`
- `extensions/aquacast.aquacast_composer_extensions/data/wq_scenarios.json`
- `extensions/aquacast.aquacast_composer_extensions/tests/test_water_quality_dynamics.py`
- `extensions/aquacast.aquacast_composer_extensions/tests/test_water_quality_model.py`

**Modified:**
- `main.py` — `WaterTempController` → `WaterQualityController`; particle authoring adds N primvars;
  view-variable-driven color write; sensor sampling returns the full vector; new module-level action
  functions. Existing module functions (`start_water_temp_controller`, `stop_water_temp_controller`,
  `water_temp_controller_inflow_state`, `toggle_water_temp_controller_inflow`, `sample_water_temp_sensor`)
  are kept as thin aliases so `extension.py` call sites and any external callers keep working.
- `aquacast/aquacast_composer_extensions/extension.py` — view-variable submenu, action menu items,
  retain inflow toggle.
- `global_variable.py` — see §8.

> **Placement note.** Pure-math + model + catalogs live at the **extension root**, next to `main.py`,
> alongside `thermal_dynamics.py` / `fish_dynamics.py`. Per the repo's CLAUDE.md, the runtime loads
> `main.py` (and its siblings) via the `importlib.util.spec_from_file_location` loader, *not* through
> the Kit-packaged `aquacast/aquacast_composer_extensions/` module tree. Only menu wiring goes in
> `extension.py`. New top-level files are picked up via that loader; they are **not** added to
> `premake5.lua`'s `prebuild_link`.

## 6. Design Detail — physics

State vector (integrated): `DO, TAN, CO2, Alk` [mg/L; Alk as CaCO₃]. `T` [°C] comes from the existing
heat balance. Derived each step (not integrated): `pH`, `NH3`.

### 6.1 Intermediate quantities
```
B          = N · W                                              [kg biomass]
MO2_base   = mo2_a · W^mo2_w_exp · Q10^((T − T_ref)/10)         [mg O2/kg/h]   (mo2_a≈83, exp≈−0.14, T_ref=10)
F          = M_feed / tau_feed                                  [kg feed/h]    (metabolized feed rate, §6.6)
R_O2       = B · MO2_base + k_feed_O2 · F · 1e6                 [mg O2/h]      (respiration + SDA/microbial)
r_nitrif   = clip(k_nitrif · TAN, 0, VTR_max) · (1 if biofilter_on else 0)   [mg TAN/L/h]
P_TAN      = F · PC · tan_per_feed                              [kg TAN/h]     (tan_per_feed=0.092, PC≈0.45)
```

### 6.2 Coupled ODE system (mg/L/h)
```
d(DO)/dt  = kLa_O2 · (DO_sat(T) − DO)        # surface/aeration reaeration
          + (Q/V) · (DO_in − DO)             # inflow makeup (DO_in ≈ DO_sat)
          − R_O2 / V                         # fish respiration + feed metabolism
          − 4.57 · r_nitrif                  # nitrification O2 demand (4.57 mg O2 / mg TAN)

d(TAN)/dt = P_TAN · 1e6 / V                  # excretion (kg→mg)
          − (Q/V) · TAN                      # dilution (TAN_in ≈ 0)
          − r_nitrif                         # bacterial oxidation

d(CO2)/dt = 1.375 · R_O2 / V                 # respiration byproduct (1.375 mg CO2 / mg O2, RQ)
          − kLa_CO2 · (CO2 − CO2_eq)         # stripping by aeration
          − (Q/V) · CO2                      # dilution (CO2_in ≈ CO2_eq)

d(Alk)/dt = − 7.14 · r_nitrif                # nitrification alkalinity consumption (7.14 mg CaCO3 / mg TAN)
          + (Q/V) · (Alk_in − Alk)           # inflow makeup
```

### 6.3 Derived: pH (carbonate equilibrium, §5-1)
```
pH = pK1 + log10( Alk_mol / CO2_mol )
   Alk_mol = Alk[mg/L] / 50000      # 1 eq CaCO3 = 50000 mg ; carbonate alkalinity ≈ [HCO3⁻]
   CO2_mol = CO2[mg/L] / 44000      # 44000 mg / mol
   pK1 ≈ 6.35
```
→ CO2↑ ⇒ pH↓ (immediate); Alk↑ ⇒ pH↑. Guard `CO2_mol`, `Alk_mol` against ≤ 0 (clamp to a small ε).

### 6.4 Derived: NH₃ toxicity (§5-3)
```
pKa       = 0.09018 + 2729.92 / (T + 273.15)          # freshwater, temperature dependent
NH3_frac  = 1 / (1 + 10^(pKa − pH))
NH3       = TAN · NH3_frac                             # the toxic, un-ionized fraction
```
→ same measured TAN becomes far more toxic as pH↑ and T↑.

### 6.5 Oxygen saturation `DO_sat(T)`
Benson–Krause / APHA freshwater fit (decreasing in T), `Tk = T + 273.15`:
```
ln(DO_sat) = −139.34411 + 1.575701e5/Tk − 6.642308e7/Tk² + 1.243800e10/Tk³ − 8.621949e11/Tk⁴
```
Implementation may substitute a calibrated lookup; the only contract is monotone-decreasing in T. This
single term produces the "hot day ⇒ oxygen shortage" scenario.

### 6.6 Feeding dynamics
Two contributions to the metabolized feed rate `F`:
1. **Baseline appetite-modulated feeding:** `F_base = DFR(T, W) · B · appetite_factor(DO) / 24` [kg/h],
   where `DFR(T, W)` is the dome-shaped Chum feed-rate lookup [%BW/day] (bilinear interp over T, W) and
   `appetite_factor(DO) = clip((DO − DO_zero)/(DO_maxFI − DO_zero), 0, 1)`. DO↓ ⇒ feeding↓ — the
   self-limiting silent-failure feedback.
2. **What-If feed pulses:** a feed-metabolism pool `M_feed` [kg] with
   `dM_feed/dt = F_in − M_feed / tau_feed`. `apply_feed(mass_kg)` adds an impulse to `M_feed`; baseline
   feeding adds `F_base` to `F_in`. The metabolized rate driving water quality is `F = M_feed / tau_feed`,
   so a 1 kg pulse spreads its DO↓ / TAN↑ / CO2↑ load over ~`tau_feed` simulated hours rather than
   instantaneously.

### 6.7 NO₂⁻ seam (reserved, inactive)
Documented shape only, gated off by `WQ_ENABLE_NO2 = False`:
```
d(NO2)/dt = + r_nitrif                       # TAN→NO2 (1st oxidation)
            − no2_oxidation_rate             # NO2→NO3
            − (Q/V) · NO2
```

## 7. Design Detail — runtime

### 7.1 `water_quality_dynamics.py` (pure functions, no state, no Omniverse)
- `mo2_base(T, W, *, a, w_exp, q10, t_ref) -> float`
- `do_saturation(T) -> float`
- `appetite_factor(DO, *, do_zero, do_maxFI) -> float`
- `nitrification_rate(TAN, *, k_nitrif, vtr_max, biofilter_on) -> float`
- `tan_production(F, *, protein_content, tan_per_feed) -> float`
- `ph_from_carbonate(CO2, Alk, *, pk1) -> float`
- `nh3_fraction(T, pH) -> float`
- `derivatives(state, params) -> dict` — returns d(DO)/dt, d(TAN)/dt, d(CO2)/dt, d(Alk)/dt for one
  evaluation, given the full parameter set. Pure; takes T as an input (not integrated here).

### 7.2 `water_quality_model.py` — `WaterQualityModel` (stateful, no Omniverse)
- Holds the state vector, system parameters, the feed pool `M_feed`, biofilter/inflow/heater flags, and
  an **action queue**.
- `advance(real_dt_s)`: `sim_h = real_dt_s · TIME_SCALE`; `n = max(1, ceil(sim_h / SUBSTEP_H))`;
  `dt_sub = sim_h / n`. For each sub-step: drain due actions, integrate `T` via
  `thermal_dynamics.step_temperature(dt_sub, …)` (closed form), integrate `DO/TAN/CO2/Alk` via explicit
  Euler with `water_quality_dynamics.derivatives`, update `M_feed`. Clamp concentrations to ≥ 0.
- `apply_action(action)`: enqueue; `snapshot()`: return current state + derived pH/NH3 as a plain dict.
- `load_scenario(preset)`: replace state + params from a catalog entry, reset clock.
- Deterministic: no RNG. Given the same action sequence and the same `real_dt` sequence, produces
  identical state. Unit tests pass `real_dt` explicitly, removing wall-clock dependence.

### 7.3 `WaterQualityController` in `main.py` (generalized from `WaterTempController`)
- Owns the particle cloud and holds one `WaterQualityModel`.
- **Authoring** (`_author_water_quality_particles`, generalized from `_author_temperature_particles`):
  create `UsdGeom.Points`, then create one vertex `FloatArray` primvar per variable
  (`temperature, dissolved_oxygen, tan, co2, ph, alkalinity`, `+ nh3` derived) plus `displayColor`. Each
  particle gets a static per-variable weight `weightᵥ,ᵢ` anchored to a configured feature (TAN anchored
  near `Feedings`, CO2 / low-DO biased toward the fish/bottom band, temperature keeps its current
  side/bottom/internal modes). Weights are computed once at authoring; positions reuse the existing
  seeded sampling.
- **Per-frame** (`_on_update`): `model.advance(dt)`; throttled at `WQ_PARTICLE_UPDATE_INTERVAL_SECONDS`,
  write **all** variable primvars from `bulk_valueᵥ + ampᵥ · weightᵥ,ᵢ · spread`, and write
  `displayColor` for the **selected view variable only** using that variable's color ramp. `spread` is
  the same `1 − exp(−elapsed · rate)` fade-in. Session-layer edit context, as today.
- **Sensors** (`sample_quality_sensor`, generalized from `sample_temperature_sensor`): same O(N)
  nearest-particle search at any of `Sensor_01..06`, returning the full per-variable mean/min/max so the
  six sensors differ spatially (e.g. the sensor nearest `Feedings` reads higher TAN).

### 7.4 What-If action API (`main.py` module-level functions → model)
Each is null-safe (no-op when the controller/model is absent):
```
apply_feed(mass_kg)            set_water_exchange(q_lph)     set_inflow(enabled)     # generalizes toggle
set_heater(power)              set_biofilter(enabled)        set_stock(n, w_kg)
load_scenario(name)            get_quality_snapshot()        sample_quality_sensor(sensor_path=None)
```
**Network seam (deferred):** these signatures are the exact surface a future `omni.kit.livestream` /
`carb.events` bridge would forward from the web viewer. No messaging code is written now.

### 7.5 Menu wiring (`extension.py`, only when `ENABLE_WATER_QUALITY = True`)
- `Aquacast > Water Quality View > [Temperature | Dissolved O₂ | TAN | pH | CO₂]` — checkable group that
  sets `VIEW_VARIABLE` (controller is the source of truth via `ticked_fn`).
- `Aquacast > Water Quality Actions > [Feed pulse | Biofilter ON/OFF | Reset scenario…]`.
- Existing `Aquacast > Water Inflow` toggle retained, now routed through `set_inflow`.

### 7.6 Visualization color ramps
One `*_COLOR_STOPS` list per view variable (linear RGB, piecewise-linear via the existing
`temperature_to_rgb`, which is variable-agnostic). Suggested semantics: DO red(low/danger)→green(safe);
TAN green(low)→red(high); pH diverging around 7 (red acidic ↔ blue alkaline); CO₂ clear→red; temperature
keeps the current blue→teal→amber→red.

## 8. Configuration (`global_variable.py` additions)

```python
# ── Water quality simulation ────────────────────────────────────────────────
ENABLE_WATER_QUALITY = True          # supersedes ENABLE_WATER_TEMP_VIS; default view = Temperature
WQ_ENABLE_NO2        = False         # NO2 cascade seam (inactive)

# Time (accelerated clock + fixed sub-step)
WQ_TIME_SCALE   = 1.0                # simulated HOURS advanced per real-time SECOND.
                                     #   sim_h = real_dt_s · WQ_TIME_SCALE
                                     #   1.0 ⇒ a 3-day (72 h) pH drift plays in ~72 s; a τ_feed=4 h
                                     #   feed pulse plays in ~4 s. Raise for faster demos.
WQ_SUBSTEP_H    = 0.0167             # fixed integration step ≈ 1 simulated minute

# Initial conditions (mg/L; pH derived)
WQ_INIT_DO  = 9.0
WQ_INIT_TAN = 0.3
WQ_INIT_CO2 = 5.0
WQ_INIT_ALK = 120.0

# System parameters
WQ_TANK_VOLUME_L   = 10000.0
WQ_FISH_COUNT      = 200
WQ_FISH_WEIGHT_KG  = 1.0
WQ_FLOW_LPH        = 2000.0          # Q ; turnover Q/V = 0.2 / h
WQ_PROTEIN_CONTENT = 0.45            # PC
WQ_KLA_O2          = 2.0             # 1/h
WQ_KLA_CO2         = 1.5             # 1/h
WQ_K_NITRIF        = 0.8             # 1/h, first-order TAN oxidation
WQ_VTR_MAX         = 5.0             # mg/L/h, biofilter capacity cap
WQ_TAU_FEED_H      = 4.0             # feed-metabolism time constant
WQ_DO_MAXFI        = 7.0             # DO ensuring full appetite
WQ_DO_ZERO         = 3.0             # DO at which feeding stops
WQ_DO_IN           = 9.0             # inflow DO (≈ DO_sat at inlet temp)
WQ_CO2_EQ          = 0.5             # atmospheric CO2 equilibrium
WQ_ALK_IN          = 120.0
WQ_BIOFILTER_DEFAULT = True

# Visualization
WQ_VIEW_VARIABLE = "temperature"     # temperature | dissolved_oxygen | tan | co2 | ph
WQ_PARTICLE_UPDATE_INTERVAL_SECONDS = 0.12
DO_COLOR_STOPS  = [...]              # red(low) → green(high)
TAN_COLOR_STOPS = [...]              # green(low) → red(high)
PH_COLOR_STOPS  = [...]              # diverging around 7
CO2_COLOR_STOPS = [...]             # clear → red
# TEMP_COLOR_STOPS already exists.

# Per-variable spatial anchors (visualization only; not physics)
FEEDINGS_PRIM_PATH = "/Root/Group/Aquarium/AquariumComponents/FishTank/Feedings"
INLET_PRIM_PATH    = "/Root/Group/Aquarium/AquariumComponents/FishTank/inlet/Inlet_Trace_Source"
WQ_VIEW_AMPLITUDE  = { "tan": 1.0, "dissolved_oxygen": 1.0, "co2": 1.0, "ph": 0.3 }
```

The constants in §6.1 (`tan_per_feed=0.092`, `co2_per_o2=1.375`, `alk_per_tan=7.14`,
`o2_per_tan=4.57`, `o2_per_feed≈0.225`, `solids_per_feed≈0.275`, `mo2_a≈83`, `mo2_w_exp=−0.14`,
`q10≈2.5`, `pk1≈6.35`) live in `data/wq_constants.json`, the dome feed-rate table in
`data/wq_feed_rate.json`, and the presets in `data/wq_scenarios.json` (normal / high-temp spike /
pump-off / biofilter-off / overfeed). Catalogs are loaded with the same hot-reload-friendly approach as
`global_variable.py`.

## 9. Backward Compatibility

- `ENABLE_WATER_QUALITY = False` ⇒ controller does nothing, no menu entries, no USD writes; the
  Isosurface / particle cloud keep their authored values. The flag is read once at controller `start()`
  and at menu registration (extension reload needed to flip, matching `ENABLE_FISH_SWIMMING`).
- With it on and all variables at safe baselines, the default `WQ_VIEW_VARIABLE = "temperature"` makes
  the cloud track `T` exactly as before. The `temperature` primvar and `temperature_to_rgb` are
  unchanged.
- Old module function names (`start_water_temp_controller`, `toggle_water_temp_controller_inflow`,
  `sample_water_temp_sensor`, …) remain as aliases delegating to the generalized controller, so
  `extension.py` and any external callers continue to work.

## 10. Determinism

- All physics is closed-form (`T`) or fixed-step explicit Euler (`DO/TAN/CO2/Alk`); pH/NH₃ are pure
  functions. No RNG in the model — the only RNG is the seeded particle *position* sampling at authoring
  (already present), which does not affect bulk state.
- `advance()` is a pure function of `(state, real_dt, action_queue)`. The only non-determinism source is
  the `real_dt` sequence (Kit frame pacing), the same constraint the existing controllers accept; the
  sub-step count is derived deterministically from `real_dt · TIME_SCALE`.
- Unit tests inject `real_dt` (or `sim_h`) directly, so model tests are fully deterministic.

## 11. Verification

### 11.1 Automated (`pytest`, no Kit) — `tests/test_water_quality_dynamics.py`
| Test | Expectation |
|---|---|
| `test_do_sat_decreases_with_temperature` | `DO_sat(10) > DO_sat(20) > DO_sat(25)` |
| `test_appetite_factor_clips` | DO ≤ DO_zero ⇒ 0; DO ≥ DO_maxFI ⇒ 1; midpoint linear |
| `test_nh3_fraction_increases_with_pH_and_T` | monotone increasing in pH and in T |
| `test_ph_drops_when_co2_rises` | higher CO2 at fixed Alk ⇒ lower pH; higher Alk ⇒ higher pH |
| `test_nitrification_zero_when_biofilter_off` | `biofilter_on=False` ⇒ rate 0 |
| `test_nitrification_capped_at_vtr_max` | large TAN ⇒ rate == VTR_max |
| `test_tan_production_scales_with_feed_and_pc` | `P_TAN ∝ F · PC · 0.092` |
| `test_derivatives_units_signs` | feed pulse ⇒ d(DO)/dt < 0, d(TAN)/dt > 0, d(CO2)/dt > 0 |

### 11.2 Automated — `tests/test_water_quality_model.py`
| Test | Expectation |
|---|---|
| `test_time_scale_maps_real_to_sim_hours` | `advance(real_dt_s)` advances `real_dt_s · WQ_TIME_SCALE` sim-hours (e.g. `advance(2.0s)` at `TIME_SCALE=1.0` ⇒ +2 sim-h) |
| `test_substep_count_is_deterministic` | sub-step count = `max(1, ceil(sim_h / SUBSTEP_H))` |
| `test_reproducible_under_same_inputs` | identical (actions, real_dt seq) ⇒ identical snapshot |
| `test_biofilter_off_tan_runaway` | biofilter off + steady feed ⇒ TAN rises monotonically |
| `test_biofilter_on_alkalinity_decline` | biofilter on + feed ⇒ Alk declines, pH drifts down (silent failure) |
| `test_pump_off_accumulates_tan_and_co2` | `Q=0` ⇒ no dilution; TAN, CO2 accumulate |
| `test_high_temp_lowers_do_via_do_sat` | raise T ⇒ steady-state DO falls |
| `test_apply_feed_pulse_decays_over_tau_feed` | a single `apply_feed` perturbs F for ~`tau_feed` hours then relaxes |
| `test_concentrations_non_negative` | states clamp at ≥ 0 under aggressive parameters |
| `test_load_scenario_resets_state` | each preset yields its documented initial conditions |

Run: `pytest extensions/aquacast.aquacast_composer_extensions/tests/ -v`

### 11.3 Manual visual verification (Kit launch)
`./start_aquacast.sh --composer`, then:
1. **t=0, view=Temperature:** cloud renders teal (~14 °C); `[Aquacast WQ]` log shows the initial state
   vector. Identical to the prior temperature feature.
2. **Switch view → TAN; trigger `overfeed` scenario:** cloud shifts green→red over the accelerated
   clock; sensor near `Feedings` reports the highest TAN.
3. **Switch view → DO; trigger `high_temp_spike`:** DO falls (DO_sat↓), color trends toward the danger
   end; appetite (baseline feed) drops as DO crosses `DO_maxFI`.
4. **Switch view → pH; biofilter ON, steady feed:** TAN stays controlled but pH **slowly drifts down**
   as Alk is consumed — the silent-failure demo a threshold alarm misses.
5. **`set_water_exchange` up / `set_inflow` ON:** TAN, CO2 fall (dilution), DO recovers.
6. **`ENABLE_WATER_QUALITY = False`, restart:** no menu entries, cloud keeps authored color.

### 11.4 Regression
- `tests/test_fish_dynamics.py` and the existing thermal behavior keep passing.
- `FishSwimController` is untouched; controllers operate on disjoint prim sets.
- Kit-hosted tests in `config/extension.toml` are not modified.

## 12. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Explicit Euler instability for stiff DO/CO2 terms at large `dt_sub` | Fixed `SUBSTEP_H ≈ 1 sim-min`; sub-step count derived from `sim_h`; clamp states ≥ 0. Drop `SUBSTEP_H` or switch to RK4 if a fast term oscillates. |
| `pH` blows up when `CO2_mol` or `Alk_mol` → 0 | Clamp both to a small ε before the `log10`; unit-tested at boundaries. |
| Writing N FloatArrays × ~8001 points per tick is slow in pure Python | One combined loop; write only at `WQ_PARTICLE_UPDATE_INTERVAL_SECONDS`; recompute `displayColor` for the selected view variable only. Vectorize with numpy if profiling demands. |
| One cloud can show only one variable | Designed-in view selector + per-variable ramps; all variables stored as primvars and exposed via sensors regardless of the displayed one. |
| Coefficients are demo-tuned, not validated against this specific tank | All coefficients live in JSON catalogs and `global_variable.py`; defaults chosen for a watchable demo, documented as tunable, not as calibrated truth. |
| Renaming `WaterTempController` breaks callers | Keep old module-function aliases; update `extension.py` call sites in the same change; covered by manual launch step 1 + regression. |
| Non-physical config (e.g. `Q=0` with biofilter off) | Math stays valid (accumulation); emit a one-shot `carb.log_warn` for obviously inconsistent presets; never block. |
| Per-frame catalog/`global_variable` reload cost | Same per-frame `importlib` reload the existing controllers already pay; catalogs memoized by list/dict identity like `TEMP_COLOR_STOPS`. |
