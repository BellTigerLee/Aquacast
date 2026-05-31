# Fish Tank Kit UI — Design

**Date:** 2026-05-31
**Component:** `extensions/aquacast.aquacast_composer_extensions`
(additions to `extension.py`, `main.py`, `global_variable.py`, `dynamic_fish_spawn.py`; new pytest)
**Status:** Approved scope (4 design decisions confirmed); pending implementation review

## 1. Background

Fish are authored into the loaded USD stage at runtime by `DynamicFishSpawner` (see
[`2026-05-31-dynamic-fish-spawn-design.md`](./2026-05-31-dynamic-fish-spawn-design.md)).
On stage open it discovers every `Water` prim (one per tank), wipes that tank's
`Fishes` group, and re-spawns `DYNAMIC_FISH_COUNT_PER_TANK` salmon drawn from
`salmon_1.usd` / `salmon_2.usd` according to a mix ratio. There is **no interactive
way** to change a tank's population while Kit is running — the count is fixed by a
config knob and only re-applied on stage open.

This feature adds an **omni.ui panel** ("kit UI", in the same style as the existing
*Aquacast Water Quality Sensor* / *Water Quality View* windows in `extension.py`)
that lets an operator add and remove salmon per tank, per species, at runtime.

## 2. Goals

- A dockable omni.ui window, built and torn down exactly like the existing sensor
  panels, listing under the `Window > Aquacast` menu.
- Select a **tank** (from the live-discovered `Water` prims) and a **salmon species**.
- **ADD** / **DELETE** N fish of the selected species to/from the selected tank,
  where N is typed into a numeric field.
- **Clear All** — remove every fish (all species) from the selected tank.
- Live **count readout**: tank total against the cap, plus a per-species breakdown.
- Enforce the population rules in §4.
- Added/removed fish are picked up by the existing `FishSwimController` with no
  controller changes (same refresh path `DynamicFishSpawner` already uses).
- Edits go to the **session layer** only (consistent with all existing fish authoring);
  nothing is persisted to the source USD.

## 3. Non-Goals

- The **salmon species attribute editor** (age band / oxygen consumption / feed
  amount / preferred temperature per species). That is a *separate* feature, captured
  as a deferred task in §12 and **not implemented here**.
- Changing `FishSwimController` motion/boids logic or `DynamicFishSpawner` auto-spawn
  behavior on stage open.
- Adding new salmon assets or a third species. The UI exposes exactly the two
  configured salmon species; the species list is data-driven (`FISH_SPECIES`) so it
  can grow later without code changes.
- Persisting the manual population into the on-disk USD, or restoring it across stage
  open (stage open still re-applies the auto-spawn default — the UI edits on top of it).
- Multi-tank batch operations. Operations target the single selected tank.

## 4. Population Rules (confirmed decisions)

| # | Decision | Choice |
|---|---|---|
| D1 | Cap unit for the 30-max / 0-min gates | **Tank total** (sum of all species in the tank), per "탱크에 최대 30마리" |
| D2 | When N exceeds capacity / available | **Clamp** to what's possible (don't reject the whole op) |
| D3 | Selectable species | The two existing assets, with **friendly labels** (`salmon_1`→"Atlantic", `salmon_2`→"Chinook") |
| D4 | Count readout granularity | **Total + per-species** (e.g. `Total 18/30 · Atlantic 10 · Chinook 8`) |

Derived behavior (`T` = current tank total, `MAX` = `MAX_FISH_PER_TANK` = 30,
`S` = count of the *selected* species):

- **ADD N (species)**: effective = `min(N, MAX − T)`. If `T == MAX` → disabled / no-op.
  Adds `effective` fish of the selected species. Status reports clamping when
  `effective < N`.
- **DELETE N (species)**: effective = `min(N, S)`. Removes the `effective`
  highest-indexed fish of the selected species. If `S == 0` the op is a no-op
  (status: "선택한 종 없음"). The button itself is gated by tank total (D1): when
  `T == 0` it is disabled.
- **Clear All**: removes all fish in the tank. Disabled / no-op when `T == 0`.
- Button enable/disable (refreshed live):
  - ADD enabled ⟺ `T < MAX`
  - DELETE enabled ⟺ `T > 0`
  - Clear All enabled ⟺ `T > 0`
- `N` is clamped to `≥ 0`; `N == 0` is a no-op.

## 5. Architecture

Same two-layer split the repo already enforces: pure helpers stay Omniverse-free and
pytest-able; USD/omni.ui code stays in the Kit-bound layer.

| Responsibility | Where | Notes |
|---|---|---|
| Pure clamp arithmetic | `dynamic_fish_spawn.py` | `clamp_add_count`, `clamp_remove_count` — pytest-covered |
| Species table, tank discovery, USD add/remove/clear/count | `main.py` | New module-level functions; reuse existing `_find_water_prim_for_tank`, `_compute_water_bounds_with_axes`, `_set_single_reference`, `dynamic_fish_spawn.*` |
| omni.ui panel + rules wiring | `extension.py` | New `_build_fish_window` mirroring `_build_sensor_window` |
| Tuning knobs | `global_variable.py` | `MAX_FISH_PER_TANK`, `FISH_SPECIES`, `ENABLE_FISH_MANAGEMENT_UI`, update interval |

