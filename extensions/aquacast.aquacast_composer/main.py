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

import fish_dynamics  # noqa: E402

import carb  # noqa: E402
import omni.kit.app  # noqa: E402
import omni.usd  # noqa: E402
from pxr import Gf, Usd, UsdGeom  # noqa: E402

_stage_structure_cache = None
_fish_swim_controller = None


def should_print_stage_topology():
    return bool(get_global_config("PRINT_STAGE_TOPOLOGY", False))


def should_export_stage_topology_json():
    return bool(get_global_config("EXPORT_STAGE_TOPOLOGY_JSON", False))


def get_stage_topology_json_path():
    default_path = Path(__file__).with_name("stage_topology.json")
    return Path(get_global_config("STAGE_TOPOLOGY_JSON_PATH", str(default_path)))


def get_global_config(name, default=None):
    config_path = Path(__file__).with_name("global_variable.py")
    spec = importlib.util.spec_from_file_location("aquacast_global_variable", config_path)
    if spec is None or spec.loader is None:
        return default

    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode

    return getattr(module, name, default)


def start_stage_structure_cache():
    global _stage_structure_cache
    if _stage_structure_cache is None:
        _stage_structure_cache = StageStructureCache()
        _stage_structure_cache.start()
    return _stage_structure_cache


def start_fish_swim_controller():
    global _fish_swim_controller
    if _fish_swim_controller is None:
        _fish_swim_controller = FishSwimController()
        _fish_swim_controller.start()
    return _fish_swim_controller


def stop_stage_structure_cache():
    global _stage_structure_cache
    if _stage_structure_cache is not None:
        _stage_structure_cache.stop()
        _stage_structure_cache = None


def stop_fish_swim_controller():
    global _fish_swim_controller
    if _fish_swim_controller is not None:
        _fish_swim_controller.stop()
        _fish_swim_controller = None


def get_stage_structure():
    if _stage_structure_cache is None:
        return {}
    return _stage_structure_cache.get_snapshot()


def _get_topology_snapshot():
    if _stage_structure_cache is not None:
        snapshot = _stage_structure_cache.get_snapshot()
        if snapshot.get("tree"):
            return snapshot

    topology_path = get_stage_topology_json_path()
    if not topology_path.exists():
        return {}

    try:
        with topology_path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except Exception as exc:
        carb.log_warn(f"[Aquacast] Failed to read stage topology JSON: {topology_path} ({exc})")
        return {}


def _iter_topology_nodes(nodes):
    for node in nodes or []:
        yield node
        yield from _iter_topology_nodes(node.get("children", []))


def _get_topology_paths_by_name(name):
    snapshot = _get_topology_snapshot()
    return [
        node.get("path", "")
        for node in _iter_topology_nodes(snapshot.get("tree", []))
        if node.get("name") == name and node.get("path")
    ]


def _as_vec3(value, default=(0.0, 0.0, 0.0)):
    if value is None:
        return Gf.Vec3d(*default)
    return Gf.Vec3d(float(value[0]), float(value[1]), float(value[2]))


def _length(vec):
    return math.sqrt(vec[0] * vec[0] + vec[1] * vec[1] + vec[2] * vec[2])


