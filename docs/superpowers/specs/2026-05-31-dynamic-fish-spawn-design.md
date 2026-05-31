# Dynamic Fish Spawn — Design

**Date:** 2026-05-31
**Component:** `extensions/aquacast.aquacast_composer_extensions` (new `dynamic_fish_spawn.py` + additions to `main.py`, `global_variable.py`, `extension.py`)
**Status:** Approved scope; pending implementation plan

## 1. Background

Today the school of fish that `FishSwimController` animates is whatever was pre-authored into the loaded USD stage (currently `Fish_01`, `Fish_02` under `/Root/Group/Aquarium/AquariumComponents/FishTank/InWater/Fishes/`). There is no way to scale the school up or down without editing the USD by hand, and no way to swap species without re-authoring the references. Upcoming multi-tank work needs to populate each tank at runtime with a configurable number of fish, drawn from a small library of salmon assets, with no USD edits per tank.

`assets/salmon_1.usd` and `assets/salmon_2.usd` are the two salmon models that should be used. They have not yet been referenced anywhere in the project.

The static `Fish_01`/`Fish_02` prims in the current stage are slated to be removed from the USD source; this design treats them as out of scope and does not depend on their presence or absence.

## 2. Goals

- Spawn N fish into a named tank at runtime, with no USD-side edits per tank.
- Configure N per tank via an environment variable (primary control) with a `global_variable.py` fallback.
- Uniform scale (default 10.0) applied at spawn time.
- Mix between `salmon_1.usd` and `salmon_2.usd` controlled by a single ratio (env-overridable).
- Initial positions uniformly distributed inside the water cylinder.
- Spawned fish are automatically picked up by the existing `FishSwimController` (no controller changes).
- The single tank function is reusable so that the upcoming multi-tank refactor only needs to call it once per tank.
- Opt-in: when no env var is set and the `global_variable.py` default is 0, behavior is bit-for-bit unchanged.

## 3. Non-Goals

- Modifying `FishSwimController` motion logic, the boids math, or per-fish trait sampling.
- Tank discovery beyond "find prims named `FishTank` or `FishTank_\d+` under `AquariumComponents`". A richer tank registry is deferred to the multi-tank effort.
- Persisting spawned fish into the on-disk USD. Spawns are session-layer only and re-generate on every stage open.
- Removing the existing static `Fish_01`/`Fish_02` from USD. Done separately.
- UI/menu controls. Configuration is environment + `global_variable.py` only.
- Animation rigs, materials, LODs, collision — whatever ships in `salmon_1.usd` / `salmon_2.usd` is what's rendered.
- Per-fish asset variation beyond the two salmon files (no random scale, no random color, etc.).

## 4. Approach Overview

Split into a **pure-helper module** (Omniverse-free, pytest-able) and a **thin USD-bound layer** in `main.py`, following the same separation principle CLAUDE.md already enforces between `fish_dynamics.py` and `FishSwimController`.

| Responsibility | Where it lives | Notes |
|---|---|---|
| Env/config parsing, asset assignment, position sampling, index allocation | `dynamic_fish_spawn.py` (new) | Pure functions; no `omni`/`pxr`/`carb` imports |
| USD prim creation, reference binding, xform op authoring | `main.py` `_spawn_fish_in_tank()` | USD calls only; delegates all decisions to the pure module |
| Stage-event subscription, tank discovery, topology refresh | `main.py` `DynamicFishSpawner` class | Mirrors `StageStructureCache` / `FishSwimController` lifecycle pattern |
| Bootstrap | `extension.py` `on_startup` | Adds `start_dynamic_fish_spawner()` immediately before `start_fish_swim_controller()` |
| Tuning knobs | `global_variable.py` | New constants, all overridable via env var |

The spawner runs on each stage `OPENED` event, into the **session layer**, so spawns are transient (gone on stage close) and never written to the source USD.

## 5. Configuration Surface

All env vars take precedence over `global_variable.py`. Env value parse failures fall back to default with a `carb.log_warn`.

| Knob | Env var | `global_variable.py` constant | Default | Notes |
|---|---|---|---|---|
| Fish per tank | `AQUACAST_DYNAMIC_FISH_COUNT` | `DYNAMIC_FISH_COUNT_PER_TANK` | `0` | `0` ⇒ feature disabled, no behavior change |
| Uniform scale | `AQUACAST_DYNAMIC_FISH_SCALE` | `DYNAMIC_FISH_SCALE` | `10.0` | Applied as `(s, s, s)` scale op at spawn |
| `salmon_1` mix ratio | `AQUACAST_SALMON_MIX` | `DYNAMIC_FISH_SALMON_1_RATIO` | `0.5` | Clamped to `[0.0, 1.0]`; `1.0` = all `salmon_1`, `0.0` = all `salmon_2` |
| `salmon_1` asset path | `AQUACAST_SALMON_1_ASSET` | `DYNAMIC_FISH_SALMON_1_PATH` | `~/cs-project/assets/salmon_1.usd` | `~` expanded; missing file ⇒ that fish skipped + warn |
| `salmon_2` asset path | `AQUACAST_SALMON_2_ASSET` | `DYNAMIC_FISH_SALMON_2_PATH` | `~/cs-project/assets/salmon_2.usd` | Same as above |
| Seed | _(reused)_ | `FISH_RNG_SEED` | `42` | Reuses existing constant for deterministic placement + asset assignment |