## 6. Configuration — `global_variable.py`

Added after the existing `DYNAMIC_FISH_*` block:

```python
ENABLE_FISH_MANAGEMENT_UI = True
MAX_FISH_PER_TANK = 30
FISH_MANAGEMENT_UI_UPDATE_INTERVAL_SECONDS = 0.5

# Selectable salmon species for the management UI.
# id = stable key stored on each fish prim's customData["aquacast:species"].
# Asset/scale default to the DYNAMIC_FISH_SALMON_* knobs above.
FISH_SPECIES = [
    {"id": "salmon_1", "label": "Atlantic",
     "asset": DYNAMIC_FISH_SALMON_1_PATH, "scale": DYNAMIC_FISH_SALMON_1_SCALE},
    {"id": "salmon_2", "label": "Chinook",
     "asset": DYNAMIC_FISH_SALMON_2_PATH, "scale": DYNAMIC_FISH_SALMON_2_SCALE},
]
```

`get_global_config()` re-reads this file on every call, so labels/cap can be tweaked
live without a restart (existing repo convention).

## 7. Pure Module — `dynamic_fish_spawn.py`

```python
def clamp_add_count(requested: int, current_total: int, max_total: int) -> int:
    # max(0, min(requested, max_total - current_total))

def clamp_remove_count(requested: int, available: int) -> int:
    # max(0, min(requested, available))
```

Deterministic, no Omniverse imports. Covered in `tests/test_dynamic_fish_spawn.py`.

## 8. USD Layer — `main.py`

### 8.1 Species table + tank discovery

```python
def get_fish_species() -> list[dict]
# Normalizes FISH_SPECIES → [{"id","label","asset"(resolved abspath),"scale"}]
# with a salmon_1/salmon_2 fallback if the constant is missing/garbage.

def list_fish_tanks() -> list[str]
# Discover Water-prim tank paths (same rule as DynamicFishSpawner._discover_tanks:
# prim name == "Water", excluding "/Looks/" and "/Materials/"), sorted.
```

### 8.2 Count

```python
def count_fish_in_tank(tank_path: str) -> dict
# Returns {"total": int, "by_species": {species_id: int}}.
# Walks <tank>/.../Fishes children matching ^Fish_\d+$ that are valid & active.
# Species id read from prim.GetCustomDataByKey("aquacast:species");
# falls back to inferring from the child "Asset" prim's reference target, else "unknown".
```

### 8.3 Add / Remove / Clear

```python
def add_fish(tank_path, species_id, count) -> dict     # {"added", "requested", "clamped"}
def remove_fish(tank_path, species_id, count) -> dict  # {"removed", "requested"}
def clear_fish(tank_path) -> dict                      # {"removed"}
```

- **add_fish**: `n = clamp_add_count(count, current_total, MAX_FISH_PER_TANK)`. Resolve
  the species asset/scale from `get_fish_species()`. Allocate names with
  `next_fish_indices(existing_child_names, n)`, positions with `sample_positions(...)`
  over the tank's water bounds, yaws with `sample_yaws(...)`. Author each
  `Fishes/Fish_NN` (Xform: Translate/RotateXYZ/Scale) + `Fish_NN/Asset` (reference to
  the species asset), and stamp `fish_prim.SetCustomDataByKey("aquacast:species", id)`.
  All inside `Usd.EditContext(stage, session_layer)`.
- **remove_fish**: `n = clamp_remove_count(count, species_count)`. Collect active
  `Fish_NN` of that species, sort by numeric index descending, remove the first `n`
  via `stage.RemovePrim()` + `SetActive(False)` (matching existing `_remove_composed_child`).
- **clear_fish**: remove all `Fish_NN` under the tank's `Fishes` group (reuses the same
  wipe used by `_spawn_fish_in_tank`).
- Each mutating op ends with the **existing refresh path**:
  `_stage_structure_cache.refresh()` (+ `export_topology_json()` when enabled) and
  `_fish_swim_controller._initialized = False; initialize_after_frames(1)`.

### 8.4 One-line touch to `_spawn_fish_in_tank`

Stamp `customData["aquacast:species"]` (`"salmon_1"`/`"salmon_2"` from `asset_index`)
on auto-spawned fish too, so the count readout and per-species DELETE work uniformly
for both auto- and manually-spawned fish. This is the only change to existing spawn code.

## 9. UI Layer — `extension.py`

New `_build_fish_window()` (called from `on_startup` after `_build_wq_view_window`,
guarded by `ENABLE_FISH_MANAGEMENT_UI`), mirroring the sensor window lifecycle:

