import asyncio
import importlib
import importlib.util
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import fish_dynamics  # noqa: E402
import thermal_dynamics  # noqa: E402
import water_quality_backend_client  # noqa: E402
import water_quality_dynamics  # noqa: E402
import water_quality_model  # noqa: E402

water_quality_backend_client = importlib.reload(water_quality_backend_client)
water_quality_dynamics = importlib.reload(water_quality_dynamics)
water_quality_model = importlib.reload(water_quality_model)

import carb  # noqa: E402
import omni.kit.app  # noqa: E402
import omni.usd  # noqa: E402
from pxr import Gf, Sdf, Usd, UsdGeom, Vt  # noqa: E402

_stage_structure_cache = None
_fish_swim_controller = None
_water_temp_controller = None
_water_quality_controller = None


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


def start_water_temp_controller():
    global _water_temp_controller
    if _water_temp_controller is None:
        if not bool(get_global_config("ENABLE_WATER_TEMP_VIS", False)):
            return None
        _water_temp_controller = WaterTempController()
        _water_temp_controller.start()
    return _water_temp_controller


def start_water_quality_controller():
    global _water_quality_controller
    if _water_quality_controller is None:
        enabled = bool(get_global_config("ENABLE_WATER_QUALITY", get_global_config("ENABLE_WATER_QUALITY_SIM", False)))
        if not enabled:
            return None
        _water_quality_controller = WaterQualityController()
        _water_quality_controller.start()
    return _water_quality_controller


def stop_water_temp_controller():
    global _water_temp_controller
    if _water_temp_controller is not None:
        _water_temp_controller.stop()
        _water_temp_controller = None


def stop_water_quality_controller():
    global _water_quality_controller
    if _water_quality_controller is not None:
        _water_quality_controller.stop()
        _water_quality_controller = None


def water_temp_controller_inflow_state():
    if _water_temp_controller is None:
        return False
    return _water_temp_controller.is_inflow_enabled()


def toggle_water_temp_controller_inflow():
    if _water_temp_controller is not None:
        _water_temp_controller.toggle_inflow()


def sample_water_temp_sensor(sensor_path=None, radius=None):
    if _water_temp_controller is None:
        return {"status": "water temperature controller is not running"}
    return _water_temp_controller.sample_temperature_sensor(sensor_path, radius)


def sample_water_quality_sensor(sensor_name=None):
    if _water_quality_controller is None:
        return {"status": "water quality controller is not running"}
    return _water_quality_controller.sample_sensor(sensor_name)


def sample_quality_sensor(sensor_path=None):
    return sample_water_quality_sensor(sensor_path)


def sample_all_water_quality_sensors():
    if _water_quality_controller is None:
        return {"status": "water quality controller is not running", "readings": []}
    return {"status": "ok", "readings": _water_quality_controller.sample_all_sensors()}


def apply_feed(mass_kg):
    if _water_quality_controller is not None:
        _water_quality_controller.apply_feed(mass_kg)


def set_water_exchange(q_lph):
    if _water_quality_controller is not None:
        _water_quality_controller.set_water_exchange(q_lph)


def set_inflow(enabled):
    if _water_quality_controller is not None:
        _water_quality_controller.set_inflow(enabled)
    if _water_temp_controller is not None:
        current = _water_temp_controller.is_inflow_enabled()
        if bool(current) != bool(enabled):
            _water_temp_controller.toggle_inflow()


def set_heater(power):
    if _water_quality_controller is not None:
        _water_quality_controller.set_heater(power)


def set_biofilter(enabled):
    if _water_quality_controller is not None:
        _water_quality_controller.set_biofilter(enabled)


def set_stock(n, w_kg):
    if _water_quality_controller is not None:
        _water_quality_controller.set_stock(n, w_kg)


def load_scenario(name):
    if _water_quality_controller is None:
        return False
    return _water_quality_controller.load_scenario(name)


def get_quality_snapshot():
    if _water_quality_controller is None:
        return {"status": "water quality controller is not running"}
    return _water_quality_controller.snapshot()


def set_quality_view_variable(variable):
    if _water_quality_controller is not None:
        _water_quality_controller.set_view_variable(variable)


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


def _topology_node_has_child(node, child_name):
    return any(child.get("name") == child_name for child in node.get("children", []) or [])


def _get_fish_base_name(prefix):
    configured = get_global_config("FISH_BASE_NAME", None)
    if configured:
        return str(configured)
    if prefix.endswith("_") and len(prefix) > 1:
        return prefix[:-1]
    return prefix


def _topology_node_matches_fish_root(node, pattern, base_name):
    name = str(node.get("name", ""))
    if pattern.match(name):
        return True
    return name == base_name and _topology_node_has_child(node, "Meshes")


def _prim_has_child(prim, child_name):
    try:
        child = prim.GetChild(child_name)
        return bool(child and child.IsValid())
    except Exception:
        return False


def _prim_matches_fish_root(prim, pattern, base_name):
    name = prim.GetName()
    if pattern.match(name):
        return True
    return name == base_name and _prim_has_child(prim, "Meshes")


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
        self._active = False
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
        self._active = True
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
                key=lambda path: (
                    0 if "/Aquarium/" in path and "/Looks/" not in path else 1,
                    0 if "/Looks/" not in path else 1,
                    0 if "/InWater/" in path and path.endswith("/Water") else 1,
                    0 if "MetalTank" in path else 1,
                    0 if path.endswith("/Water") else 1,
                    path,
                ),
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
        base_name = _get_fish_base_name(prefix)
        pattern = re.compile(rf"^{re.escape(prefix)}\d+$")

        if bool(get_global_config("FISH_USE_STAGE_TOPOLOGY_JSON", True)):
            topology_fish = []
            snapshot = _get_topology_snapshot()
            for node in _iter_topology_nodes(snapshot.get("tree", [])):
                path = str(node.get("path", ""))
                if not path or not _topology_node_matches_fish_root(node, pattern, base_name):
                    continue
                prim = stage.GetPrimAtPath(path)
                if prim and prim.IsValid():
                    topology_fish.append(prim)
            if topology_fish:
                return sorted(topology_fish, key=lambda prim: prim.GetPath().pathString)

        fish = []
        for prim in stage.Traverse():
            if (
                prim.GetPath().GetParentPath().pathString == "/"
                and _prim_matches_fish_root(prim, pattern, base_name)
            ):
                fish.append(prim)
        return sorted(fish, key=lambda prim: prim.GetPath().pathString)

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