**Why opt-in default 0?** Avoids any visual regression on environments that don't yet know about this feature. User explicitly turns it on with `AQUACAST_DYNAMIC_FISH_COUNT=N`.

## 6. Pure Module — `dynamic_fish_spawn.py`

Zero Omniverse imports. All functions are deterministic given the same inputs and seed.

```python
def resolve_count(env_value: str | None, default: int) -> int
def resolve_scale(env_value: str | None, default: float) -> float
def resolve_mix_ratio(env_value: str | None, default: float) -> float
def resolve_asset_path(env_value: str | None, default: str) -> str   # ~ expansion + abspath

def next_fish_indices(
    existing_names: Iterable[str],
    count: int,
    prefix: str = "Fish_",
) -> list[int]
# Scan names for ^{prefix}\d+$, find max N, return [N+1, N+2, ..., N+count].
# If none match, return [1, 2, ..., count].

def assign_assets(
    count: int,
    salmon_1_ratio: float,
    seed: int,
) -> list[int]
# Returns a list of length `count` where each element is 0 (salmon_1) or 1 (salmon_2).
# Builds floor(count*ratio) zeros + remainder ones, then shuffles with seeded RNG.
# Guarantees the visual mix matches the ratio within rounding.

def sample_positions(
    count: int,
    water_radius: float,
    water_min_z: float,
    water_max_z: float,
    seed: int,
) -> list[tuple[float, float, float]]
# Uniform distribution inside the cylinder:
#   r = R * sqrt(U(0,1)), theta = U(0, 2pi) -> x = r*cos, y = r*sin
#   z = U(water_min_z, water_max_z)
# Seeded RNG (separate stream from assign_assets so changing count doesn't shift asset choices).
```

## 7. USD Layer — `main.py` additions

### 7.1 `_spawn_fish_in_tank()`

```python
def _spawn_fish_in_tank(
    stage,                              # Usd.Stage
    tank_path: str,                     # e.g. "/Root/Group/Aquarium/AquariumComponents/FishTank"
    count: int,
    *,
    scale: float,
    asset_paths: tuple[str, str],       # (salmon_1, salmon_2) absolute
    mix_ratio: float,
    seed: int,
    water_bounds: tuple[float, float, float] | None = None,  # (radius, min_z, max_z)
) -> list[str]:
    """Spawn `count` salmon prims under `<tank_path>/InWater/Fishes/`.

    Writes to the session layer so spawns are transient. Returns the prim paths
    that were created (may be < count if asset files are missing).
    """
```

Pseudocode:
```
parent_path = tank_path + "/InWater/Fishes"
ensure Xform exists at parent_path

if water_bounds is None:
    bounds = _compute_water_bounds_for_tank(stage, tank_path)
    if bounds is None: log_warn + return []

existing = [child.GetName() for child in stage.GetPrimAtPath(parent_path).GetChildren()]
indices = next_fish_indices(existing, count)
asset_choices = assign_assets(count, mix_ratio, seed)
positions = sample_positions(count, *bounds, seed=seed + 1)
yaws = seeded uniform(0, 360) for each fish (seed + 2)

with Usd.EditContext(stage, stage.GetSessionLayer()):
    for i in range(count):
        asset_idx = asset_choices[i]
        asset_uri = asset_paths[asset_idx]
        if not os.path.exists(asset_uri):
            log_warn, skip
            continue
        fish_path = f"{parent_path}/Fish_{indices[i]:02d}"
        prim = stage.DefinePrim(fish_path, "Xform")
        prim.GetReferences().AddReference(asset_uri)
        xform = UsdGeom.Xformable(prim)
        xform.ClearXformOpOrder()
        xform.AddTranslateOp().Set(Gf.Vec3d(*positions[i]))
        xform.AddRotateXYZOp().Set(Gf.Vec3f(0.0, 0.0, yaws[i]))
        xform.AddScaleOp().Set(Gf.Vec3f(scale, scale, scale))
        created.append(fish_path)
return created
```

