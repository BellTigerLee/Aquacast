# Fish Neighbor Loop Vectorization — Design

**Date:** 2026-05-31
**Component:** `extensions/aquacast.aquacast_composer_extensions` (`fish_dynamics.py` + `main.py` `FishSwimController`)
**Status:** Approved scope; pending implementation plan

## 1. Background

`FishSwimController._desired_direction` (main.py:1117–1146) computes the cohesion / alignment / separation steering for every fish on every frame. For each of the N fish, it iterates over every other fish in `self._fish` and runs a distance check followed by accumulator updates if the pair is within `separation_radius` (`water_radius * FISH_SEPARATION_RADIUS_RATIO`, ≈18% of tank radius).

Total cost per frame: `N × (N-1)` pair evaluations — pure-Python double loop with `Gf.Vec3d` arithmetic. Today's stage has 2 fish, so this is invisible. Once the dynamic-fish-spawn feature (`2026-05-31-dynamic-fish-spawn-design.md`) and the upcoming multi-tank refactor land, the scene will host up to ~30 fish per tank × 6–7 tanks (~210 total). At 60 Hz with ~44,000 pair evaluations per frame in pure Python, this becomes the dominant cost in the swim controller.

The 2026-05-20 fish motion realism spec explicitly listed "Refactor of the O(n²) neighbor loop" as a non-goal. This design picks that work up.

## 2. Goals

- Reduce per-frame neighbor-loop wall time by an order of magnitude or more at N≈30 and N≈210, with no behavior change visible to the eye.
- Keep the boids math (cohesion, alignment, separation, weights) numerically equivalent to the current implementation, modulo float-accumulation order at 1e-10 atol.
- Keep changes to `FishSwimController` minimal — touch one method's internals, not its architecture.
- Add the new pure-math helper to `fish_dynamics.py` so it is pytest-covered alongside the existing helpers, following the project's `fish_dynamics.py` ↔ `main.py` split (per CLAUDE.md).
- Determinism preserved: same seed + same inputs ⇒ same outputs, restart to restart.

## 3. Non-Goals

- Spatial partitioning (uniform hash grid, KD-tree). Justified for N≫500; overkill at user's target N≈210, and adds rebuild cost per frame.
- Per-tank partitioning of the neighbor loop. Belongs with the multi-tank refactor, which also needs per-tank water bounds and boundary steering. Including it here would couple two unrelated changes.
- Promoting fish state to numpy-resident structures (e.g., a struct-of-arrays `FishPool`). That is a larger refactor and would change how every method reads fish fields. Pack/unpack per frame is good enough at this scale.
- Changes to `_wander_vector`, `_boundary_steering`, `_clamp_position`, `_set_fish_transform`, or per-fish realism dynamics (depth band, banking, speed noise).
- GPU / OmniGraph offload. Out of scope at this N.

## 4. Approach Overview

Replace the inner `for other in self._fish` loop in `_desired_direction` with a single per-frame call to a new pure-numpy helper `compute_flock_vectors()` defined in `fish_dynamics.py`. The helper computes the pairwise distance matrix, the in-range mask, and the three accumulators (separation, alignment, cohesion-center) for every fish at once via numpy broadcasting.

`_on_update` packs current positions and directions into `(N, 3)` numpy arrays once at the start of each frame, calls `compute_flock_vectors`, caches the four output arrays, and passes them through to `_desired_direction`. Each per-fish call then reads only its own row from the cache and runs the existing `_normalized(...) + weight` math exactly as today.

Architectural shape is unchanged: still one `FishSwimController`, still per-fish post-processing for wander/boundary/depth/lerp/USD-write, still session-layer transform writes. Only the neighbor-pair computation moves from per-pair Python to per-frame numpy.

## 5. Pure Helper — `fish_dynamics.py`

One new function, no Omniverse imports, fully pytest-able.