class WaterTempController:
    """Drive bulk water temperature and runtime temperature particle visualization."""

    def __init__(self):
        self._active = False
        self._stage_event_sub = None
        self._update_sub = None
        self._initialized = False
        self._isosurface_prim = None
        self._display_color_attr = None
        self._T = float(get_global_config("INITIAL_WATER_TEMP_C", 14.0))
        self._inflow_enabled = bool(get_global_config("INFLOW_ENABLED_DEFAULT", True))
        inlet = float(get_global_config("INLET_WATER_TEMP_C", 14.0))
        room = float(get_global_config("ROOM_TEMP_C", 22.0))
        if inlet > room:
            carb.log_warn(
                f"[Aquacast Temp] INLET_WATER_TEMP_C ({inlet}) > ROOM_TEMP_C ({room}); "
                "inflow will heat rather than cool"
            )
        self._last_update_time = None
        self._last_log_time = 0.0
        self._next_init_retry_time = 0.0
        self._warned_missing_isosurface = False
        self._color_stops_cached = None
        self._color_stops_sorted = None
        self._prev_rgb = None
        self._water_prim = None
        self._particles_prim = None
        self._particle_color_attr = None
        self._particle_display_color_attr = None
        self._particle_display_color_attrs = []
        self._particle_temperature_attr = None
        self._particle_heat_weights = []
        self._particle_positions = []
        self._particle_temperatures = []
        self._last_particle_update_time = 0.0
        self._particle_elapsed = 0.0
        self._warned_missing_water = False

    def start(self):
        self._active = True
        usd_context = omni.usd.get_context()
        self._stage_event_sub = usd_context.get_stage_event_stream().create_subscription_to_pop(
            self._on_stage_event,
            name="aquacast_water_temp_stage",
        )
        self._update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update,
            name="aquacast_water_temp_update",
        )
        for frames in (1, 3, 10, 30):
            asyncio.ensure_future(self._initialize_after_frames(frames))

    def stop(self):
        self._active = False
        self._stage_event_sub = None
        self._update_sub = None
        self._isosurface_prim = None
        self._display_color_attr = None
        self._water_prim = None
        self._particles_prim = None
        self._particle_color_attr = None
        self._particle_display_color_attr = None
        self._particle_display_color_attrs = []
        self._particle_temperature_attr = None
        self._particle_heat_weights = []
        self._particle_positions = []
        self._particle_temperatures = []
        self._initialized = False

    async def _initialize_after_frames(self, frames=1):
        app = omni.kit.app.get_app()
        for _ in range(frames):
            await app.next_update_async()
            if not getattr(self, "_active", True):
                return
        self._initialize()

    def _initialize(self):
        if not getattr(self, "_active", True):
            return
        if not bool(get_global_config("ENABLE_WATER_TEMP_VIS", False)):
            self._initialized = False
            self._isosurface_prim = None
            self._display_color_attr = None
            self._water_prim = None
            self._particles_prim = None
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            return

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            self._schedule_init_retry()
            return

        if bool(get_global_config("ENABLE_PARTICLE_SYSTEM_TEMP_COLOR", False)):
            self._bind_isosurface(stage)
        if bool(get_global_config("ENABLE_WATER_TEMP_PARTICLES", True)):
            self._bind_temperature_particles(stage)

        self._initialized = bool(self._display_color_attr or self._particle_color_attr)
        if not self._initialized:
            self._schedule_init_retry()
            return

        targets = []
        if self._isosurface_prim and self._isosurface_prim.IsValid():
            targets.append(f"Isosurface={self._isosurface_prim.GetPath()}")
        if self._particles_prim and self._particles_prim.IsValid():
            targets.append(f"Particles={self._particles_prim.GetPath()}")
        carb.log_info(
            f"[Aquacast Temp] Initialized {'; '.join(targets)}; "
            f"T={self._T:.2f} C, inflow={'ON' if self._inflow_enabled else 'OFF'}"
        )

    def _bind_isosurface(self, stage):
        isosurface_prim = self._find_isosurface_prim(stage)
        if not isosurface_prim or not isosurface_prim.IsValid():
            self._warn_missing_isosurface_once()
            self._isosurface_prim = None
            self._display_color_attr = None
            return

        self._warned_missing_isosurface = False
        self._isosurface_prim = isosurface_prim
        self._display_color_attr = self._bind_display_color_primvar(stage, isosurface_prim)
        if self._display_color_attr is None:
            carb.log_warn(
                "[Aquacast Temp] Failed to bind displayColor primvar on "
                f"{isosurface_prim.GetPath()}; color updates will be skipped"
            )

    def _bind_temperature_particles(self, stage):
        water_prim = self._find_water_prim(stage)
        if not water_prim or not water_prim.IsValid():
            self._warn_missing_water_once()
            self._water_prim = None
            self._particles_prim = None
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            self._particle_positions = []
            self._particle_heat_weights = []
            return

        self._warned_missing_water = False
        self._water_prim = water_prim
        if (
            self._particles_prim
            and self._particles_prim.IsValid()
            and self._particle_color_attr is not None
            and self._particle_heat_weights
        ):
            return
        try:
            self._author_temperature_particles(stage, water_prim)
        except Exception as exc:
            carb.log_warn(f"[Aquacast Temp] Failed to author temperature particles: {exc}")
            self._particles_prim = None
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            self._particle_positions = []
            self._particle_heat_weights = []

    def is_inflow_enabled(self):
        return self._inflow_enabled

    def toggle_inflow(self):
        self._inflow_enabled = not self._inflow_enabled
        carb.log_info(f"[Aquacast Temp] Inflow toggled -> {'ON' if self._inflow_enabled else 'OFF'}")

    def sample_temperature_sensor(self, sensor_path=None, radius=None):
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return {"status": "stage is not open"}

        sensor_prim = self._find_sensor_prim(stage, sensor_path)
        if not sensor_prim or not sensor_prim.IsValid():
            return {"status": "sensor prim not found"}

        positions = self._particle_positions
        temperatures = self._read_particle_temperatures()
        if not positions or not temperatures:
            return {
                "status": "temperature particles are not ready",
                "sensor_path": sensor_prim.GetPath().pathString,
            }

        count = min(len(positions), len(temperatures))
        if count <= 0:
            return {"status": "temperature particles are empty"}

        radius_value = float(radius if radius is not None else get_global_config("TEMP_SENSOR_SAMPLE_RADIUS", 8.0))
        radius_value = max(0.001, radius_value)
        fallback_count = max(1, int(get_global_config("TEMP_SENSOR_FALLBACK_NEAREST_COUNT", 16)))
        sensor_pos = self._prim_world_center(stage, sensor_prim)

        samples = []
        nearest = []
        radius_sq = radius_value * radius_value
        for index in range(count):
            pos = positions[index]
            dx = float(pos[0]) - float(sensor_pos[0])
            dy = float(pos[1]) - float(sensor_pos[1])
            dz = float(pos[2]) - float(sensor_pos[2])
            distance_sq = dx * dx + dy * dy + dz * dz
            temp = float(temperatures[index])
            nearest.append((distance_sq, temp))
            if distance_sq <= radius_sq:
                samples.append((distance_sq, temp))

        used_fallback = False
        if not samples:
            nearest.sort(key=lambda item: item[0])
            samples = nearest[:fallback_count]
            used_fallback = True

        values = [temp for _, temp in samples]
        distances = [math.sqrt(distance_sq) for distance_sq, _ in samples]
        return {
            "status": "ok",
            "sensor_path": sensor_prim.GetPath().pathString,
            "sensor_position": (float(sensor_pos[0]), float(sensor_pos[1]), float(sensor_pos[2])),
            "radius": radius_value,
            "sample_count": len(values),
            "used_fallback": used_fallback,
            "average_c": sum(values) / len(values),
            "min_c": min(values),
            "max_c": max(values),
            "nearest_distance": min(distances) if distances else None,
            "farthest_distance": max(distances) if distances else None,
        }

    def _read_particle_temperatures(self):
        if self._particle_temperatures:
            return self._particle_temperatures
        if self._particle_temperature_attr is None:
            return []
        try:
            values = self._particle_temperature_attr.Get()
        except Exception:
            return []
        return [float(value) for value in values] if values else []

    def _find_sensor_prim(self, stage, sensor_path=None):
        configured = str(sensor_path or get_global_config("TEMP_SENSOR_PRIM_PATH", "") or "").strip()
        if configured:
            prim = stage.GetPrimAtPath(configured)
            if prim and prim.IsValid():
                return prim

        configured_name = str(get_global_config("TEMP_SENSOR_PRIM_NAME", "") or "").strip()
        if not configured_name and configured:
            configured_name = Path(configured).name
        if configured_name:
            for path in _get_topology_paths_by_name(configured_name):
                prim = stage.GetPrimAtPath(path)
                if prim and prim.IsValid():
                    return prim

            for prim in stage.Traverse():
                if prim and prim.IsValid() and prim.GetName() == configured_name:
                    return prim

        for path in _get_topology_paths_by_name("Sensor"):
            prim = stage.GetPrimAtPath(path)
            if prim and prim.IsValid():
                return prim

        for prim in stage.Traverse():
            if prim and prim.IsValid() and prim.GetName() == "Sensor":
                return prim
        return None

    def _prim_world_center(self, stage, prim):
        try:
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
                useExtentsHint=True,
            )
            aligned = bbox_cache.ComputeWorldBound(prim).ComputeAlignedBox()
            minimum = aligned.GetMin()
            maximum = aligned.GetMax()
            center = Gf.Vec3d(
                (minimum[0] + maximum[0]) * 0.5,
                (minimum[1] + maximum[1]) * 0.5,
                (minimum[2] + maximum[2]) * 0.5,
            )
            if all(math.isfinite(float(center[index])) for index in range(3)):
                return center
        except Exception:
            pass

        try:
            xformable = UsdGeom.Xformable(prim)
            matrix = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            return matrix.Transform(Gf.Vec3d(0.0, 0.0, 0.0))
        except Exception:
            return Gf.Vec3d(0.0, 0.0, 0.0)

    def _schedule_init_retry(self):
        retry = float(get_global_config("TEMP_VIS_INIT_RETRY_SECONDS", 1.0))
        self._next_init_retry_time = time.time() + max(0.05, retry)

    def _warn_missing_isosurface_once(self):
        if self._warned_missing_isosurface:
            return
        message = (
            "[Aquacast Temp] Isosurface prim not found; temperature particles may still run "
            f"(configured path={get_global_config('ISOSURFACE_PRIM_PATH', '')!r})"
        )
        if bool(get_global_config("ENABLE_WATER_TEMP_PARTICLES", True)):
            carb.log_info(message)
        else:
            carb.log_warn(message)
        self._warned_missing_isosurface = True

    def _warn_missing_water_once(self):
        if self._warned_missing_water:
            return
        carb.log_warn(
            "[Aquacast Temp] Water prim not found; temperature particles will retry "
            f"(configured path={get_global_config('WATER_PRIM_PATH', '')!r})"
        )
        self._warned_missing_water = True

    def _find_water_prim(self, stage):
        configured = str(get_global_config("WATER_PRIM_PATH", "") or "").strip()
        if configured:
            prim = stage.GetPrimAtPath(configured)
            if prim and prim.IsValid():
                return prim

        topology_paths = _get_topology_paths_by_name("Water")
        topology_paths = sorted(
            topology_paths,
            key=lambda path: (
                0 if "/Looks/" not in path and "/Materials/" not in path else 1,
                0 if "/Group/" in path else 1,
                0 if path.endswith("/Water") else 1,
                path,
            ),
        )
        for path in topology_paths:
            prim = stage.GetPrimAtPath(path)
            if prim and prim.IsValid():
                carb.log_info(f"[Aquacast Temp] Water prim resolved from stage topology: {path}")
                return prim

        for prim in stage.Traverse():
            if prim and prim.IsValid() and prim.GetName() == "Water":
                path = prim.GetPath().pathString
                if "/Looks/" not in path and "/Materials/" not in path:
                    return prim
        return None

    def _temperature_particle_path(self, water_prim):
        configured = str(get_global_config("TEMP_PARTICLE_PRIM_PATH", "") or "").strip()
        if configured:
            return Sdf.Path(configured)
        parent = water_prim.GetPath().GetParentPath()
        if parent == Sdf.Path.absoluteRootPath:
            return Sdf.Path("/TemperatureParticlesInsideWater")
        return parent.AppendChild("TemperatureParticlesInsideWater")

    def _read_water_particle_bounds(self, water_prim):
        bbox_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(),
            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
            useExtentsHint=True,
        )
        aligned = bbox_cache.ComputeWorldBound(water_prim).ComputeAlignedBox()
        min_v = aligned.GetMin()
        max_v = aligned.GetMax()
        center = Gf.Vec3d(
            (min_v[0] + max_v[0]) * 0.5,
            (min_v[1] + max_v[1]) * 0.5,
            (min_v[2] + max_v[2]) * 0.5,
        )

        up_axis_name = str(get_global_config("TEMP_PARTICLE_UP_AXIS", "Y") or "Y").upper()
        up_axis = {"X": 0, "Y": 1, "Z": 2}.get(up_axis_name, 1)
        radial_axes = [index for index in range(3) if index != up_axis]
        size = [max_v[index] - min_v[index] for index in range(3)]
        radius = max(0.001, min(size[radial_axes[0]], size[radial_axes[1]]) * 0.5)
        up_min = min_v[up_axis]
        up_max = max_v[up_axis]
        return center, radius, up_min, up_max, up_axis, radial_axes

    def _author_temperature_particles(self, stage, water_prim):
        import random

        center, radius, up_min, up_max, up_axis, radial_axes = self._read_water_particle_bounds(water_prim)
        count = max(1, int(get_global_config("TEMP_PARTICLE_COUNT", 2000)))
        radius_eff = radius * float(get_global_config("TEMP_PARTICLE_RADIUS_RATIO", 0.94))
        up_margin = (up_max - up_min) * (1.0 - float(get_global_config("TEMP_PARTICLE_HEIGHT_RATIO", 0.94))) * 0.5
        up_low = up_min + max(0.0, up_margin)
        up_high = up_max - max(0.0, up_margin)
        rng = random.Random(int(get_global_config("TEMP_PARTICLE_RANDOM_SEED", 42)))
        heating_mode = str(get_global_config("TEMP_PARTICLE_HEATING_MODE", "side") or "side")

        positions = []
        heat_weights = []
        for _ in range(count):
            radial = radius_eff * math.sqrt(rng.random())
            theta = math.tau * rng.random()
            coords = [float(center[0]), float(center[1]), float(center[2])]
            coords[radial_axes[0]] = float(center[radial_axes[0]] + radial * math.cos(theta))
            coords[radial_axes[1]] = float(center[radial_axes[1]] + radial * math.sin(theta))
            up_value = rng.uniform(up_low, up_high) if up_high > up_low else center[up_axis]
            coords[up_axis] = float(up_value)
            positions.append(Gf.Vec3f(coords[0], coords[1], coords[2]))
            radial_norm = radial / max(radius_eff, 1e-6)
            up_norm = (up_value - up_low) / max(up_high - up_low, 1e-6)
            if heating_mode == "bottom":
                weight = 1.0 - _smoothstep(0.0, 0.18, up_norm)
            elif heating_mode == "internal":
                up_center_norm = (up_value - center[up_axis]) / max(up_high - up_low, 1e-6)
                distance_norm = math.sqrt((radial_norm * radial_norm) + (up_center_norm * up_center_norm))
                weight = 1.0 - _smoothstep(0.0, 0.22, distance_norm)
            else:
                weight = _smoothstep(0.72, 1.0, radial_norm)
            heat_weights.append(max(0.0, min(1.0, weight)))

        width = float(get_global_config("TEMP_PARTICLE_WIDTH", max(0.01, radius * 0.025)))

        particle_path = self._temperature_particle_path(water_prim)
        session_layer = stage.GetSessionLayer()
        edit_target = session_layer if session_layer is not None else stage.GetRootLayer()
        cyan = Gf.Vec3f(*thermal_dynamics.temperature_to_rgb(
            self._T,
            self._sorted_stops(get_global_config("TEMP_COLOR_STOPS", [])),
        ))
        display_attrs = []
        with Usd.EditContext(stage, edit_target):
            if stage.GetPrimAtPath(particle_path).IsValid():
                stage.RemovePrim(particle_path)
            container = UsdGeom.Xform.Define(stage, particle_path)
            container.CreateVisibilityAttr(UsdGeom.Tokens.inherited)
            container.CreatePurposeAttr(UsdGeom.Tokens.default_)
            for index, pos in enumerate(positions):
                sphere_path = particle_path.AppendChild(f"P_{index:04d}")
                sphere = UsdGeom.Sphere.Define(stage, sphere_path)
                sphere.CreateRadiusAttr(width)
                sphere.CreateVisibilityAttr(UsdGeom.Tokens.inherited)
                sphere.CreatePurposeAttr(UsdGeom.Tokens.default_)
                color_attr = sphere.CreateDisplayColorAttr(Vt.Vec3fArray([cyan]))
                sphere.CreateDisplayOpacityAttr(Vt.FloatArray([1.0]))
                xformable = UsdGeom.Xformable(sphere.GetPrim())
                translate_op = xformable.AddTranslateOp(precision=UsdGeom.XformOp.PrecisionDouble)
                translate_op.Set(Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2])))
                display_attrs.append(color_attr)

        self._particles_prim = stage.GetPrimAtPath(particle_path)
        self._particle_color_attr = display_attrs[0] if display_attrs else None
        self._particle_display_color_attr = self._particle_color_attr
        self._particle_display_color_attrs = display_attrs
        self._particle_temperature_attr = None
        self._particle_positions = positions
        self._particle_heat_weights = heat_weights
        self._last_particle_update_time = 0.0
        self._particle_elapsed = 0.0
        self._write_particle_samples(stage, force=True)
        carb.log_info(
            f"[Aquacast Temp] Authored {count} visible temperature spheres at {particle_path} "
            f"inside water={water_prim.GetPath()}"
        )

    def _write_particle_samples(self, stage, force=False):
        if self._particle_color_attr is None or not self._particle_heat_weights:
            return

        now = time.monotonic()
        interval = float(get_global_config("TEMP_PARTICLE_UPDATE_INTERVAL_SECONDS", 0.12))
        if not force and interval > 0.0 and now - self._last_particle_update_time < interval:
            return
        if self._last_particle_update_time:
            self._particle_elapsed += max(0.0, min(now - self._last_particle_update_time, 0.25))
        self._last_particle_update_time = now

        stops = self._sorted_stops(get_global_config("TEMP_COLOR_STOPS", []))
        if not stops:
            return
        sphere_color_attrs = getattr(self, "_particle_display_color_attrs", []) or []
        water_quality_drives_color = bool(get_global_config("ENABLE_WATER_QUALITY", get_global_config("ENABLE_WATER_QUALITY_SIM", False)))
        write_temperature_color = force or not water_quality_drives_color
        heat_delta = float(get_global_config("TEMP_PARTICLE_HEAT_DELTA_C", 42.0))
        spread_rate = float(get_global_config("TEMP_PARTICLE_SPREAD_RATE", 0.05))
        spread = 1.0 - math.exp(-max(0.0, self._particle_elapsed) * max(0.0, spread_rate))
        temperatures = []
        colors = []
        for weight in self._particle_heat_weights:
            temp = self._T + heat_delta * weight * spread
            temperatures.append(float(temp))
            if write_temperature_color:
                colors.append(Gf.Vec3f(*thermal_dynamics.temperature_to_rgb(temp, stops)))
        self._particle_temperatures = temperatures

        try:
            session_layer = stage.GetSessionLayer()
            if session_layer is not None:
                with Usd.EditContext(stage, session_layer):
                    if write_temperature_color:
                        if sphere_color_attrs:
                            for attr, color in zip(sphere_color_attrs, colors):
                                attr.Set(Vt.Vec3fArray([color]))
                        else:
                            self._particle_color_attr.Set(Vt.Vec3fArray(colors))
                            if self._particle_display_color_attr is not None:
                                self._particle_display_color_attr.Set(Vt.Vec3fArray(colors))
                    if self._particle_temperature_attr is not None:
                        self._particle_temperature_attr.Set(Vt.FloatArray(temperatures))
            else:
                if write_temperature_color:
                    if sphere_color_attrs:
                        for attr, color in zip(sphere_color_attrs, colors):
                            attr.Set(Vt.Vec3fArray([color]))
                    else:
                        self._particle_color_attr.Set(Vt.Vec3fArray(colors))
                        if self._particle_display_color_attr is not None:
                            self._particle_display_color_attr.Set(Vt.Vec3fArray(colors))
                if self._particle_temperature_attr is not None:
                    self._particle_temperature_attr.Set(Vt.FloatArray(temperatures))
        except Exception as exc:
            carb.log_warn(f"[Aquacast Temp] Failed to write temperature particle colors: {exc}")

    def _find_isosurface_prim(self, stage):
        configured = str(get_global_config("ISOSURFACE_PRIM_PATH", "") or "").strip()
        if configured:
            prim = stage.GetPrimAtPath(configured)
            if prim and prim.IsValid():
                return prim

        if bool(get_global_config("TEMP_VIS_USE_STAGE_TOPOLOGY_JSON", True)):
            for path in _get_topology_paths_by_name("Isosurface"):
                prim = stage.GetPrimAtPath(path)
                if prim and prim.IsValid():
                    return prim

        traversal = stage.TraverseAll() if hasattr(stage, "TraverseAll") else stage.Traverse()
        for prim in traversal:
            if prim and prim.IsValid() and prim.GetName() == "Isosurface":
                return prim
        return None

    def _bind_display_color_primvar(self, stage, prim):
        try:
            session_layer = stage.GetSessionLayer()
            previous_edit_target = stage.GetEditTarget()
            if session_layer is not None:
                stage.SetEditTarget(session_layer)
            try:
                primvars_api = UsdGeom.PrimvarsAPI(prim)
                primvar = primvars_api.CreatePrimvar(
                    "displayColor",
                    Sdf.ValueTypeNames.Color3fArray,
                    UsdGeom.Tokens.constant,
                )
                return primvar.GetAttr()
            finally:
                if previous_edit_target is not None:
                    stage.SetEditTarget(previous_edit_target)
        except Exception as exc:
            carb.log_warn(f"[Aquacast Temp] CreatePrimvar failed: {exc}")
            return None

    def _write_color(self, stage, r, g, b):
        if self._display_color_attr is None:
            return

        rgb = (
            max(0.0, min(1.0, r)),
            max(0.0, min(1.0, g)),
            max(0.0, min(1.0, b)),
        )
        if self._prev_rgb is not None and all(
            abs(current - previous) <= (0.5 / 255.0)
            for current, previous in zip(rgb, self._prev_rgb)
        ):
            return

        try:
            session_layer = stage.GetSessionLayer()
            if session_layer is not None:
                with Usd.EditContext(stage, session_layer):
                    self._display_color_attr.Set(Vt.Vec3fArray([Gf.Vec3f(*rgb)]))
            else:
                self._display_color_attr.Set(Vt.Vec3fArray([Gf.Vec3f(*rgb)]))
            self._prev_rgb = rgb
        except Exception as exc:
            carb.log_warn(f"[Aquacast Temp] Failed to write displayColor: {exc}")

    def _sorted_stops(self, stops):
        if stops is not self._color_stops_cached:
            self._color_stops_cached = stops
            try:
                self._color_stops_sorted = sorted(stops, key=lambda stop: stop[0])
            except Exception:
                self._color_stops_sorted = []
        return self._color_stops_sorted

    def _on_update(self, _event):
        if not getattr(self, "_active", True):
            return
        now = time.time()

        if not self._initialized:
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

        t_room = float(get_global_config("ROOM_TEMP_C", 22.0))
        t_inlet = float(get_global_config("INLET_WATER_TEMP_C", 14.0))
        k_room = float(get_global_config("THERMAL_K_ROOM", 0.012))
        k_inflow = float(get_global_config("THERMAL_K_INFLOW", 0.022))

        self._T = thermal_dynamics.step_temperature(
            self._T,
            dt,
            T_room=t_room,
            T_inlet=t_inlet,
            k_room=k_room,
            k_inflow=k_inflow,
            inflow_enabled=self._inflow_enabled,
        )

        stops = self._sorted_stops(get_global_config("TEMP_COLOR_STOPS", []))
        stage = omni.usd.get_context().get_stage()
        if bool(get_global_config("ENABLE_PARTICLE_SYSTEM_TEMP_COLOR", False)) and stops and stage is not None:
            r, g, b = thermal_dynamics.temperature_to_rgb(self._T, stops)
            self._write_color(stage, r, g, b)
        if stage is not None:
            self._write_particle_samples(stage)

        self._maybe_log(now, t_room, t_inlet, k_room, k_inflow)

    def _maybe_log(self, now, t_room, t_inlet, k_room, k_inflow):
        interval = float(get_global_config("TEMP_VIS_LOG_INTERVAL_SECONDS", 5.0))
        if interval <= 0.0 or now - self._last_log_time < interval:
            return

        self._last_log_time = now
        eq = thermal_dynamics.equilibrium_temperature(
            T_room=t_room,
            T_inlet=t_inlet,
            k_room=k_room,
            k_inflow=k_inflow,
            inflow_enabled=self._inflow_enabled,
        )
        eq_str = f"{eq:.2f} C" if eq is not None else "n/a"
        carb.log_info(
            f"[Aquacast Temp] T={self._T:.2f} C, eq={eq_str}, "
            f"inflow={'ON' if self._inflow_enabled else 'OFF'}"
        )

    def _on_stage_event(self, event):
        event_type = event.type
        if event_type == int(omni.usd.StageEventType.OPENED):
            self._initialized = False
            self._isosurface_prim = None
            self._display_color_attr = None
            self._water_prim = None
            self._particles_prim = None
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            self._particle_positions = []
            self._particle_heat_weights = []
            self._particle_temperatures = []
            self._prev_rgb = None
            self._T = float(get_global_config("INITIAL_WATER_TEMP_C", 14.0))
            self._last_update_time = None
            self._last_particle_update_time = 0.0
            self._particle_elapsed = 0.0
            self._next_init_retry_time = 0.0
            self._warned_missing_isosurface = False
            self._warned_missing_water = False
            for frames in (1, 3, 10, 30):
                asyncio.ensure_future(self._initialize_after_frames(frames))
        elif event_type == int(omni.usd.StageEventType.ASSETS_LOADED):
            if not self._initialized:
                for frames in (1, 3, 10):
                    asyncio.ensure_future(self._initialize_after_frames(frames))
        elif event_type == int(omni.usd.StageEventType.CLOSED):
            self._initialized = False
            self._isosurface_prim = None
            self._display_color_attr = None
            self._water_prim = None
            self._particles_prim = None
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            self._particle_positions = []
            self._particle_heat_weights = []
            self._prev_rgb = None
            self._last_update_time = None
            self._last_particle_update_time = 0.0
            self._particle_elapsed = 0.0
            self._next_init_retry_time = 0.0
            self._warned_missing_isosurface = False
            self._warned_missing_water = False