`_compute_water_bounds_for_tank()` extracts the radius/z computation already in `FishSwimController` (around the `min_v`/`max_v` lines near main.py:661) into a small helper so both the spawner and the controller share it. No behavior change for the controller.

### 7.2 `DynamicFishSpawner` class

Mirrors `StageStructureCache` (main.py:928) structure:

```python
class DynamicFishSpawner:
    def __init__(self): ...
    def start(self):
        subscribe to usd_context stage event stream
    def stop(self):
        unsubscribe
    def _on_stage_event(self, event):
        if event.type == OPENED:
            self._spawn_all_tanks()
    def _spawn_all_tanks(self):
        count = resolve_count(env, default)
        if count <= 0: return
        scale = resolve_scale(env, default)
        mix = resolve_mix_ratio(env, default)
        assets = (resolve_asset_path(...), resolve_asset_path(...))
        seed = int(get_global_config("FISH_RNG_SEED", 42))
        for tank_path in self._discover_tanks():
            _spawn_fish_in_tank(stage, tank_path, count,
                                scale=scale, asset_paths=assets,
                                mix_ratio=mix, seed=seed)
        # Force topology refresh so FishSwimController can find the new fish
        if _stage_structure_cache is not None:
            _stage_structure_cache._capture()
            if should_export_stage_topology_json():
                _stage_structure_cache.export_topology_json()
    def _discover_tanks(self):
        # Walk topology for prim names matching ^FishTank(_\d+)?$ under
        # /Root/Group/Aquarium/AquariumComponents (or whatever the cached snapshot exposes).
        # Returns list of path strings.
```

Module-level start/stop helpers next to the existing `start_fish_swim_controller` (main.py:76):
```python
def start_dynamic_fish_spawner(): ...
def stop_dynamic_fish_spawner(): ...
```

### 7.3 Naming and discovery contract with `FishSwimController`

- Spawned prims are named `Fish_NN` (zero-padded 2-digit, three-digit when N≥100).
- `next_fish_indices` skips any indices already taken by other `Fish_\d+` children (e.g., leftover static prims during the transition before the static USD edit lands).
- `FishSwimController._find_fish_prims()` already matches `^Fish_\d+$` via the topology JSON path (main.py:666–691). With topology re-capture forced after spawn, no controller-side change is required.

## 8. Bootstrap — `extension.py`

In `CreateSetupExtension.on_startup` (line 131), insert one call directly before the existing `start_fish_swim_controller()` (line 180):

```python
self._dynamic_fish_spawner = aquacast_main.start_dynamic_fish_spawner()
self._fish_swim_controller = aquacast_main.start_fish_swim_controller()
```

Stop pair added to `on_shutdown` next to the existing `stop_fish_swim_controller` (extension.py:912–914).

Subscription order to the stage event stream is not strictly guaranteed, but **correctness does not depend on it** — see §9.

## 9. Topology Refresh Race

Three subscribers fire on `OPENED`: `StageStructureCache`, `DynamicFishSpawner`, `FishSwimController`. Possible orderings:

- **Spawn-before-controller:** controller sees the new fish in the (re-captured) topology → picks them up first try.
- **Controller-before-spawn:** controller's initial scan finds 0 fish, falls into its existing `FISH_INIT_RETRY_SECONDS` retry loop (main.py:638), retries after spawn + topology export complete → picks them up on retry.

Both paths converge on the same end state. No new synchronization primitive is needed. The only requirement is that `DynamicFishSpawner._spawn_all_tanks()` ends by triggering `StageStructureCache._capture()` + `export_topology_json()` so the JSON cache reflects the spawn before the controller's next retry tick.

## 10. Idempotency

Spawns go to the session layer, which is wiped on stage close, so re-opening the same stage simply re-spawns from scratch. If `OPENED` fires twice for the same stage instance (rare), the second invocation's `next_fish_indices` will see the prior spawn's prims and allocate fresh indices after them — counts grow rather than colliding. This is acceptable for the rare edge case; a sharper guard (stage-id memoization) is not justified.

## 11. Error Handling

| Failure | Behavior |
|---|---|
| `count <= 0` after parsing | Return silently (feature off). |
| Env var parses to garbage (`"abc"`) | Fall back to default, `carb.log_warn` once. |
| Asset file missing | Skip that fish, `carb.log_warn` with path, continue with the rest. Returned list reflects what actually got created. |
| Tank prim not in topology yet | Log info, skip this `OPENED` cycle. Next stage event retries. |
| Water bounds can't be computed (no `Water` prim) | Log warn, skip spawn. Controller's own retry loop will eventually surface the same issue and log there. |
| `DefinePrim` / `AddReference` raises | Log warn with prim path + exception, continue with remaining fish. |

No silent swallowing — every skip emits exactly one `carb.log_*` line so the cause is grep-able in the Kit log.