```python
def compute_flock_vectors(
    positions: np.ndarray,          # shape (N, 3), float64
    directions: np.ndarray,         # shape (N, 3), float64, assumed pre-normalized
    separation_radius: float,       # > 0
    *,
    eps: float = 1e-6,              # zero-distance / self-pair cutoff
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """All-pair boids accumulators in one numpy pass.

    Returns:
        separation:        (N, 3) — Σ_j normalize(p_i − p_j) · (1 − d_ij / r_sep)
        alignment:         (N, 3) — Σ_j dir_j                 (raw sum, caller divides by count)
        cohesion_center:   (N, 3) — Σ_j pos_j                 (raw sum, caller divides by count)
        neighbor_counts:   (N,)   — number of j with eps < d_ij ≤ r_sep

    The caller computes per-fish:
        cohesion_dir  = normalize(cohesion_center[i] / count − position[i])
        alignment_dir = normalize(alignment[i] / count)
        separation_dir = normalize(separation[i])
    matching the existing _desired_direction expression exactly.
    """
```

**Why raw sums instead of pre-normalized directions?** The current `_desired_direction` uses `_normalized(vec, fallback=direction)`, where the per-fish `direction` is the fallback for zero-length vectors. That fallback differs per fish and is awkward to express in pure numpy. Returning raw sums keeps the per-fish normalization (with its per-fish fallback) on the caller side, preserving line-for-line equivalence with the old code.

### 5.1 Algorithm

```
N = positions.shape[0]
if N < 2: return zeros (early exit)

diff = positions[:, None, :] − positions[None, :, :]              # (N, N, 3)
dist = sqrt(sum(diff² , axis=−1))                                 # (N, N)
mask = (dist > eps) & (dist ≤ separation_radius)                  # (N, N) bool

neighbor_counts = mask.sum(axis=1)                                # (N,)

safe_dist = where(mask, dist, 1.0)                                # avoid div-by-zero
unit_offset = diff / safe_dist[..., None]                         # (N, N, 3)
weight = (1 − dist / separation_radius) * mask                    # (N, N)
separation = (unit_offset * weight[..., None]).sum(axis=1)        # (N, 3)

alignment = (directions[None, :, :] * mask[..., None]).sum(axis=1)        # (N, 3)
cohesion_center = (positions[None, :, :] * mask[..., None]).sum(axis=1)   # (N, 3)
```

Self-pairs (i==j) have `dist=0` → `mask=False`, so they are excluded automatically without an explicit identity-matrix subtraction. Coincident-position pairs are excluded by the same `eps` cutoff that the current code uses (`distance <= 1e-6` in main.py:1132).

### 5.2 Memory

`diff` is the largest temporary: `(N, N, 3) × 8B`. At N=210 that's 1.06 MB. At N=30 it's 22 KB. Both negligible. Hard ceiling before this approach needs revisiting: N ≈ 2000 (~96 MB). User's target is an order of magnitude below that.

## 6. Caller — `main.py`

### 6.1 `_on_update` — per-frame numpy pack + one helper call

Inserted between the existing per-frame constants block and the per-fish loop:

```python
sep_radius = self._water_radius * float(get_global_config("FISH_SEPARATION_RADIUS_RATIO", 0.18))
positions = np.asarray([list(f["position"]) for f in self._fish], dtype=np.float64)
directions = np.asarray([list(f["direction"]) for f in self._fish], dtype=np.float64)
sep_arr, align_arr, coh_arr, n_arr = fish_dynamics.compute_flock_vectors(
    positions, directions, sep_radius
)
flock_cache = {
    "separation": sep_arr,
    "alignment": align_arr,
    "cohesion_center": coh_arr,
    "neighbor_counts": n_arr,
}

for idx, fish in enumerate(self._fish):
    desired = self._desired_direction(fish, now, realism_on, flock_cache, idx)
    # ... existing per-fish updates unchanged ...
```

