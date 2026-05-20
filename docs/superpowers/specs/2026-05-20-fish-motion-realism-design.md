# Fish Motion Realism — Design

**Date:** 2026-05-20
**Component:** `extensions/aquacast.aquacast_composer` (`main.py` → `FishSwimController`)
**Status:** Approved scope; pending implementation plan

## 1. Background

`FishSwimController` already implements a boids-style flock (cohesion, alignment, separation, wander, boundary steering) inside the cylindrical `Water` prim. Every fish, however, shares one parameter set and one wander phase, so the school looks choreographed:

- **Speed is a single constant** (`water_radius * FISH_SWIM_SPEED_RADIUS_PER_SECOND`) for all fish.
- **Vertical motion** is a global `sin(now * 0.55 + phase)` weighted by `FISH_VERTICAL_WANDER_WEIGHT`. The per-fish `phase` only offsets the sine; frequency and amplitude are identical, so the school bobs in near-unison and stays at the same average depth.
- **Orientation** is computed by `_local_direction_to_rotate_xyz`, which hard-zeroes the X (roll) component. Fish turn flat — no banking.

This design improves motion realism along three axes (speed variation, banked turns, stratified depth) without touching body articulation, behavior states, or the boids math.

## 2. Goals

- Each fish behaves as an individual: distinct cruise speed, depth preference, and timing.
- Speed varies slowly and continuously around each fish's personal cruise.
- Fish roll into turns; banking is visible at sharp turns and absent at straight cruise.
- Behavior is deterministic given a seed (default seed = fish index) so visual diffs are reproducible across restarts.
- One toggle (`ENABLE_REALISM_DYNAMICS`) reproduces today's behavior bit-for-bit.

## 3. Non-Goals

- Tail/spine articulation, skinning, or animation rigs.
- Reactive bursts (escape behavior near walls or close neighbors). Speed is intrinsic-only.
- Multi-species, behavior states (feeding/idle/sleep), predator/prey.
- Refactor of the O(n²) neighbor loop.
- Authoring/UX work — parameters still live in `global_variable.py`.
- Promotion of per-fish state to a `FishAgent` class (deferred to a future refactor).

## 4. Approach Overview

Extend `FishSwimController` in place; do **not** introduce new classes. Three responsibilities are added:

| Responsibility            | Where it lives                       | Replaces / supplements                              |
|---------------------------|--------------------------------------|-----------------------------------------------------|
| Per-fish trait sampling   | `_make_fish_state` (extended)        | Supplements current `phase`/`head_length` dict      |
| Intrinsic speed variation | new helper `_intrinsic_speed`        | Constant `speed` in `_on_update`                    |
| Depth band attraction     | new helper `_depth_attraction`       | Replaces global vertical term in `_wander_vector`   |
| Banking                   | new helper `_compute_orientation`    | Replaces `_local_direction_to_rotate_xyz`           |

`_on_update` and `_wander_vector` are rewired but their overall control flow is unchanged. The boids math (`_desired_direction`, neighbor weighting) is untouched.

## 5. Design Detail

### 5.1 Per-fish trait sampling

At `_make_fish_state(prim, index)`, a deterministic `random.Random` is seeded from a stable hash of the fish's prim name combined with `FISH_RNG_BASE_SEED` (e.g. `random.Random(f"{FISH_RNG_BASE_SEED}:{prim.GetName()}")`). Name-based seeding (not index-based) means each fish keeps its personality when other fish are added or removed from the stage. This RNG is then used to sample:

| Trait                        | Default range                 | Purpose                                                |
|------------------------------|-------------------------------|--------------------------------------------------------|
| `cruise_speed_scale`         | `[0.85, 1.15]`                | Multiplier on global `FISH_SWIM_SPEED_RADIUS_PER_SECOND` |
| `speed_noise_amplitude`      | `[0.15, 0.35]`                | Fractional swing of intrinsic speed noise              |
| `speed_noise_freq_hz`        | `[0.05, 0.12]`                | Low frequency, so swings span ~10–20 s                 |
| `speed_noise_phase`          | `[0, 2π)`                     | Decorrelates noise across fish                         |
| `depth_band_center_norm`     | `[0.15, 0.85]`                | Preferred depth as fraction of `[water_min_z, water_max_z]` |
| `depth_band_half_width_norm` | `[0.08, 0.18]`                | Tolerated drift either side of the band                |
| `vertical_wander_freq_hz`    | `[0.07, 0.18]`                | Per-fish bob frequency, breaks unison                  |
| `vertical_wander_phase`      | `[0, 2π)`                     | Per-fish bob phase                                     |
| `bank_gain`                  | `[0.6, 1.0]`                  | Multiplier on yaw-rate → roll mapping                  |