```
ui.Window("Aquacast Fish Management")
└ VStack
  ├ Label "Fish Management"
  ├ HStack  Label "Tank:"     ComboBox(tanks)        # index → tank_path
  ├ HStack  Label "Species:"  ComboBox(species)      # index → species id (labels shown)
  ├ Label   "Total 0/30 · Atlantic 0 · Chinook 0"    # live count readout (D4)
  ├ HStack  Label "Qty:"      IntField(default 1)
  ├ HStack  Button "ADD"      Button "DELETE"
  ├ Button  "Clear All"
  └ Label   status/last-action (clamp messages)
```

- Menu item `Window > Aquacast/Fish Management` via `MenuItemDescription` +
  `omni.kit.menu.utils.add_menu_items` (same as `_register_sensor_window`).
- Tank ComboBox populated lazily once `list_fish_tanks()` is non-empty (assets finish
  loading after stage open); rebuilt if the discovered set changes.
- A periodic async refresh loop (interval `FISH_MANAGEMENT_UI_UPDATE_INTERVAL_SECONDS`,
  same pattern as `_refresh_loop`) updates the count readout and the enabled state of
  ADD/DELETE/Clear All per §4.
- Button callbacks read the selected tank/species/qty, call the `main.py` API, and
  write a status line (e.g. "ADD 5 → 2 added (clamped at 30)").
- `on_shutdown` teardown: cancel the refresh task, remove the menu item, destroy the
  window (new `_teardown_fish_window`, called from `on_shutdown`).

## 10. Error Handling

| Failure | Behavior |
|---|---|
| No stage / no tanks discovered | Tank combo shows "(no tank)"; buttons disabled; status "스테이지/탱크 없음" |
| `ENABLE_FISH_MANAGEMENT_UI` false | Window not built (early return), like the sensor window |
| Species asset file missing | That add is skipped with `carb.log_warn`; `added` reflects what was created |
| Qty ≤ 0 | No-op; status "수량을 1 이상 입력" |
| ADD at cap / DELETE or Clear at 0 | Gated by disabled buttons; API also no-ops defensively |
| `main.py` module failed to load | UI not built (`self._aquacast_main is None`), matching existing guards |

No silent swallowing in `main.py`: each skip emits one `carb.log_*` line.

## 11. Testing

- **Pytest (`tests/test_dynamic_fish_spawn.py`)** — extend with `clamp_add_count`
  (capacity exhausted → 0; partial clamp; negative inputs → 0) and `clamp_remove_count`
  (more than available → available; negative → 0). Run:
  `pytest extensions/aquacast.aquacast_composer_extensions/tests/ -v`.
- **USD/omni.ui code** — not unit-tested, per repo convention (can't run outside Kit).
- **Manual smoke** (`./start_aquacast.sh --composer`):
  1. `Window > Aquacast/Fish Management` opens the panel; tank + species combos populate.
  2. Type 5, ADD → 5 Atlantic appear and swim; readout `Total 5/30 · Atlantic 5 · Chinook 0`.
  3. ADD until 30 → ADD disables; further ADD clamps (status shows clamp).
  4. DELETE 3 Atlantic → readout drops by 3; DELETE of a 0-count species → no-op status.
  5. Clear All → tank empties, readout `Total 0/30`, DELETE/Clear disable, ADD enabled.

## 12. Deferred — Salmon Species Attribute UI (separate TASK)

Out of scope for this feature, tracked as its own task (do **not** build alongside the
tank UI). Design intent to capture when picked up:

- A separate omni.ui editor to configure **per-species** attributes:
  **age band**, **oxygen consumption**, **feed amount**, **preferred temperature**
  (and likely growth/biomass).
- Natural data home: extend each `FISH_SPECIES` entry with these fields, so this tank
  UI's species selector and the future attribute editor share one source of truth.
- Likely coupling points: `water_quality_dynamics.py` (oxygen/feed → DO/TAN load) and
  `thermal_dynamics.py` (preferred temp vs tank temp), and `WQ_FISH_COUNT`/
  `WQ_FISH_WEIGHT_KG` could be derived from live per-species counts × per-species weight.

## 13. Files Touched

| File | Change |
|---|---|
| `global_variable.py` | `+ENABLE_FISH_MANAGEMENT_UI`, `+MAX_FISH_PER_TANK`, `+FISH_SPECIES`, `+FISH_MANAGEMENT_UI_UPDATE_INTERVAL_SECONDS` |
| `dynamic_fish_spawn.py` | `+clamp_add_count`, `+clamp_remove_count` |
| `main.py` | `+get_fish_species`, `+list_fish_tanks`, `+count_fish_in_tank`, `+add_fish`, `+remove_fish`, `+clear_fish`, `+_fish_change_refresh` helper; one-line species `customData` stamp in `_spawn_fish_in_tank` |
| `aquacast/aquacast_composer_extensions/extension.py` | `+_build_fish_window`, `+_register_fish_window`, `+_show_fish_window`, `+_fish_refresh_loop`, button callbacks, `+_teardown_fish_window`; calls from `on_startup`/`on_shutdown` |
| `tests/test_dynamic_fish_spawn.py` | `+clamp_*` coverage |

## 14. Open Questions

None blocking. All four design decisions confirmed (§4). Species friendly names
(Atlantic/Chinook) are placeholders editable in `FISH_SPECIES`.
