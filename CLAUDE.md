# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo contains a single NVIDIA Omniverse Kit extension, `aquacast.aquacast_composer`, plus a launcher script. It is NOT a standalone app — it is mounted into the sibling `kit-app-template` checkout at `~/cs-project/kit-app-template/` via `--ext-folder`. The two `.kit` apps (`aquacast.aquacast.kit`, `aquacast.aquacast_streaming.kit`) that consume this extension live in `kit-app-template/source/apps/`, not here.

This means: the extension cannot be imported, run, or smoke-tested in isolation. Anything that touches `carb`, `omni.*`, or `pxr` only works when launched through `start_aquacast.sh`.

## Running

```bash
./start_aquacast.sh              # default: streaming kit, no window
./start_aquacast.sh --composer   # interactive composer (window)
./start_aquacast.sh --streaming  # explicit streaming mode
```

The script shells out to `~/cs-project/kit-app-template/repo.sh launch <kit> -- --ext-folder ./extensions --enable aquacast.aquacast_composer`. If `kit-app-template` is missing or unbuilt, launch will fail — that is the host project, fix it there, do not vendor anything into this repo.

## Tests

There are two distinct test layers — only one is runnable from this repo:

- **`extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py`** — pure-math unit tests with no Kit/USD/Omniverse dependencies. Run directly:
  ```bash
  pytest extensions/aquacast.aquacast_composer/tests/ -v
  pytest extensions/aquacast.aquacast_composer/tests/test_fish_dynamics.py::test_wrap_to_pi_wraps_above_pi
  ```
- **`extensions/aquacast.aquacast_composer/aquacast/aquacast_composer/tests/`** — Kit-hosted tests (`test_app_startup.py`, `test_app_extensions.py`) declared in `config/extension.toml` under `[[test]]`. These require `omni.kit.ui_test` and run through `repo.sh test` in `kit-app-template`, not pytest. Do not try to invoke them from here.

The split is deliberate: keep pure-math helpers in `fish_dynamics.py` so they stay testable without Kit. New motion-math logic should follow that pattern — if a function only needs `math`/`random`, put it in `fish_dynamics.py` and add a plain pytest, not a Kit test.

## Architecture

Two layers, deliberately decoupled by a manual module loader:

1. **`aquacast/aquacast_composer/extension.py`** — the `omni.ext.IExt` entry point (`CreateSetupExtension`) that Kit discovers via `[[python.module]]` in `config/extension.toml`. Handles menu/layout/viewport setup. On startup it calls `_load_aquacast_main_module()` which uses `importlib.util.spec_from_file_location` to load `main.py` from two directories up — this is **intentional**, not a bug. It exists so the runtime logic in `main.py` can live next to `global_variable.py` (the tweakable knobs file) without being inside the Kit-packaged Python module tree.

2. **`main.py`** — the actual runtime. Holds two singletons started from `extension.py`:
   - `StageStructureCache` — subscribes to USD stage events, caches a name+path tree of the stage, optionally exports it to `stage_topology.json`.
   - `FishSwimController` — finds `Fish_*` prims and the `Water` cylinder, runs per-frame boid-style steering (cohesion / alignment / separation / wander / boundary + optional realism depth-band & banking), writes transforms to the **session layer** (`stage.SetEditTarget(stage.GetSessionLayer())`) so motion is not persisted to disk.

3. **`fish_dynamics.py`** — pure-math helpers (`wrap_to_pi`, `yaw_from_direction`, `intrinsic_speed_factor`, `depth_attraction_strength`, `compute_target_roll`, `sample_fish_traits`). Zero Omniverse imports. This is the boundary that keeps things unit-testable.

4. **`global_variable.py`** — flat module of tuning constants (boid weights, speed ranges, depth-band ratios, RNG seed, etc.). Loaded by `main.py` via `get_global_config()`, which **re-reads the file on every call** with `importlib.util.spec_from_file_location` and `sys.dont_write_bytecode = True`. This is intentional: editing `global_variable.py` while Kit is running picks up new values without a restart. Do not "optimize" this by caching the module or importing it normally.

### Prim resolution

`FishSwimController` resolves the Water cylinder and `Fish_*` prims in three tiers, in order:

1. Configured `WATER_PRIM_PATH` in `global_variable.py`.
2. `stage_topology.json` cached on disk (when `FISH_USE_STAGE_TOPOLOGY_JSON = True`), preferring paths containing `MetalTank`.
3. Stage traversal fallback by name.

If the Water prim is not found yet (assets still loading), init is retried every `FISH_INIT_RETRY_SECONDS`. Stage open/close events reset the controller. When debugging "fish not moving," check carb log lines prefixed `[Aquacast]` — every important state transition logs there.

### Coordinate convention

Fish are authored with **−X forward**. `yaw_from_direction` uses `atan2(-dy, -dx)` and `_local_direction_to_rotate_xyz` mirrors this. If you change forward axis, both must change together, plus the `RotateXYZ` op order in `_set_fish_transform`.

## Editing conventions specific to this repo

- The `stage_topology.json` at the extension root is a **generated artifact** (produced when `EXPORT_STAGE_TOPOLOGY_JSON = True`) but it is committed and read by the runtime as a cache. Don't hand-edit it; regenerate by toggling the flag, launching, then toggling back.
- `premake5.lua` `prebuild_link`s `data/`, `layouts/`, and `aquacast/` into the build target — `main.py`, `fish_dynamics.py`, `global_variable.py`, `stage_topology.json`, and `tests/` at the extension root are deliberately **outside** that link list. They are picked up at runtime via the `importlib` loader described above. If you add a new top-level Python file that the runtime needs, either (a) put it next to `extension.py` inside `aquacast/aquacast_composer/` and import it normally, or (b) load it with the same `importlib.util.spec_from_file_location` pattern from `main.py`. Don't add it to `prebuild_link` and expect imports to work.
- `.etli` files in the repo root are NvStreamer capture logs, gitignored — leave them alone.

## Planning docs

`docs/superpowers/specs/` and `docs/superpowers/plans/` hold design + execution docs for in-progress features (e.g. `2026-05-20-fish-motion-realism-design.md`). Check these before making non-trivial changes to fish motion — they capture intent and tradeoffs that aren't visible from the code.