Ranges are exposed as `(low, high)` tuples in `global_variable.py` (Section 6). All draws use the same per-fish RNG instance so each fish's full trait set is a deterministic function of its index.

### 5.2 Intrinsic speed variation

A new helper produces each fish's current scalar speed (units: world units / second):

```
base_speed   = water_radius * FISH_SWIM_SPEED_RADIUS_PER_SECOND * fish["cruise_speed_scale"]
swing        = 1 + fish["speed_noise_amplitude"] * sin(2π * fish["speed_noise_freq_hz"] * now + fish["speed_noise_phase"])
current_speed = base_speed * max(FISH_MIN_SPEED_FRACTION, swing)
```

`FISH_MIN_SPEED_FRACTION` (default `0.4`) floors the swing so fish never stall. Sum-of-sines was chosen over an Ornstein-Uhlenbeck process to match the existing code style (`_wander_vector` is also sine-based) and to remain deterministic without per-fish history.

`_on_update` replaces its single `speed` value with a per-iteration `_intrinsic_speed(fish, now)` call inside the existing fish loop.

### 5.3 Depth behavior

Vertical motion has two components, both per-fish:

**(a) Preferred-band attraction.** Each fish has a target depth derived once at init:

```
fish["preferred_z"] = water_min_z + (water_max_z - water_min_z) * depth_band_center_norm
fish["band_half"]   = (water_max_z - water_min_z) * depth_band_half_width_norm
```

A new `_depth_attraction(fish)` returns a unit-ish vector along `±Z`:

```
delta      = fish["preferred_z"] - position.z
strength   = clamp(delta / fish["band_half"], -1.0, 1.0)
return Gf.Vec3d(0.0, 0.0, strength)
```

Outside the band the pull is at full strength; inside it falls linearly to zero, so fish drift naturally within their band rather than locking to a depth.

**(b) Decorrelated wander.** `_wander_vector` keeps its horizontal sines (uniform across fish — that already looks fine) but replaces its vertical term with:

```
vertical = sin(2π * fish["vertical_wander_freq_hz"] * now + fish["vertical_wander_phase"])
```

Composition in `_desired_direction`:

```
flock    + wander * FISH_WANDER_WEIGHT
         + boundary * FISH_BOUNDARY_WEIGHT
         + depth_attraction * FISH_DEPTH_BAND_WEIGHT     # new
```

`FISH_DEPTH_BAND_WEIGHT` (default `0.45`) is tuned to be strong enough to maintain stratification but weaker than boundary steering, so wall avoidance still dominates near edges.

### 5.4 Banking on turns

`_local_direction_to_rotate_xyz` is replaced by `_compute_orientation(fish, prev_direction, dt)`. Pitch and yaw are computed identically to today. Roll is added:

```
# yaw_rate, signed, around world Z (radians/s)
prev_yaw    = atan2(-prev_direction.y, -prev_direction.x)
cur_yaw     = atan2(-direction.y,      -direction.x)
yaw_delta   = wrap_to_pi(cur_yaw - prev_yaw)
yaw_rate    = yaw_delta / max(dt, 1e-4)

target_roll = clamp(yaw_rate * fish["bank_gain"] * FISH_BANK_GAIN_GLOBAL,
                    -FISH_MAX_BANK_RADIANS,
                    +FISH_MAX_BANK_RADIANS)

# Low-pass so roll lags slightly and doesn't snap when direction lerps:
alpha           = _lerp_alpha(FISH_BANK_LERP_RATE, dt)
fish["roll"]    = fish.get("roll", 0.0) + (target_roll - fish.get("roll", 0.0)) * alpha
roll_degrees    = degrees(fish["roll"])
return Gf.Vec3f(roll_degrees, pitch_degrees, yaw_degrees)
```

Sign convention is verified empirically during implementation (the sign of `yaw_rate * bank_gain` may need flipping depending on the rotation order set on the prim — `RotateXYZ` order is established by `_set_compatible_fish_xform_order`).

The fish state dict gains `roll` (current filtered roll, in radians) and the controller stores `prev_direction` per fish across frames.

### 5.5 Composition in `_on_update`

Per fish, per frame:

1. `desired = _desired_direction(fish, now)`  *(unchanged math; now also includes depth attraction term)*
2. `fish.target_direction = lerp_direction(fish.target_direction, desired, direction_lerp_t)`
3. `prev_direction = fish.direction`
4. `fish.direction = rotate_toward(fish.direction, fish.target_direction, max_turn)`
5. `current_speed = _intrinsic_speed(fish, now)`
6. `fish.position = clamp_position(fish.position + fish.direction * current_speed * dt, fish.direction, fish.head_length)`
7. `_set_fish_transform(fish.prim, fish.position, fish.direction, fish, prev_direction, dt)`  *(transform setter forwards to `_compute_orientation`)*