Pack cost (list-comprehension + `np.asarray`) is tens of microseconds at N=210 — orders of magnitude below the Python-loop cost it replaces.

### 6.2 `_desired_direction` — inner loop deleted, rows read from cache

```python
def _desired_direction(self, fish, now, realism_on, flock_cache, idx):
    position = fish["position"]
    direction = fish["direction"]

    neighbor_count = int(flock_cache["neighbor_counts"][idx])
    flock = Gf.Vec3d(0.0, 0.0, 0.0)
    if neighbor_count:
        coh_center = Gf.Vec3d(*(flock_cache["cohesion_center"][idx] / neighbor_count))
        align_vec  = Gf.Vec3d(*(flock_cache["alignment"][idx] / neighbor_count))
        sep_vec    = Gf.Vec3d(*flock_cache["separation"][idx])

        cohesion   = _normalized(coh_center - position, direction)
        alignment  = _normalized(align_vec,             direction)
        separation = _normalized(sep_vec,               direction)

        flock += cohesion   * float(get_global_config("FISH_COHESION_WEIGHT",   0.18))
        flock += alignment  * float(get_global_config("FISH_ALIGNMENT_WEIGHT",  0.25))
        flock += separation * float(get_global_config("FISH_SEPARATION_WEIGHT", 0.42))

    wander   = self._wander_vector(fish, now, realism_on) * float(get_global_config("FISH_WANDER_WEIGHT", 0.20))
    boundary = self._boundary_steering(fish)              * float(get_global_config("FISH_BOUNDARY_WEIGHT", 1.35))

    depth = Gf.Vec3d(0.0, 0.0, 0.0)
    if realism_on and "preferred_z" in fish:
        strength = fish_dynamics.depth_attraction_strength(
            position_z=fish["position"][2],
            preferred_z=fish["preferred_z"],
            band_half=fish["band_half"],
        )
        depth = Gf.Vec3d(0.0, 0.0, strength) * float(get_global_config("FISH_DEPTH_BAND_WEIGHT", 0.45))

    return _normalized(direction + flock + wander + boundary + depth, direction)
```

Deleted block: main.py:1121–1137 (the entire `for other in self._fish` loop and its `separation_radius` local).

### 6.3 Signature change

`_desired_direction` gains two parameters: `flock_cache: dict`, `idx: int`. Its only call site is `_on_update` (main.py:1089), updated in 6.1. No other code references this method.

## 7. Numerical Equivalence

The vectorized form computes the same scalar expressions as the loop, just in a different evaluation order. Specifically:

- The `separation` accumulator: the loop adds `_normalized(offset) * (1 - dist/sep_radius)` per pair. The vectorized form computes `(offset / dist) * (1 - dist/sep_radius)` per pair (identical, since `_normalized(offset) = offset / |offset|` for non-zero `offset`) and sums.
- The `alignment` accumulator: raw direction sum, identical.
- The `cohesion_center` accumulator: raw position sum, identical.
- `neighbor_count`: count of `True` mask entries on row i.

Float accumulation order differs (numpy's reduction tree vs. Python's left-to-right `+=`). Differences are at ~1e-12 absolute scale per accumulator, well below the lerp/normalize steps that follow. No visible behavior change.

A direct golden test (§9.1) pins this down: at N=10 and N=50 with random inputs, the loop and vectorized outputs agree to `atol=1e-10`.

## 8. Edge Cases

| Input | Behavior |
|---|---|
| N = 0 | Helper returns four empty arrays; outer loop runs zero times. |
| N = 1 | Helper returns one row of zeros + `neighbor_counts=[0]`; caller's `if neighbor_count` branch skipped → flock=0, only wander/boundary/depth contribute. Same as today. |
| All fish outside each other's `separation_radius` | mask all False; all accumulators zero; `neighbor_counts` all zero. |
| Two fish at exactly the same position | `dist=0` < `eps` → masked out; no contribution. Same as today's `if distance <= 1e-6: continue`. |
| Direction vector accidentally non-normalized | Alignment sum is over raw vectors; magnitude carries through. Same risk as today. Not introduced by this change. |