## 12. Testing

### 12.1 Pure-module pytest — `tests/test_dynamic_fish_spawn.py`

Runs via `pytest extensions/aquacast.aquacast_composer_extensions/tests/`. No Kit/USD dependencies.

- `resolve_count`: negative → 0; missing env → default; non-numeric → default with warn assertion not required (log mocking out of scope).
- `resolve_scale`: same shape as count, float-typed, negative → 0.0.
- `resolve_mix_ratio`: missing → default; out of `[0,1]` → clamped.
- `resolve_asset_path`: `~` expansion; absolute path passthrough.
- `next_fish_indices`: empty input → `[1..count]`; mixed input with `Fish_03`, `Fish_07`, `Other_99` → starts at 8; non-matching names ignored.
- `assign_assets`: ratio 1.0 → all 0s; ratio 0.0 → all 1s; ratio 0.5 with count 10 → exactly 5 of each; reproducible across calls with same seed; different seed → different order.
- `sample_positions`: all points satisfy `x²+y² ≤ R²` and `min_z ≤ z ≤ max_z`; reproducible with same seed; covers the cylinder (mean of N=10000 samples within tolerance).

### 12.2 USD-bound code (not unit-tested)

`_spawn_fish_in_tank`, `DynamicFishSpawner`, and the `extension.py` hook follow the project's existing convention of not having pure-pytest coverage for Kit/USD-bound code (per CLAUDE.md).

### 12.3 Manual smoke check

```bash
AQUACAST_DYNAMIC_FISH_COUNT=5 ./start_aquacast.sh --composer
```

Expected:
- Kit log contains `[Aquacast] Dynamic fish spawned: count=5 tank=/Root/...` exactly once per tank.
- Viewport shows 5 salmon at scale 10, scattered inside the water cylinder, with a mix matching `AQUACAST_SALMON_MIX` (default 0.5).
- Within `FISH_INIT_RETRY_SECONDS` after stage open, the salmon begin swimming under `FishSwimController`.
- Regenerated `stage_topology.json` (if `EXPORT_STAGE_TOPOLOGY_JSON=True`) lists the new `Fish_NN` entries under `Fishes`.
- Disabling: `AQUACAST_DYNAMIC_FISH_COUNT=0 ./start_aquacast.sh` → no spawn log, no new prims, identical viewport to pre-change baseline.

## 13. Forward Compatibility — Multi-Tank

The single-tank function `_spawn_fish_in_tank(stage, tank_path, count, ...)` is the unit of reuse. When multi-tank lands:

1. `_discover_tanks()` already returns a list — multi-tank just makes it return >1 entry.
2. Per-tank count overrides can be added later by reading `AQUACAST_DYNAMIC_FISH_COUNT_<TANK_NAME>` before falling back to the global default. Not implemented now (YAGNI).
3. Asset mix and scale stay global for now; per-tank overrides can follow the same pattern if a need surfaces.

No structural change anticipated when multi-tank work begins — only the discovery function gains real entries.

## 14. Open Questions

None. All ambiguities resolved during brainstorming:
- Static `Fish_01`/`Fish_02` ⇒ user removes from USD separately; this design is agnostic.
- Spawn trigger ⇒ per-tank function + auto on extension load.
- Asset mix ⇒ env-controlled ratio.
- Initial position ⇒ uniform random inside cylinder.

## 15. Files Touched

| File | Change |
|---|---|
| `extensions/aquacast.aquacast_composer_extensions/dynamic_fish_spawn.py` | **new** — pure helpers |
| `extensions/aquacast.aquacast_composer_extensions/main.py` | `+_spawn_fish_in_tank`, `+DynamicFishSpawner`, `+start_dynamic_fish_spawner`, `+stop_dynamic_fish_spawner`, `+_compute_water_bounds_for_tank` (extract); existing `FishSwimController` water-bounds code refactored to call the new helper |
| `extensions/aquacast.aquacast_composer_extensions/global_variable.py` | `+DYNAMIC_FISH_COUNT_PER_TANK`, `+DYNAMIC_FISH_SCALE`, `+DYNAMIC_FISH_SALMON_1_RATIO`, `+DYNAMIC_FISH_SALMON_1_PATH`, `+DYNAMIC_FISH_SALMON_2_PATH` |
| `extensions/aquacast.aquacast_composer_extensions/aquacast/aquacast_composer_extensions/extension.py` | Add `start_dynamic_fish_spawner()` before `start_fish_swim_controller()` in `on_startup`; matching stop in `on_shutdown`; track handle on `self._dynamic_fish_spawner` |
| `extensions/aquacast.aquacast_composer_extensions/tests/test_dynamic_fish_spawn.py` | **new** — pure pytest coverage of `dynamic_fish_spawn.py` |