def _normalized(vec, fallback=Gf.Vec3d(-1.0, 0.0, 0.0)):
    length = _length(vec)
    if length <= 1e-6:
        return Gf.Vec3d(fallback)
    return Gf.Vec3d(vec[0] / length, vec[1] / length, vec[2] / length)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _smoothstep(edge0, edge1, value):
    if abs(edge1 - edge0) <= 1e-9:
        return 1.0 if value >= edge1 else 0.0
    x = _clamp((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _lerp_vec3(start, end, t):
    t = _clamp(t, 0.0, 1.0)
    return Gf.Vec3d(
        start[0] + (end[0] - start[0]) * t,
        start[1] + (end[1] - start[1]) * t,
        start[2] + (end[2] - start[2]) * t,
    )


def _lerp_direction(current, target, t):
    current = _normalized(current)
    target = _normalized(target, current)
    return _normalized(_lerp_vec3(current, target, t), current)


def _lerp_alpha(rate, dt):
    return _clamp(1.0 - math.exp(-max(0.0, rate) * max(0.0, dt)), 0.0, 1.0)


def _rotate_toward(current, target, max_angle):
    current = _normalized(current)
    target = _normalized(target, current)
    dot = _clamp(current[0] * target[0] + current[1] * target[1] + current[2] * target[2], -1.0, 1.0)
    angle = math.acos(dot)
    if angle <= 1e-6:
        return target
    t = min(1.0, max_angle / angle)
    sin_angle = math.sin(angle)
    if abs(sin_angle) <= 1e-6:
        return target
    a = math.sin((1.0 - t) * angle) / sin_angle
    b = math.sin(t * angle) / sin_angle
    return _normalized(Gf.Vec3d(
        current[0] * a + target[0] * b,
        current[1] * a + target[1] * b,
        current[2] * a + target[2] * b,
    ), current)


def _xform_translation(prim):
    matrix = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    return matrix.ExtractTranslation()


def _find_xform_op(xformable, op_type):
    for op in xformable.GetOrderedXformOps():
        if op.GetOpType() == op_type:
            return op
    return None


def _local_direction_to_rotate_xyz(direction):
    direction = _normalized(direction)
    horizontal = math.sqrt(direction[0] * direction[0] + direction[1] * direction[1])
    yaw = math.degrees(math.atan2(-direction[1], -direction[0]))
    pitch = math.degrees(math.atan2(direction[2], max(horizontal, 1e-6)))
    return Gf.Vec3f(0.0, float(pitch), float(yaw))


def _compute_orientation(direction, fish, prev_direction, dt):
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
    bank_lerp = _lerp_alpha(float(get_global_config("FISH_BANK_LERP_RATE", 3.0)), dt)
    current_roll = float(fish.get("roll", 0.0))
    new_roll = current_roll + (target_roll - current_roll) * bank_lerp
    fish["roll"] = new_roll

    return Gf.Vec3f(float(math.degrees(new_roll)), float(pitch), float(yaw))


def _set_compatible_fish_xform_order(xformable, translate_op, rotate_op):
    ordered_ops = xformable.GetOrderedXformOps()
    scale_ops = []
    other_ops = []

    for op in ordered_ops:
        if op in (translate_op, rotate_op):
            continue
        if op.GetOpType() == UsdGeom.XformOp.TypeOrient:
            continue
        if op.GetOpType() == UsdGeom.XformOp.TypeScale:
            scale_ops.append(op)
        else:
            other_ops.append(op)

    desired_order = [translate_op, rotate_op] + other_ops + scale_ops
    if list(ordered_ops) != desired_order:
        xformable.SetXformOpOrder(desired_order)


def _get_animation_target_prim(fish_root_prim):
    meshes_prim = fish_root_prim.GetChild("Meshes")
    if meshes_prim and meshes_prim.IsValid():
        return meshes_prim
    return fish_root_prim


def _world_to_parent_local_position(prim, world_position):
    parent = prim.GetParent()
    if not parent or not parent.IsValid():
        return world_position
    parent_world = UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    return parent_world.GetInverse().Transform(world_position)


def _world_to_parent_local_direction(prim, world_direction):
    parent = prim.GetParent()
    if not parent or not parent.IsValid():
        return world_direction
    parent_world = UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    inverse = parent_world.GetInverse()
    origin = inverse.Transform(Gf.Vec3d(0.0, 0.0, 0.0))
    endpoint = inverse.Transform(world_direction)
    return _normalized(endpoint - origin, world_direction)


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


class FishSwimController:
    """Animate numbered Fish prims inside the Water cylinder."""

    def __init__(self):
        self._stage_event_sub = None
        self._update_sub = None
        self._last_update_time = None
        self._initialized = False
        self._fish = []
        self._water_center = Gf.Vec3d(0.0, 0.0, 0.0)
        self._water_radius = 1.0
        self._water_min_z = 0.0
        self._water_max_z = 1.0
        self._warned_missing_water = False
        self._next_init_retry_time = 0.0

    def start(self):
        usd_context = omni.usd.get_context()
        self._stage_event_sub = usd_context.get_stage_event_stream().create_subscription_to_pop(
            self._on_stage_event,
            name="aquacast_fish_swim_stage",
        )
        self._update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update,
            name="aquacast_fish_swim_update",
        )
        asyncio.ensure_future(self.initialize_after_frames(3))

    def stop(self):
        self._stage_event_sub = None
        self._update_sub = None
        self._fish = []
        self._initialized = False

    async def initialize_after_frames(self, frames=1):
        app = omni.kit.app.get_app()
        for _ in range(frames):
            await app.next_update_async()
        self.initialize()

    def initialize(self):
        if not bool(get_global_config("ENABLE_FISH_SWIMMING", False)):
            self._initialized = False
            self._fish = []
            return

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            self._initialized = False
            self._schedule_init_retry()
            return

        water_prim = self._find_water_prim(stage)
        if not water_prim or not water_prim.IsValid():
            self._warn_missing_water_once(stage)
            self._initialized = False
            self._schedule_init_retry()
            return

        self._warned_missing_water = False
        self._read_water_bounds(water_prim)
        fish_prims = self._find_fish_prims(stage)
        self._fish = [self._make_fish_state(prim, index) for index, prim in enumerate(fish_prims)]
        self._initialized = bool(self._fish)
        self._last_update_time = time.monotonic()
        carb.log_info(
            f"[Aquacast] Fish swimming initialized: fish_count={len(self._fish)}, "
            f"water_radius={self._water_radius:.3f}"
        )

    def _find_water_prim(self, stage):
        configured_path = str(get_global_config("WATER_PRIM_PATH", "") or "")
        if configured_path:
            prim = stage.GetPrimAtPath(configured_path)
            if prim and prim.IsValid():
                return prim

        if bool(get_global_config("FISH_USE_STAGE_TOPOLOGY_JSON", True)):
            topology_paths = _get_topology_paths_by_name("Water")
            topology_paths = sorted(
                topology_paths,
                key=lambda path: (0 if "MetalTank" in path else 1, path),
            )
            for path in topology_paths:
                prim = stage.GetPrimAtPath(path)
                if prim and prim.IsValid():
                    carb.log_info(f"[Aquacast] Water prim resolved from stage topology: {path}")
                    return prim

        named_water = []
        for prim in stage.Traverse():
            if prim and prim.IsValid() and prim.GetName() == "Water":
                named_water.append(prim)

        for prim in named_water:
            if "MetalTank" in prim.GetPath().pathString:
                carb.log_warn(
                    f"[Aquacast] WATER_PRIM_PATH not found; using fallback Water prim: {prim.GetPath()}"
                )
                return prim

        if named_water:
            prim = sorted(named_water, key=lambda candidate: candidate.GetPath().pathString)[0]
            carb.log_warn(
                f"[Aquacast] WATER_PRIM_PATH not found; using first Water prim: {prim.GetPath()}"
            )
            return prim

        return None

    def _warn_missing_water_once(self, stage):
        if self._warned_missing_water:
            return
        root_layer = stage.GetRootLayer()
        root_identifier = root_layer.identifier if root_layer else ""
        configured_path = str(get_global_config("WATER_PRIM_PATH", "") or "")
        carb.log_warn(
            "[Aquacast] Fish swimming waiting: Water prim not found "
            f"(configured_path={configured_path}, root_layer={root_identifier})"
        )
        self._warned_missing_water = True

    def _schedule_init_retry(self):
        retry_seconds = float(get_global_config("FISH_INIT_RETRY_SECONDS", 1.0))
        self._next_init_retry_time = time.monotonic() + max(0.1, retry_seconds)

    def _read_water_bounds(self, water_prim):
        bbox_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(),
            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
            useExtentsHint=True,
        )
        aligned = bbox_cache.ComputeWorldBound(water_prim).ComputeAlignedBox()
        min_v = aligned.GetMin()
        max_v = aligned.GetMax()
        self._water_center = Gf.Vec3d(
            (min_v[0] + max_v[0]) * 0.5,
            (min_v[1] + max_v[1]) * 0.5,
            (min_v[2] + max_v[2]) * 0.5,
        )
        self._water_radius = max(0.001, min(max_v[0] - min_v[0], max_v[1] - min_v[1]) * 0.5)
        vertical_margin = (max_v[2] - min_v[2]) * 0.08
        self._water_min_z = min_v[2] + vertical_margin
        self._water_max_z = max_v[2] - vertical_margin

    def _find_fish_prims(self, stage):
        prefix = str(get_global_config("FISH_NAME_PREFIX", "Fish_"))
        pattern = re.compile(rf"^{re.escape(prefix)}\d+$")

        if bool(get_global_config("FISH_USE_STAGE_TOPOLOGY_JSON", True)):
            topology_fish = []
            snapshot = _get_topology_snapshot()
            for node in _iter_topology_nodes(snapshot.get("tree", [])):
                name = str(node.get("name", ""))
                path = str(node.get("path", ""))
                if not pattern.match(name) or not path:
                    continue
                prim = stage.GetPrimAtPath(path)
                if prim and prim.IsValid():
                    topology_fish.append(prim)
            if topology_fish:
                return sorted(topology_fish, key=lambda prim: prim.GetName())

        fish = []
        for prim in stage.Traverse():
            if prim.GetPath().GetParentPath().pathString == "/" and pattern.match(prim.GetName()):
                fish.append(prim)
        return sorted(fish, key=lambda prim: prim.GetName())

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
            "prev_direction": initial_direction,
            "roll": 0.0,
        }

        if bool(get_global_config("ENABLE_REALISM_DYNAMICS", True)):
            traits = fish_dynamics.sample_fish_traits(
                prim_name=prim.GetName(),
                base_seed=int(get_global_config("FISH_RNG_BASE_SEED", 1)),
                ranges=self._get_trait_ranges(),
            )
            state.update(traits)

            water_height = max(1e-6, self._water_max_z - self._water_min_z)
            state["preferred_z"] = self._water_min_z + water_height * state["depth_band_center_norm"]
            state["band_half"] = water_height * state["depth_band_half_width_norm"]

        return state

    def _estimate_head_length(self, prim):
        try:
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
                useExtentsHint=True,
            )
            aligned = bbox_cache.ComputeWorldBound(prim).ComputeAlignedBox()
            min_v = aligned.GetMin()
            max_v = aligned.GetMax()
            longest = max(max_v[0] - min_v[0], max_v[1] - min_v[1], max_v[2] - min_v[2])
            return max(self._water_radius * 0.03, longest * 0.5)
        except Exception:
            return self._water_radius * 0.08

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
        base_speed = self._water_radius * float(get_global_config("FISH_SWIM_SPEED_RADIUS_PER_SECOND", 0.12))
        min_speed_fraction = float(get_global_config("FISH_MIN_SPEED_FRACTION", 0.4))
        direction_lerp_rate = float(get_global_config("FISH_DIRECTION_LERP_RATE", 4.0))
        direction_lerp_t = _lerp_alpha(direction_lerp_rate, dt)
        max_turn = float(get_global_config("FISH_MAX_TURN_RADIANS_PER_SECOND", 1.8)) * dt

        for fish in self._fish:
            desired = self._desired_direction(fish, now, realism_on)
            fish["target_direction"] = _lerp_direction(fish["target_direction"], desired, direction_lerp_t)
            fish["prev_direction"] = fish["direction"]
            fish["direction"] = _rotate_toward(fish["direction"], fish["target_direction"], max_turn)

            if realism_on and "speed_noise_amplitude" in fish:
                speed_factor = fish_dynamics.intrinsic_speed_factor(
                    now=now,
                    amplitude=fish["speed_noise_amplitude"],
                    freq_hz=fish["speed_noise_freq_hz"],
                    phase=fish["speed_noise_phase"],
                    min_fraction=min_speed_fraction,
                )
                speed = base_speed * fish["cruise_speed_scale"] * speed_factor
            else:
                speed = base_speed

            next_position = fish["position"] + fish["direction"] * speed * dt
            fish["position"] = self._clamp_position(next_position, fish["direction"], fish["head_length"])
            _set_fish_transform(
                fish["prim"],
                fish["position"],
                fish["direction"],
                fish=fish,
                dt=dt,
                realism_on=realism_on,
            )

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
            depth = Gf.Vec3d(0.0, 0.0, strength) * float(get_global_config("FISH_DEPTH_BAND_WEIGHT", 0.45))

        return _normalized(direction + flock + wander + boundary + depth, direction)

    def _wander_vector(self, fish, now, realism_on=True):
        phase = fish["phase"]
        horizontal = Gf.Vec3d(math.cos(now * 0.7 + phase), math.sin(now * 0.9 + phase * 1.7), 0.0)
        if realism_on and "vertical_wander_freq_hz" in fish:
            vertical_z = math.sin(
                2.0 * math.pi * fish["vertical_wander_freq_hz"] * now + fish["vertical_wander_phase"]
            )
        else:
            vertical_z = math.sin(now * 0.55 + phase)
        vertical = Gf.Vec3d(0.0, 0.0, vertical_z)
        return _normalized(horizontal + vertical * float(get_global_config("FISH_VERTICAL_WANDER_WEIGHT", 0.12)))

    def _boundary_steering(self, fish):
        position = fish["position"]
        direction = fish["direction"]
        head = position + direction * fish["head_length"]
        rel = Gf.Vec3d(head[0] - self._water_center[0], head[1] - self._water_center[1], 0.0)
        radial = _length(rel)
        inward = _normalized(Gf.Vec3d(-rel[0], -rel[1], 0.0), direction)

        safe_radius = self._safe_radius(fish["head_length"])
        start_radius = safe_radius * float(get_global_config("FISH_BOUNDARY_START_RATIO", 0.68))
        wall_t = _smoothstep(start_radius, safe_radius, radial)

        tangent_sign = 1.0 if math.sin(fish["phase"]) >= 0.0 else -1.0
        tangent = Gf.Vec3d(-inward[1] * tangent_sign, inward[0] * tangent_sign, 0.0)
        smooth_turn = 0.5 - 0.5 * math.cos(math.pi * wall_t)
        steer = inward * smooth_turn + tangent * (1.0 - smooth_turn) * wall_t * 0.45

        z_mid = (self._water_min_z + self._water_max_z) * 0.5
        if head[2] > self._water_max_z:
            steer += Gf.Vec3d(0.0, 0.0, -1.0) * _smoothstep(z_mid, self._water_max_z, head[2])
        elif head[2] < self._water_min_z:
            steer += Gf.Vec3d(0.0, 0.0, 1.0) * _smoothstep(self._water_min_z, z_mid, head[2])

        return steer

    def _clamp_position(self, position, direction, head_length=0.0):
        safe_radius = self._safe_radius(head_length)
        head = position + direction * head_length
        rel = Gf.Vec3d(head[0] - self._water_center[0], head[1] - self._water_center[1], 0.0)
        radial = _length(rel)
        if radial > safe_radius:
            rel = _normalized(rel) * safe_radius
            head = Gf.Vec3d(self._water_center[0] + rel[0], self._water_center[1] + rel[1], head[2])
            position = head - direction * head_length

        return Gf.Vec3d(
            position[0],
            position[1],
            _clamp(position[2], self._water_min_z, self._water_max_z),
        )

    def _safe_radius(self, head_length):
        margin_ratio = float(get_global_config("FISH_BOUNDARY_MARGIN_RATIO", 0.12))
        return max(self._water_radius * 0.2, self._water_radius * (1.0 - margin_ratio) - head_length)

    def _on_stage_event(self, event):
        event_type = event.type
        if event_type in (
            int(omni.usd.StageEventType.OPENED),
            int(omni.usd.StageEventType.ASSETS_LOADED),
        ):
            self._initialized = False
            self._next_init_retry_time = 0.0
            self._warned_missing_water = False
            asyncio.ensure_future(self.initialize_after_frames(3))
        elif event_type == int(omni.usd.StageEventType.CLOSED):
            self._initialized = False
            self._fish = []
            self._next_init_retry_time = 0.0
            self._warned_missing_water = False