## 9. Testing

### 9.1 Pure pytest — `tests/test_fish_dynamics.py` additions

All tests are pure-math, no Kit/USD imports. Run with `pytest extensions/aquacast.aquacast_composer_extensions/tests/`.

```
test_compute_flock_vectors_n_zero_returns_empty
test_compute_flock_vectors_n_one_returns_zeros
test_compute_flock_vectors_two_far_apart_no_neighbors
test_compute_flock_vectors_two_within_radius_mutual_pair
test_compute_flock_vectors_excludes_self_diagonal
test_compute_flock_vectors_excludes_overlapping_positions
test_compute_flock_vectors_neighbor_count_matches_naive_loop
test_compute_flock_vectors_separation_weight_drops_to_zero_at_radius_edge
test_compute_flock_vectors_matches_naive_loop_n10_random   # golden equivalence
test_compute_flock_vectors_matches_naive_loop_n50_random   # golden equivalence
```

The two `matches_naive_loop_*` tests are the heart of the safety net. The test file defines a ~15-line pure-Python reference implementation of the current inner loop and asserts the vectorized helper agrees on the same random `positions`/`directions` to `atol=1e-10`. This is the contract that lets us refactor the caller without visual regression.

### 9.2 USD-bound code

`_on_update` and `_desired_direction` integration is not unit-tested — same convention as the rest of `FishSwimController` (per CLAUDE.md split).

### 9.3 Manual smoke check

```bash
AQUACAST_DYNAMIC_FISH_COUNT=30 ./start_aquacast.sh --composer
```

Expected:
- 30 salmon per tank swim with cohesion / alignment / separation visible (clustering, no overlaps, aligned heading within clumps).
- Frame rate steady at 60 fps; no visible hitches.
- Behavior visually indistinguishable from a build run at the same seed prior to this change.

Side-by-side compare option for the brave: keep a git stash of the pre-change branch, alternate launches with the same `FISH_RNG_SEED`, and eyeball the school dynamics.

### 9.4 Optional throwaway timing

Temporary `time.perf_counter()` brackets around the new pack-and-compute block, logging at INFO every ~5 s. Measure once at N=30 and at a synthetic N=210 (spawn-disabled test stage), record numbers in this design doc's appendix, then **remove the timing code** before commit. Production code carries no perf instrumentation.

## 10. Files Touched

| File | Change |
|---|---|
| `extensions/aquacast.aquacast_composer_extensions/fish_dynamics.py` | **+1 function:** `compute_flock_vectors(positions, directions, separation_radius, *, eps=1e-6)` |
| `extensions/aquacast.aquacast_composer_extensions/main.py` | `FishSwimController._on_update`: insert per-frame numpy pack + `compute_flock_vectors` call; pass `flock_cache, idx` to `_desired_direction`. `FishSwimController._desired_direction`: drop inner `for other in self._fish` loop (main.py:1121–1137), read accumulators from `flock_cache[...][idx]` instead. Signature gains `flock_cache, idx`. |
| `extensions/aquacast.aquacast_composer_extensions/tests/test_fish_dynamics.py` | **+~10 tests** covering edge cases and N=10 / N=50 golden equivalence against a reference loop defined in the test file. |

## 11. Forward Compatibility

When the multi-tank refactor lands, the natural next step is to partition `self._fish` by tank and run `compute_flock_vectors` once per tank instead of once globally. The helper signature already accepts arbitrary `positions`/`directions` arrays, so no API change is needed — only the caller groups fish by tank before packing. That refactor also owns per-tank `water_center`/`water_radius`/`_boundary_steering`, which are outside this design's scope.

## 12. Open Questions

None. Approach, target N, behavior preservation, and module placement were all settled during brainstorming.