class WaterQualityController:
    """Drive water-quality state and expose sensor/particle scalar fields."""

    _PARTICLE_PRIMVAR_NAMES = {
        "temperature": "temperature",
        "dissolved_oxygen": "dissolved_oxygen",
        "tan": "tan",
        "co2": "co2",
        "alkalinity": "alkalinity",
        "ph": "ph",
        "nh3": "nh3",
    }

    def __init__(self):
        self._active = False
        self._update_sub = None
        self._model = None
        self._last_update_time = None
        self._last_log_time = 0.0
        self._last_particle_write_time = 0.0
        self._last_particle_field_write_time = 0.0
        self._next_load_retry_time = 0.0
        self._particle_primvars = {}
        self._display_color_attr = None
        self._sphere_color_attrs = []
        self._view_variable_override = None
        self._using_backend = False

    def start(self):
        self._active = True
        self._load_model()
        self._update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update,
            name="aquacast_water_quality_update",
        )

    def stop(self):
        self._active = False
        self._update_sub = None
        self._model = None
        self._particle_primvars = {}
        self._display_color_attr = None
        self._sphere_color_attrs = []
        self._last_update_time = None

    def sample_sensor(self, sensor_name=None):
        if self._model is None:
            return {"status": "water quality model is not ready"}
        name = str(sensor_name or get_global_config("WQ_DEFAULT_SENSOR_NAME", "mixed_tank_outlet") or "").strip()
        if "/" in name:
            name = Path(name).name
        if not name:
            name = "mixed_tank_outlet"
        reading = self._model.sensor_reading(name).as_dict()
        reading["status"] = "ok"
        reading["sensor_path"] = self._sensor_path_for_name(name)
        return reading

    def sample_all_sensors(self):
        names = get_global_config("WQ_SENSOR_PRIM_NAMES", list(water_quality_model.DEFAULT_SENSOR_NAMES))
        return [self.sample_sensor(name) for name in names]

    def snapshot(self):
        if self._model is None:
            return {"status": "water quality model is not ready"}
        snap = self._model.snapshot()
        snap["status"] = "ok"
        snap["view_variable"] = self._view_variable()
        return snap

    def apply_feed(self, mass_kg):
        if self._model is not None:
            self._model.apply_feed(mass_kg)

    def set_water_exchange(self, q_lph):
        if self._model is not None:
            self._model.set_water_exchange(q_lph)

    def set_inflow(self, enabled):
        if self._model is not None:
            self._model.set_inflow(enabled)

    def set_heater(self, power):
        if self._model is not None:
            self._model.set_heater(power)

    def set_biofilter(self, enabled):
        if self._model is not None:
            self._model.set_biofilter(enabled)

    def set_stock(self, n, w_kg):
        if self._model is not None:
            self._model.set_stock(n, w_kg)

    def load_scenario(self, name):
        if self._model is None:
            return False
        loaded = self._model.load_scenario(str(name))
        if loaded:
            carb.log_info(f"[Aquacast WQ] Loaded scenario={name}")
        return loaded

    def set_view_variable(self, variable):
        value = str(variable or "").strip().lower()
        aliases = {
            "do": "dissolved_oxygen",
            "dissolved_o2": "dissolved_oxygen",
            "oxygen": "dissolved_oxygen",
            "alk": "alkalinity",
        }
        value = aliases.get(value, value)
        if value in self._PARTICLE_PRIMVAR_NAMES:
            self._view_variable_override = value
            self._last_particle_write_time = 0.0
            carb.log_info(f"[Aquacast WQ] View variable={value}")

    def _load_model(self):
        base = Path(__file__).resolve().parent
        constants_path = Path(get_global_config("WQ_CONSTANTS_JSON_PATH", str(base / "data" / "wq_constants.json")))
        feed_rate_path = Path(get_global_config("WQ_FEED_RATE_JSON_PATH", str(base / "data" / "wq_feed_rate.json")))
        scenarios_path = Path(get_global_config("WQ_SCENARIOS_JSON_PATH", str(base / "data" / "wq_scenarios.json")))
        scenario_name = str(get_global_config("WQ_SCENARIO_NAME", "baseline") or "baseline")
        try:
            if bool(get_global_config("WQ_BACKEND_ENABLED", False)):
                backend_url = str(get_global_config("WQ_BACKEND_URL", "http://127.0.0.1:8765") or "http://127.0.0.1:8765")
                timeout_s = float(get_global_config("WQ_BACKEND_TIMEOUT_SECONDS", 0.25))
                client = water_quality_backend_client.WaterQualityBackendClient(backend_url, timeout_s=timeout_s)
                client.health()
                if bool(get_global_config("WQ_BACKEND_RESET_ON_CONNECT", False)):
                    client.reset(scenario_name)
                self._model = client
                self._using_backend = True
                carb.log_info(f"[Aquacast WQ] Connected to backend={backend_url}")
            else:
                self._model = water_quality_model.load_model(constants_path, feed_rate_path, scenarios_path, scenario_name)
                self._apply_global_overrides()
                self._using_backend = False
                carb.log_info(f"[Aquacast WQ] Initialized scenario={scenario_name}")
        except Exception as exc:
            self._model = None
            self._using_backend = False
            self._next_load_retry_time = time.time() + 1.0
            carb.log_warn(f"[Aquacast WQ] Failed to load water quality model: {exc}")

    def _apply_global_overrides(self):
        if self._model is None:
            return
        params = self._model.params
        overrides = {
            "time_scale": "WQ_TIME_SCALE",
            "substep_h": "WQ_SUBSTEP_H",
            "tank_volume_l": "WQ_TANK_VOLUME_L",
            "fish_count": "WQ_FISH_COUNT",
            "fish_weight_kg": "WQ_FISH_WEIGHT_KG",
            "flow_lph": "WQ_FLOW_LPH",
            "protein_content": "WQ_PROTEIN_CONTENT",
            "kla_o2_h": "WQ_KLA_O2",
            "kla_co2_h": "WQ_KLA_CO2",
            "k_nitrif_h": "WQ_K_NITRIF",
            "vtr_max_mg_l_h": "WQ_VTR_MAX",
            "tau_feed_h": "WQ_TAU_FEED_H",
            "do_maxFI": "WQ_DO_MAXFI",
            "do_zero": "WQ_DO_ZERO",
            "do_in": "WQ_DO_IN",
            "co2_eq": "WQ_CO2_EQ",
            "alk_in": "WQ_ALK_IN",
            "biofilter_on": "WQ_BIOFILTER_DEFAULT",
        }
        for param_name, config_name in overrides.items():
            value = get_global_config(config_name, None)
            if value is not None:
                params[param_name] = value
        state = self._model.state
        state.dissolved_oxygen_mg_l = float(get_global_config("WQ_INIT_DO", state.dissolved_oxygen_mg_l))
        state.tan_mg_l = float(get_global_config("WQ_INIT_TAN", state.tan_mg_l))
        state.co2_mg_l = float(get_global_config("WQ_INIT_CO2", state.co2_mg_l))
        state.alkalinity_mg_l_as_caco3 = float(get_global_config("WQ_INIT_ALK", state.alkalinity_mg_l_as_caco3))

    def _on_update(self, _event):
        if not self._active:
            return
        if self._model is None:
            if time.time() < self._next_load_retry_time:
                return
            self._load_model()
            if self._model is None:
                return

        now = time.time()
        if self._last_update_time is None:
            self._last_update_time = now
            return

        interval = max(0.05, float(get_global_config("WQ_UPDATE_INTERVAL_SECONDS", 0.5)))
        if now - self._last_update_time < interval:
            return

        dt = min(now - self._last_update_time, 5.0)
        self._last_update_time = now
        if self._model is not None:
            inflow_enabled = self._current_inflow_enabled()
            if hasattr(self._model, "params"):
                self._model.params["inflow_enabled"] = inflow_enabled
            elif hasattr(self._model, "set_inflow"):
                self._model.set_inflow(inflow_enabled)
        state = self._model.advance(dt, temperature_c=self._current_temperature_c())

        stage = omni.usd.get_context().get_stage()
        if stage is not None and bool(get_global_config("WQ_WRITE_PARTICLE_PRIMVARS", True)):
            self._write_particle_primvars(stage, now)
        self._maybe_log(now, state)

    def _current_temperature_c(self):
        controller = globals().get("_water_temp_controller")
        if controller is not None and hasattr(controller, "_T"):
            try:
                return float(controller._T)
            except Exception:
                pass
        return float(get_global_config("INITIAL_WATER_TEMP_C", 14.0))

    def _current_inflow_enabled(self):
        controller = globals().get("_water_temp_controller")
        if controller is not None and hasattr(controller, "is_inflow_enabled"):
            try:
                return bool(controller.is_inflow_enabled())
            except Exception:
                pass
        return True

    def _write_particle_primvars(self, stage, now):
        update_interval = float(get_global_config("WQ_PARTICLE_UPDATE_INTERVAL_SECONDS", 1.0))
        if update_interval > 0.0 and now - self._last_particle_write_time < update_interval:
            return
        field_interval = float(get_global_config("WQ_PARTICLE_FIELD_UPDATE_INTERVAL_SECONDS", 0.5))
        write_all_fields = now - self._last_particle_field_write_time >= max(0.0, field_interval)

        temp_controller = globals().get("_water_temp_controller")
        if temp_controller is None:
            return
        heat_weights = getattr(temp_controller, "_particle_heat_weights", None) or []
        positions = getattr(temp_controller, "_particle_positions", None) or []
        if not heat_weights:
            return

        particle_path = str(get_global_config("TEMP_PARTICLE_PRIM_PATH", "") or "")
        prim = stage.GetPrimAtPath(particle_path) if particle_path else None
        if not prim or not prim.IsValid():
            return

        if not self._particle_primvars and not self._sphere_color_attrs and self._display_color_attr is None:
            self._bind_particle_primvars(stage, prim)
        if not self._particle_primvars and not self._sphere_color_attrs and self._display_color_attr is None:
            return

        values = self._model.particle_values(heat_weights, positions)
        temperature_values = self._temperature_particle_values(temp_controller, len(heat_weights))
        if temperature_values:
            values["temperature"] = temperature_values
        elif self._view_variable() == "temperature":
            return
        display_colors = self._display_colors(values)
        session_layer = stage.GetSessionLayer()
        try:
            edit_context = Usd.EditContext(stage, session_layer) if session_layer is not None else None
            if edit_context is not None:
                edit_context.__enter__()
            try:
                if write_all_fields:
                    if self._particle_primvars:
                        for key, attr in self._particle_primvars.items():
                            field = values.get(key)
                            if field:
                                attr.Set(Vt.FloatArray(field))
                    self._last_particle_field_write_time = now
                if display_colors:
                    if self._sphere_color_attrs:
                        for attr, color in zip(self._sphere_color_attrs, display_colors):
                            attr.Set(Vt.Vec3fArray([color]))
                    elif self._display_color_attr is not None:
                        self._display_color_attr.Set(Vt.Vec3fArray(display_colors))
            finally:
                if edit_context is not None:
                    edit_context.__exit__(None, None, None)
        except Exception as exc:
            carb.log_warn(f"[Aquacast WQ] Failed to write particle primvars: {exc}")
            self._particle_primvars = {}
            self._display_color_attr = None
            self._sphere_color_attrs = []
            return
        self._last_particle_write_time = now

    def _temperature_particle_values(self, temp_controller, expected_count):
        temperatures = getattr(temp_controller, "_particle_temperatures", None) or []
        if len(temperatures) == expected_count:
            return [float(value) for value in temperatures]

        heat_weights = getattr(temp_controller, "_particle_heat_weights", None) or []
        if len(heat_weights) != expected_count:
            return []
        try:
            bulk_t = float(getattr(temp_controller, "_T", get_global_config("INITIAL_WATER_TEMP_C", 14.0)))
            elapsed = float(getattr(temp_controller, "_particle_elapsed", 0.0))
            heat_delta = float(get_global_config("TEMP_PARTICLE_HEAT_DELTA_C", 0.0))
            spread_rate = float(get_global_config("TEMP_PARTICLE_SPREAD_RATE", 0.05))
            spread = 1.0 - math.exp(-max(0.0, elapsed) * max(0.0, spread_rate))
            return [bulk_t + heat_delta * float(weight) * spread for weight in heat_weights]
        except Exception:
            return []

    def _bind_particle_primvars(self, stage, prim):
        try:
            session_layer = stage.GetSessionLayer()
            edit_context = Usd.EditContext(stage, session_layer) if session_layer is not None else None
            if edit_context is not None:
                edit_context.__enter__()
            try:
                sphere_attrs = []
                for child in prim.GetChildren():
                    if child and child.IsValid() and child.GetTypeName() == "Sphere":
                        color_attr = UsdGeom.Sphere(child).CreateDisplayColorAttr()
                        sphere_attrs.append(color_attr)
                self._sphere_color_attrs = sphere_attrs
                if sphere_attrs:
                    self._particle_primvars = {}
                    self._display_color_attr = None
                    return

                primvars_api = UsdGeom.PrimvarsAPI(prim)
                self._particle_primvars = {
                    key: primvars_api.CreatePrimvar(
                        name,
                        Sdf.ValueTypeNames.FloatArray,
                        UsdGeom.Tokens.vertex,
                    ).GetAttr()
                    for key, name in self._PARTICLE_PRIMVAR_NAMES.items()
                }
                self._display_color_attr = primvars_api.CreatePrimvar(
                    "displayColor",
                    Sdf.ValueTypeNames.Color3fArray,
                    UsdGeom.Tokens.vertex,
                ).GetAttr()
            finally:
                if edit_context is not None:
                    edit_context.__exit__(None, None, None)
        except Exception as exc:
            carb.log_warn(f"[Aquacast WQ] Failed to bind particle primvars: {exc}")
            self._particle_primvars = {}
            self._display_color_attr = None
            self._sphere_color_attrs = []

    def _display_colors(self, values):
        view = self._view_variable()
        field = values.get(view) or values.get("temperature") or []
        if not field:
            return []
        stops = self._color_stops_for_view(view)
        if not stops:
            stops = self._color_stops_for_view("temperature")
        return self._ramp_colors(field, stops)

    def _ramp_colors(self, field, stops):
        if not stops:
            return []
        values = np.asarray(field, dtype=np.float64)
        stop_values = np.asarray([float(stop[0]) for stop in stops], dtype=np.float64)
        stop_colors = np.asarray([stop[1] for stop in stops], dtype=np.float64)
        if len(stop_values) == 1:
            rgb = np.repeat(stop_colors[:1], len(values), axis=0)
        else:
            rgb = np.column_stack(
                [
                    np.interp(values, stop_values, stop_colors[:, channel])
                    for channel in range(3)
                ]
            )
        rgb = np.clip(rgb, 0.0, 1.0)
        return [Gf.Vec3f(float(row[0]), float(row[1]), float(row[2])) for row in rgb]

    def _view_variable(self):
        configured = self._view_variable_override
        value = str(configured or get_global_config("WQ_VIEW_VARIABLE", "temperature") or "temperature").strip().lower()
        aliases = {
            "do": "dissolved_oxygen",
            "dissolved_o2": "dissolved_oxygen",
            "oxygen": "dissolved_oxygen",
            "alk": "alkalinity",
        }
        value = aliases.get(value, value)
        if value not in self._PARTICLE_PRIMVAR_NAMES:
            return "temperature"
        return value

    def _color_stops_for_view(self, view):
        config_name = {
            "temperature": "TEMP_COLOR_STOPS",
            "dissolved_oxygen": "DO_COLOR_STOPS",
            "tan": "TAN_COLOR_STOPS",
            "co2": "CO2_COLOR_STOPS",
            "ph": "PH_COLOR_STOPS",
            "alkalinity": "ALK_COLOR_STOPS",
            "nh3": "NH3_COLOR_STOPS",
        }.get(view, "TEMP_COLOR_STOPS")
        try:
            return sorted(get_global_config(config_name, []) or [], key=lambda stop: stop[0])
        except Exception:
            return []

    def _sensor_path_for_name(self, sensor_name):
        for path in _get_topology_paths_by_name(sensor_name):
            return path
        suffix = f"/{sensor_name}"
        stage = omni.usd.get_context().get_stage()
        if stage is not None:
            for prim in stage.Traverse():
                if prim and prim.IsValid() and prim.GetPath().pathString.endswith(suffix):
                    return prim.GetPath().pathString
        return ""

    def _maybe_log(self, now, state):
        interval = float(get_global_config("WQ_LOG_INTERVAL_SECONDS", 5.0))
        if interval <= 0.0 or now - self._last_log_time < interval:
            return
        self._last_log_time = now
        snapshot = self._model.snapshot() if self._model is not None else state.as_dict()
        carb.log_info(
            f"[Aquacast WQ] sim_h={snapshot.get('sim_time_h', 0.0):.2f}, "
            f"T={snapshot.get('temperature_c', 0.0):.2f} C, "
            f"DO={snapshot.get('dissolved_oxygen_mg_l', 0.0):.2f} mg/L, "
            f"TAN={snapshot.get('tan_mg_l', 0.0):.3f} mg/L, "
            f"CO2={snapshot.get('co2_mg_l', 0.0):.2f} mg/L, "
            f"Alk={snapshot.get('alkalinity_mg_l_as_caco3', 0.0):.1f} mg/L, "
            f"pH={snapshot.get('ph', 0.0):.2f}, NH3={snapshot.get('nh3_mg_l', 0.0):.4f} mg/L, "
            f"view={self._view_variable()}"
        )


if __name__ == "__main__":
    print("This module is loaded by the Aquacast Kit extension.")