## 6. Configuration (`global_variable.py` additions)

```python
ENABLE_REALISM_DYNAMICS = True

FISH_RNG_BASE_SEED = 1
FISH_MIN_SPEED_FRACTION = 0.4

FISH_CRUISE_SPEED_SCALE_RANGE         = (0.85, 1.15)
FISH_SPEED_NOISE_AMPLITUDE_RANGE      = (0.15, 0.35)
FISH_SPEED_NOISE_FREQ_HZ_RANGE        = (0.05, 0.12)

FISH_DEPTH_BAND_CENTER_NORM_RANGE     = (0.15, 0.85)
FISH_DEPTH_BAND_HALF_WIDTH_NORM_RANGE = (0.08, 0.18)
FISH_DEPTH_BAND_WEIGHT                = 0.45

FISH_VERTICAL_WANDER_FREQ_HZ_RANGE    = (0.07, 0.18)

FISH_BANK_GAIN_RANGE                  = (0.6, 1.0)
FISH_BANK_GAIN_GLOBAL                 = 0.35
FISH_MAX_BANK_RADIANS                 = 0.6   # ~34°
FISH_BANK_LERP_RATE                   = 3.0
```

`FISH_VERTICAL_WANDER_WEIGHT` stays for backward compatibility but is unused when `ENABLE_REALISM_DYNAMICS=True`; the depth attraction system takes over.

## 7. Backward Compatibility

`ENABLE_REALISM_DYNAMICS = False` selects a legacy code path that:

- Skips per-fish trait sampling (`_make_fish_state` returns today's dict shape).
- Uses the global `speed` value in `_on_update`.
- Calls the original `_local_direction_to_rotate_xyz` (roll always 0).
- Uses the original vertical term in `_wander_vector`.
- Omits the depth attraction term from `_desired_direction`.

This is a runtime branch, not a separate module — kept tight enough that we can delete the legacy path later if the new behavior holds up. The flag is read **exactly once per frame, in `_on_update`**, and propagated through method arguments to `_wander_vector`, `_desired_direction`, and the transform setter. No other site reads the flag, so the legacy/new dispatch is centralized.

## 8. Determinism

- Trait sampling uses one `random.Random(f"{FISH_RNG_BASE_SEED}:{prim_name}")` per fish, isolated from the global `random` module.
- All per-frame motion is closed-form sine-based; no internal RNG.
- Implication: the same kit launch with the same `Fish_N` set and the same seed reproduces frame-for-frame motion, modulo the variable `dt` from `_on_update` (capped at 50 ms today).

## 9. Verification

Manual:

1. Launch with `--composer` and `ENABLE_REALISM_DYNAMICS=True`. Watch ≥30 s. Confirm by eye: (a) different fish at different depths, (b) different bob rhythms, (c) visible roll on tight turns, (d) no roll while cruising straight.
2. Set `ENABLE_REALISM_DYNAMICS=False`, relaunch. Confirm motion matches the current `main` branch behavior (no roll, uniform speed, shared bob).
3. Set `FISH_RNG_BASE_SEED` to two different values, relaunch each. Confirm visibly different individual personalities.

Automated (kit test harness, optional in this iteration):

- Existing tests in `tests/test_app_startup.py` and `tests/test_app_extensions.py` must continue passing.
- No new integration tests added in this iteration — visual verification is the binding check.

## 10. Risks & Mitigations

| Risk                                                                  | Mitigation                                                                 |
|-----------------------------------------------------------------------|----------------------------------------------------------------------------|
| Banking sign is wrong (fish roll *out* of turns)                      | Flip `bank_gain` sign during verification; document the chosen sign.       |
| Depth attraction overpowers boundary, fish push into walls            | `FISH_DEPTH_BAND_WEIGHT < FISH_BOUNDARY_WEIGHT`; verified in scenario (1). |
| Speed noise drives fish too slow when stacking with `FISH_BOUNDARY_WEIGHT` slowdowns | `FISH_MIN_SPEED_FRACTION` floors swing; `_on_update` keeps no other slowdown. |
| Per-fish RNG state diverges from index ordering when fish are added/removed mid-stage | Reseed on each `initialize` call — same fish name → same seed.            |
| `main.py` grows further (already 27 KB)                               | Three new helpers, ~120–150 lines net; acceptable. Class extraction deferred. |