class StageStructureCache:
    """Keep a lightweight name-only snapshot of the current USD stage."""

    def __init__(self):
        self.stage_name = ""
        self.root_layer = ""
        self.default_prim = ""
        self.tree = []
        self._stage_event_sub = None

    def start(self):
        usd_context = omni.usd.get_context()
        self._stage_event_sub = usd_context.get_stage_event_stream().create_subscription_to_pop(
            self._on_stage_event,
            name="aquacast_stage_structure_cache",
        )
        self.refresh()
        asyncio.ensure_future(self.refresh_after_frames(2))

    def stop(self):
        self._stage_event_sub = None
        self.clear()

    def clear(self):
        self.stage_name = ""
        self.root_layer = ""
        self.default_prim = ""
        self.tree = []

    async def refresh_after_frames(self, frames=1):
        app = omni.kit.app.get_app()
        for _ in range(frames):
            await app.next_update_async()
        self.refresh()

    def refresh(self):
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            self.clear()
            carb.log_info("[Aquacast] No USD stage is open")
            return

        root_layer = stage.GetRootLayer()
        self.root_layer = root_layer.identifier if root_layer else ""
        self.stage_name = Path(self.root_layer).stem if self.root_layer else "Untitled"

        default_prim = stage.GetDefaultPrim()
        self.default_prim = default_prim.GetName() if default_prim and default_prim.IsValid() else ""
        self.tree = self._build_name_tree(stage)

        carb.log_info(
            f"[Aquacast] Stage cached: name={self.stage_name}, "
            f"root_count={len(self.tree)}"
        )
        if should_print_stage_topology():
            self.print_topology()
        if should_export_stage_topology_json():
            self.export_topology_json()

    def get_snapshot(self):
        return {
            "stage_name": self.stage_name,
            "root_layer": self.root_layer,
            "default_prim": self.default_prim,
            "tree": self.tree,
        }

    def _build_name_tree(self, stage):
        nodes_by_path = {}
        roots = []

        for prim in Usd.PrimRange.Stage(stage):
            if not prim or not prim.IsValid() or prim.GetPath().pathString == "/":
                continue

            path = prim.GetPath().pathString
            node = {
                "name": prim.GetName(),
                "path": path,
                "children": [],
            }
            nodes_by_path[path] = node

            parent_path = prim.GetPath().GetParentPath().pathString
            parent = nodes_by_path.get(parent_path)
            if parent:
                parent["children"].append(node)
            else:
                roots.append(node)

        return roots

    def print_topology(self):
        carb.log_info(f"[Aquacast] Stage topology: {self.stage_name}")
        if self.default_prim:
            carb.log_info(f"[Aquacast] Default prim: {self.default_prim}")

        if not self.tree:
            carb.log_info("[Aquacast] Stage topology is empty")
            return

        for line in self._format_tree_lines(self.tree):
            carb.log_info(f"[Aquacast] {line}")

    def _format_tree_lines(self, nodes, depth=0):
        lines = []
        prefix = "  " * depth
        for node in nodes:
            lines.append(f"{prefix}- {node['name']}")
            lines.extend(self._format_tree_lines(node["children"], depth + 1))
        return lines

    def export_topology_json(self):
        output_path = get_stage_topology_json_path()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.get_snapshot()

        with output_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")

        carb.log_info(f"[Aquacast] Stage topology JSON exported: {output_path}")

    def _on_stage_event(self, event):
        event_type = event.type
        if event_type in (
            int(omni.usd.StageEventType.OPENED),
            int(omni.usd.StageEventType.ASSETS_LOADED),
        ):
            asyncio.ensure_future(self.refresh_after_frames(2))
        elif event_type == int(omni.usd.StageEventType.CLOSED):
            self.clear()
            carb.log_info("[Aquacast] Stage cache cleared.")


if __name__ == "__main__":
    print("This module is loaded by the Aquacast Kit extension.")
