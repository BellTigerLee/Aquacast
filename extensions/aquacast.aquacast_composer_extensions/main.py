import asyncio
import importlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import dynamic_fish_spawn  # noqa: E402
import fish_dynamics  # noqa: E402
import thermal_dynamics  # noqa: E402
import water_quality_backend_client  # noqa: E402
import water_quality_dynamics  # noqa: E402
import water_quality_model  # noqa: E402

dynamic_fish_spawn = importlib.reload(dynamic_fish_spawn)
water_quality_backend_client = importlib.reload(water_quality_backend_client)
water_quality_dynamics = importlib.reload(water_quality_dynamics)
water_quality_model = importlib.reload(water_quality_model)

import carb  # noqa: E402
import omni.kit.app  # noqa: E402
import omni.usd  # noqa: E402
from pxr import Gf, Sdf, Usd, UsdGeom, Vt  # noqa: E402

_stage_structure_cache = None
_dynamic_fish_spawner = None
_fish_swim_controller = None
_water_temp_controller = None
_water_quality_controller = None
_topology_json_cache = {
    "path": None,
    "mtime_ns": None,
    "snapshot": {},
    "name_index": {},
}


def should_print_stage_topology():
    return bool(get_global_config("PRINT_STAGE_TOPOLOGY", False))


def should_export_stage_topology_json():
    return bool(get_global_config("EXPORT_STAGE_TOPOLOGY_JSON", False))


def should_use_stage_structure_cache():
    default = should_print_stage_topology() or should_export_stage_topology_json()
    return bool(get_global_config("ENABLE_STAGE_STRUCTURE_CACHE", default))


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


def _env_value(name):
    return os.environ.get(f"AQUACAST_{name}")


def _warn_env_parse(name, raw, default):
    carb.log_warn(f"[Aquacast] Invalid AQUACAST_{name}={raw!r}; using default={default!r}")


def _resolve_dynamic_count(default):
    raw = _env_value("DYNAMIC_FISH_COUNT")
    value = dynamic_fish_spawn.resolve_count(raw, default)
    if raw is not None:
        try:
            dynamic_fish_spawn.resolve_count(raw, default)
            int(float(str(raw).strip()))
        except (TypeError, ValueError):
            _warn_env_parse("DYNAMIC_FISH_COUNT", raw, default)
    return value


def _resolve_dynamic_scale(default, env_name="DYNAMIC_FISH_SCALE"):
    raw = _env_value(env_name)
    value = dynamic_fish_spawn.resolve_scale(raw, default)
    if raw is not None:
        try:
            float(str(raw).strip())
        except (TypeError, ValueError):
            _warn_env_parse(env_name, raw, default)
    return value


def _resolve_dynamic_mix(default):
    raw = _env_value("SALMON_MIX")
    value = dynamic_fish_spawn.resolve_mix_ratio(raw, default)
    if raw is not None:
        try:
            float(str(raw).strip())
        except (TypeError, ValueError):
            _warn_env_parse("SALMON_MIX", raw, default)
    return value


def _fish_rng_seed_is_random(default):
    raw = _env_value("FISH_RNG_SEED")
    value = default if raw is None else raw
    return dynamic_fish_spawn.is_random_seed(value)


def _resolve_fish_rng_seed(default):
    raw = _env_value("FISH_RNG_SEED")
    value = dynamic_fish_spawn.resolve_seed(raw, default)
    if raw is not None and not dynamic_fish_spawn.is_random_seed(raw):
        try:
            int(float(str(raw).strip()))
        except (TypeError, ValueError):
            _warn_env_parse("FISH_RNG_SEED", raw, default)
    return value


def _resolve_dynamic_asset(env_name, default):
    return dynamic_fish_spawn.resolve_asset_path(_env_value(env_name), default)


def start_stage_structure_cache():
    global _stage_structure_cache
    if not should_use_stage_structure_cache():
        carb.log_info("[Aquacast] Stage structure cache disabled")
        return None
    if _stage_structure_cache is None:
        _stage_structure_cache = StageStructureCache()
        _stage_structure_cache.start()
    return _stage_structure_cache


def start_dynamic_fish_spawner():
    global _dynamic_fish_spawner
    if _dynamic_fish_spawner is None:
        _dynamic_fish_spawner = DynamicFishSpawner()
        _dynamic_fish_spawner.start()
    return _dynamic_fish_spawner


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


def stop_dynamic_fish_spawner():
    global _dynamic_fish_spawner
    if _dynamic_fish_spawner is not None:
        _dynamic_fish_spawner.stop()
        _dynamic_fish_spawner = None


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


def _build_topology_name_index(snapshot):
    index = {}
    stack = list(snapshot.get("tree", []) or [])
    while stack:
        node = stack.pop()
        node_name = node.get("name")
        path = node.get("path")
        if node_name and path:
            index.setdefault(node_name, []).append(path)
        stack.extend(node.get("children", []) or [])
    for paths in index.values():
        paths.sort()
    return index


def _load_topology_json_snapshot():
    topology_path = get_stage_topology_json_path()
    if not topology_path.exists():
        _topology_json_cache.update({"path": topology_path, "mtime_ns": None, "snapshot": {}, "name_index": {}})
        return {}, {}

    try:
        mtime_ns = topology_path.stat().st_mtime_ns
        if (
            _topology_json_cache.get("path") == topology_path
            and _topology_json_cache.get("mtime_ns") == mtime_ns
        ):
            return _topology_json_cache.get("snapshot", {}), _topology_json_cache.get("name_index", {})

        with topology_path.open("r", encoding="utf-8") as stream:
            snapshot = json.load(stream)
        name_index = _build_topology_name_index(snapshot)
        _topology_json_cache.update({
            "path": topology_path,
            "mtime_ns": mtime_ns,
            "snapshot": snapshot,
            "name_index": name_index,
        })
        return snapshot, name_index
    except Exception as exc:
        carb.log_warn(f"[Aquacast] Failed to read stage topology JSON: {topology_path} ({exc})")
        _topology_json_cache.update({"path": topology_path, "mtime_ns": None, "snapshot": {}, "name_index": {}})
        return {}, {}


def _get_topology_snapshot():
    if _stage_structure_cache is not None:
        snapshot = _stage_structure_cache.get_snapshot()
        if snapshot.get("tree"):
            return snapshot

    snapshot, _name_index = _load_topology_json_snapshot()
    return snapshot


def _iter_topology_nodes(nodes):
    for node in nodes or []:
        yield node
        yield from _iter_topology_nodes(node.get("children", []))


def _get_topology_paths_by_name(name):
    if _stage_structure_cache is None:
        _snapshot, name_index = _load_topology_json_snapshot()
        return list(name_index.get(name, []))

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


def _sample_color_stops(stops, count):
    count = max(1, int(count))
    if not stops:
        return [Gf.Vec3f(0.0, 0.75, 0.75) for _ in range(count)]
    sorted_stops = sorted(stops, key=lambda stop: stop[0])
    stop_values = np.asarray([float(stop[0]) for stop in sorted_stops], dtype=np.float64)
    stop_colors = np.asarray([stop[1] for stop in sorted_stops], dtype=np.float64)
    if len(stop_values) == 1:
        colors = np.repeat(stop_colors[:1], count, axis=0)
    else:
        samples = np.linspace(stop_values[0], stop_values[-1], count)
        colors = np.column_stack([
            np.interp(samples, stop_values, stop_colors[:, channel])
            for channel in range(3)
        ])
    colors = np.clip(colors, 0.0, 1.0)
    return [Gf.Vec3f(float(row[0]), float(row[1]), float(row[2])) for row in colors]


def _colors_to_proto_indices(colors, palette):
    if not colors or not palette:
        return Vt.IntArray()
    color_values = np.asarray(
        [[float(color[0]), float(color[1]), float(color[2])] for color in colors],
        dtype=np.float64,
    )
    palette_values = np.asarray(
        [[float(color[0]), float(color[1]), float(color[2])] for color in palette],
        dtype=np.float64,
    )
    distances = np.sum((color_values[:, None, :] - palette_values[None, :, :]) ** 2, axis=2)
    return Vt.IntArray([int(index) for index in np.argmin(distances, axis=1)])


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


def _water_up_axis_index():
    up_axis_name = str(get_global_config("FISH_WATER_UP_AXIS", get_global_config("TEMP_PARTICLE_UP_AXIS", "Y")) or "Y").upper()
    return {"X": 0, "Y": 1, "Z": 2}.get(up_axis_name, 1)


def _compute_water_bounds_with_axes(water_prim):
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
    up_axis = _water_up_axis_index()
    radial_axes = [index for index in range(3) if index != up_axis]
    size = [max_v[index] - min_v[index] for index in range(3)]
    radius = max(0.001, min(size[radial_axes[0]], size[radial_axes[1]]) * 0.5)
    vertical_margin = size[up_axis] * 0.08
    return center, radius, min_v[up_axis] + vertical_margin, max_v[up_axis] - vertical_margin, up_axis, radial_axes


def _compute_water_bounds_from_prim(water_prim):
    center, radius, min_up, max_up, _up_axis, _radial_axes = _compute_water_bounds_with_axes(water_prim)
    return center, radius, min_up, max_up


def _find_water_prim_for_tank(stage, tank_path):
    tank = stage.GetPrimAtPath(tank_path)
    if tank and tank.IsValid():
        if tank.GetName() == "Water":
            return tank
        candidates = []
        for prim in Usd.PrimRange(tank):
            if prim and prim.IsValid() and prim.GetName() == "Water":
                path = prim.GetPath().pathString
                candidates.append((
                    0 if "/InWater/" in path or path.endswith("/InWater/Water") else 1,
                    0 if "/Looks/" not in path and "/Materials/" not in path else 1,
                    path,
                    prim,
                ))
        if candidates:
            return sorted(candidates, key=lambda item: item[:3])[0][3]

    configured_path = str(get_global_config("WATER_PRIM_PATH", "") or "")
    if configured_path:
        prim = stage.GetPrimAtPath(configured_path)
        if prim and prim.IsValid():
            return prim
    return None


def _compute_water_bounds_for_tank(stage, tank_path):
    water_prim = _find_water_prim_for_tank(stage, tank_path)
    if not water_prim or not water_prim.IsValid():
        return None
    return _compute_water_bounds_from_prim(water_prim)


def _remove_composed_child(stage, path):
    stage.RemovePrim(path)
    prim = stage.GetPrimAtPath(path)
    if prim and prim.IsValid():
        prim.SetActive(False)


def _set_single_reference(prim, asset_path):
    refs = prim.GetReferences()
    if hasattr(refs, "SetReferences"):
        refs.SetReferences([Sdf.Reference(asset_path)])
    else:
        refs.ClearReferences()
        refs.AddReference(asset_path)


def _spawn_fish_in_tank(
    stage,
    tank_path: str,
    count: int,
    *,
    salmon_scales: tuple[float, float],
    asset_paths: tuple[str, str],
    mix_ratio: float,
    seed: int,
    water_bounds: tuple[float, float, float] | None = None,
) -> list[str]:
    if count <= 0:
        return []

    water_prim = _find_water_prim_for_tank(stage, tank_path)
    if not water_prim or not water_prim.IsValid():
        carb.log_warn(f"[Aquacast] Dynamic fish skipped: Water prim not found for tank={tank_path}")
        return []

    if water_bounds is None:
        center, radius, min_up, max_up, up_axis, radial_axes = _compute_water_bounds_with_axes(water_prim)
    else:
        radius, min_up, max_up = water_bounds
        center = Gf.Vec3d(0.0, 0.0, 0.0)
        up_axis = 2
        radial_axes = [0, 1]

    fishes_path = water_prim.GetPath().GetParentPath().AppendChild("Fishes")
    session_layer = stage.GetSessionLayer()
    edit_target = session_layer if session_layer is not None else stage.GetRootLayer()
    created = []

    with Usd.EditContext(stage, edit_target):
        existing_fishes = stage.GetPrimAtPath(fishes_path)
        removed_paths = []
        if existing_fishes and existing_fishes.IsValid():
            removed_paths = [prim.GetPath() for prim in Usd.PrimRange(existing_fishes)]
            for prim_path in sorted(removed_paths, key=lambda path: len(path.pathString), reverse=True):
                stage.RemovePrim(prim_path)
                prim = stage.GetPrimAtPath(prim_path)
                if prim and prim.IsValid():
                    prim.SetActive(False)
            carb.log_info(
                f"[Aquacast] Dynamic fish Fishes group reset: removed={len(removed_paths)} path={fishes_path}"
            )

        fishes_parent = stage.DefinePrim(fishes_path, "Xform")
        fishes_parent.SetActive(True)

        prefix = str(get_global_config("FISH_NAME_PREFIX", "Fish_"))
        indices = dynamic_fish_spawn.next_fish_indices([], count, prefix)
        asset_choices = dynamic_fish_spawn.assign_assets(count, mix_ratio, seed)
        positions = dynamic_fish_spawn.sample_positions(count, radius, min_up, max_up, seed + 1)
        yaws = dynamic_fish_spawn.sample_yaws(count, seed + 2)

        for index, asset_index, position, yaw in zip(indices, asset_choices, positions, yaws):
            asset_path = asset_paths[asset_index]
            fish_scale = salmon_scales[asset_index]
            if not os.path.exists(asset_path):
                carb.log_warn(f"[Aquacast] Dynamic fish asset missing; skipping path={asset_path}")
                continue
            fish_path = fishes_path.AppendChild(f"{prefix}{index:02d}")
            asset_prim_path = fish_path.AppendChild("Asset")
            try:
                fish_prim = stage.DefinePrim(fish_path, "Xform")
                fish_prim.SetActive(True)
                for child in list(fish_prim.GetChildren()):
                    _remove_composed_child(stage, child.GetPath())

                xform = UsdGeom.Xformable(fish_prim)
                xform.ClearXformOpOrder()
                coords = [float(center[0]), float(center[1]), float(center[2])]
                coords[radial_axes[0]] = float(center[radial_axes[0]] + position[0])
                coords[radial_axes[1]] = float(center[radial_axes[1]] + position[1])
                coords[up_axis] = float(position[2])
                xform.AddTranslateOp(precision=UsdGeom.XformOp.PrecisionDouble).Set(
                    Gf.Vec3d(coords[0], coords[1], coords[2])
                )
                xform.AddRotateXYZOp(precision=UsdGeom.XformOp.PrecisionFloat).Set(
                    Gf.Vec3f(0.0, 0.0, float(yaw))
                )
                xform.AddScaleOp(precision=UsdGeom.XformOp.PrecisionFloat).Set(
                    Gf.Vec3f(fish_scale, fish_scale, fish_scale)
                )

                asset_prim = stage.DefinePrim(asset_prim_path, "Xform")
                asset_prim.SetActive(True)
                _set_single_reference(asset_prim, asset_path)
                created.append(fish_path.pathString)
            except Exception as exc:
                carb.log_warn(f"[Aquacast] Dynamic fish spawn failed path={fish_path}: {exc}")

    return created


class DynamicFishSpawner:
    def __init__(self):
        self._stage_event_sub = None
        self._spawned_stage_key = None

    def start(self):
        usd_context = omni.usd.get_context()
        self._stage_event_sub = usd_context.get_stage_event_stream().create_subscription_to_pop(
            self._on_stage_event,
            name="aquacast_dynamic_fish_spawn_stage",
        )
        asyncio.ensure_future(self.spawn_after_frames(2))

    def stop(self):
        self._stage_event_sub = None

    async def spawn_after_frames(self, frames=1):
        app = omni.kit.app.get_app()
        for _ in range(frames):
            await app.next_update_async()
        self._spawn_all_tanks()

    def _on_stage_event(self, event):
        if event.type == int(omni.usd.StageEventType.OPENED):
            self._spawned_stage_key = None
            asyncio.ensure_future(self.spawn_after_frames(2))

    def _spawn_all_tanks(self):
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            return
        root_layer = stage.GetRootLayer()
        stage_key = root_layer.identifier if root_layer else ""
        seed_config = get_global_config("FISH_RNG_SEED", 42)
        random_seed_mode = _fish_rng_seed_is_random(seed_config)
        if stage_key and self._spawned_stage_key == stage_key and not random_seed_mode:
            return

        default_count = int(get_global_config("DYNAMIC_FISH_COUNT_PER_TANK", 0))
        count = _resolve_dynamic_count(default_count)
        if count <= 0:
            return

        scale = _resolve_dynamic_scale(float(get_global_config("DYNAMIC_FISH_SCALE", 10.0)))
        salmon_scales = (
            _resolve_dynamic_scale(
                float(get_global_config("DYNAMIC_FISH_SALMON_1_SCALE", scale)),
                "SALMON_1_SCALE",
            ),
            _resolve_dynamic_scale(
                float(get_global_config("DYNAMIC_FISH_SALMON_2_SCALE", scale)),
                "SALMON_2_SCALE",
            ),
        )
        mix_ratio = _resolve_dynamic_mix(float(get_global_config("DYNAMIC_FISH_SALMON_1_RATIO", 0.5)))
        asset_paths = (
            _resolve_dynamic_asset("SALMON_1_ASSET", str(get_global_config("DYNAMIC_FISH_SALMON_1_PATH", "~/cs-project/assets/salmon_1.usd"))),
            _resolve_dynamic_asset("SALMON_2_ASSET", str(get_global_config("DYNAMIC_FISH_SALMON_2_PATH", "~/cs-project/assets/salmon_2.usd"))),
        )
        seed = _resolve_fish_rng_seed(seed_config)

        tank_paths = self._discover_tanks(stage)
        if not tank_paths:
            carb.log_info("[Aquacast] Dynamic fish skipped: no Water prims found")
            return

        total = 0
        for offset, tank_path in enumerate(tank_paths):
            created = _spawn_fish_in_tank(
                stage,
                tank_path,
                count,
                salmon_scales=salmon_scales,
                asset_paths=asset_paths,
                mix_ratio=mix_ratio,
                seed=seed + offset * 1009,
            )
            total += len(created)
            if created:
                carb.log_info(f"[Aquacast] Dynamic fish spawned: count={len(created)} tank={tank_path}")

        if total:
            self._spawned_stage_key = stage_key
        if total and _stage_structure_cache is not None:
            _stage_structure_cache.refresh()
            if should_export_stage_topology_json():
                _stage_structure_cache.export_topology_json()
        if total and _fish_swim_controller is not None:
            _fish_swim_controller._initialized = False
            asyncio.ensure_future(_fish_swim_controller.initialize_after_frames(1))
    def _discover_tanks(self, stage):
        waters = []
        configured = str(get_global_config("WATER_PRIM_PATH", "") or "").strip()
        if configured:
            prim = stage.GetPrimAtPath(configured)
            if prim and prim.IsValid():
                return [prim.GetPath().pathString]

        for prim in stage.Traverse():
            if not prim or not prim.IsValid() or prim.GetName() != "Water":
                continue
            path = prim.GetPath().pathString
            if "/Looks/" in path or "/Materials/" in path:
                continue
            waters.append(path)
        return sorted(waters)

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
        self._water_up_axis = _water_up_axis_index()
        self._water_radial_axes = [index for index in range(3) if index != self._water_up_axis]
        self._water_bounds_by_fishes_parent = {}
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

        water_prims = self._find_water_prims(stage)
        if not water_prims:
            self._warn_missing_water_once(stage)
            self._initialized = False
            self._schedule_init_retry()
            return

        self._warned_missing_water = False
        self._water_bounds_by_fishes_parent = {}
        for water_prim in water_prims:
            bounds = self._read_water_bounds_values(water_prim)
            fishes_parent_path = water_prim.GetPath().GetParentPath().AppendChild("Fishes").pathString
            self._water_bounds_by_fishes_parent[fishes_parent_path] = bounds
        self._apply_water_bounds(self._water_bounds_by_fishes_parent[next(iter(self._water_bounds_by_fishes_parent))])
        fish_prims = self._find_fish_prims(stage)
        self._fish = [self._make_fish_state(prim, index) for index, prim in enumerate(fish_prims)]
        self._initialized = bool(self._fish)
        self._last_update_time = time.monotonic()
        carb.log_info(
            f"[Aquacast] Fish swimming initialized: fish_count={len(self._fish)}, "
            f"water_radius={self._water_radius:.3f}"
        )

    def _find_water_prims(self, stage):
        configured = str(get_global_config("WATER_PRIM_PATH", "") or "").strip()
        if configured:
            prim = stage.GetPrimAtPath(configured)
            return [prim] if prim and prim.IsValid() else []

        water_prims = []
        seen = set()
        for path in _get_topology_paths_by_name("Water"):
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                continue
            path_string = prim.GetPath().pathString
            if "/Looks/" in path_string or "/Materials/" in path_string:
                continue
            seen.add(path_string)
            water_prims.append(prim)

        for prim in stage.Traverse():
            if not prim or not prim.IsValid() or prim.GetName() != "Water":
                continue
            path_string = prim.GetPath().pathString
            if path_string in seen or "/Looks/" in path_string or "/Materials/" in path_string:
                continue
            seen.add(path_string)
            water_prims.append(prim)

        return sorted(water_prims, key=lambda prim: prim.GetPath().pathString)

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

    def _read_water_bounds_values(self, water_prim):
        return _compute_water_bounds_with_axes(water_prim)

    def _apply_water_bounds(self, bounds):
        center, radius, min_up, max_up, up_axis, radial_axes = bounds
        self._water_center = center
        self._water_radius = radius
        self._water_min_z = min_up
        self._water_max_z = max_up
        self._water_up_axis = up_axis
        self._water_radial_axes = list(radial_axes)

    def _read_water_bounds(self, water_prim):
        bounds = self._read_water_bounds_values(water_prim)
        self._apply_water_bounds(bounds)
        return bounds

    def _bounds_for_fish_prim(self, prim):
        parent_path = prim.GetPath().GetParentPath().pathString
        return self._water_bounds_by_fishes_parent.get(
            parent_path,
            (
                self._water_center,
                self._water_radius,
                self._water_min_z,
                self._water_max_z,
                self._water_up_axis,
                list(self._water_radial_axes),
            ),
        )

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
            if not _prim_matches_fish_root(prim, pattern, base_name):
                continue
            path = prim.GetPath().pathString
            parent_path = prim.GetPath().GetParentPath().pathString
            if parent_path == "/" or parent_path.endswith("/Fishes"):
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
        bounds = self._bounds_for_fish_prim(prim)
        center, radius, min_up, max_up, up_axis, radial_axes = bounds
        angle = index * math.tau / max(1, 3)
        direction_values = [0.0, 0.0, 0.0]
        direction_values[radial_axes[0]] = -math.cos(angle)
        direction_values[radial_axes[1]] = -math.sin(angle)
        direction_values[up_axis] = 0.08 * math.sin(index + 1)
        initial_direction = _normalized(Gf.Vec3d(*direction_values))
        state = {
            "root_prim": prim,
            "prim": animation_prim,
            "position": self._clamp_position(position, initial_direction, bounds=bounds),
            "direction": initial_direction,
            "water_center": center,
            "water_radius": radius,
            "water_min_up": min_up,
            "water_max_up": max_up,
            "water_up_axis": up_axis,
            "water_radial_axes": list(radial_axes),
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

            water_height = max(1e-6, max_up - min_up)
            state["preferred_up"] = min_up + water_height * state["depth_band_center_norm"]
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
        separation_radius = self._water_radius * float(get_global_config("FISH_SEPARATION_RADIUS_RATIO", 0.18))
        positions = np.asarray(
            [[float(fish["position"][0]), float(fish["position"][1]), float(fish["position"][2])] for fish in self._fish],
            dtype=np.float64,
        )
        directions = np.asarray(
            [[float(fish["direction"][0]), float(fish["direction"][1]), float(fish["direction"][2])] for fish in self._fish],
            dtype=np.float64,
        )
        separation_arr, alignment_arr, cohesion_arr, neighbor_counts = fish_dynamics.compute_flock_vectors(
            positions,
            directions,
            separation_radius,
        )
        flock_cache = {
            "separation": separation_arr,
            "alignment": alignment_arr,
            "cohesion_center": cohesion_arr,
            "neighbor_counts": neighbor_counts,
        }

        for index, fish in enumerate(self._fish):
            desired = self._desired_direction(fish, now, realism_on, flock_cache, index)
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
            fish["position"] = self._clamp_position(next_position, fish["direction"], fish["head_length"], fish=fish)
            _set_fish_transform(
                fish["prim"],
                fish["position"],
                fish["direction"],
                fish=fish,
                dt=dt,
                realism_on=realism_on,
            )

    def _desired_direction(self, fish, now, realism_on=True, flock_cache=None, index=0):
        position = fish["position"]
        direction = fish["direction"]

        flock = Gf.Vec3d(0.0, 0.0, 0.0)
        if flock_cache is not None:
            neighbor_count = int(flock_cache["neighbor_counts"][index])
            if neighbor_count:
                cohesion_center = Gf.Vec3d(*((flock_cache["cohesion_center"][index] / neighbor_count).tolist()))
                alignment_vec = Gf.Vec3d(*((flock_cache["alignment"][index] / neighbor_count).tolist()))
                separation_vec = Gf.Vec3d(*(flock_cache["separation"][index].tolist()))

                cohesion = _normalized(cohesion_center - position, direction)
                alignment = _normalized(alignment_vec, direction)
                separation = _normalized(separation_vec, direction)
                flock += cohesion * float(get_global_config("FISH_COHESION_WEIGHT", 0.18))
                flock += alignment * float(get_global_config("FISH_ALIGNMENT_WEIGHT", 0.25))
                flock += separation * float(get_global_config("FISH_SEPARATION_WEIGHT", 0.42))

        wander = self._wander_vector(fish, now, realism_on) * float(get_global_config("FISH_WANDER_WEIGHT", 0.20))
        boundary = self._boundary_steering(fish) * float(get_global_config("FISH_BOUNDARY_WEIGHT", 1.35))

        depth = Gf.Vec3d(0.0, 0.0, 0.0)
        if realism_on and "preferred_up" in fish:
            up_axis = int(fish.get("water_up_axis", self._water_up_axis))
            strength = fish_dynamics.depth_attraction_strength(
                position_z=fish["position"][up_axis],
                preferred_z=fish["preferred_up"],
                band_half=fish["band_half"],
            )
            depth_values = [0.0, 0.0, 0.0]
            depth_values[up_axis] = strength
            depth = Gf.Vec3d(*depth_values) * float(get_global_config("FISH_DEPTH_BAND_WEIGHT", 0.45))

        return _normalized(direction + flock + wander + boundary + depth, direction)


    def _wander_vector(self, fish, now, realism_on=True):
        phase = fish["phase"]
        up_axis = int(fish.get("water_up_axis", self._water_up_axis))
        radial_axes = list(fish.get("water_radial_axes", self._water_radial_axes))
        horizontal_values = [0.0, 0.0, 0.0]
        horizontal_values[radial_axes[0]] = math.cos(now * 0.7 + phase)
        horizontal_values[radial_axes[1]] = math.sin(now * 0.9 + phase * 1.7)
        horizontal = Gf.Vec3d(*horizontal_values)
        if realism_on and "vertical_wander_freq_hz" in fish:
            vertical_up = math.sin(
                2.0 * math.pi * fish["vertical_wander_freq_hz"] * now + fish["vertical_wander_phase"]
            )
        else:
            vertical_up = math.sin(now * 0.55 + phase)
        vertical_values = [0.0, 0.0, 0.0]
        vertical_values[up_axis] = vertical_up
        vertical = Gf.Vec3d(*vertical_values)
        return _normalized(horizontal + vertical * float(get_global_config("FISH_VERTICAL_WANDER_WEIGHT", 0.12)))

    def _boundary_steering(self, fish):
        position = fish["position"]
        direction = fish["direction"]
        center = fish.get("water_center", self._water_center)
        min_up = float(fish.get("water_min_up", self._water_min_z))
        max_up = float(fish.get("water_max_up", self._water_max_z))
        up_axis = int(fish.get("water_up_axis", self._water_up_axis))
        radial_axes = list(fish.get("water_radial_axes", self._water_radial_axes))
        head = position + direction * fish["head_length"]
        rel_values = [0.0, 0.0, 0.0]
        for axis in radial_axes:
            rel_values[axis] = head[axis] - center[axis]
        rel = Gf.Vec3d(*rel_values)
        radial = _length(rel)
        inward = _normalized(Gf.Vec3d(-rel[0], -rel[1], -rel[2]), direction)

        safe_radius = self._safe_radius(fish["head_length"], fish=fish)
        start_radius = safe_radius * float(get_global_config("FISH_BOUNDARY_START_RATIO", 0.68))
        wall_t = _smoothstep(start_radius, safe_radius, radial)

        tangent_sign = 1.0 if math.sin(fish["phase"]) >= 0.0 else -1.0
        tangent_values = [0.0, 0.0, 0.0]
        tangent_values[radial_axes[0]] = -inward[radial_axes[1]] * tangent_sign
        tangent_values[radial_axes[1]] = inward[radial_axes[0]] * tangent_sign
        tangent = Gf.Vec3d(*tangent_values)
        smooth_turn = 0.5 - 0.5 * math.cos(math.pi * wall_t)
        steer = inward * smooth_turn + tangent * (1.0 - smooth_turn) * wall_t * 0.45

        up_mid = (min_up + max_up) * 0.5
        if head[up_axis] > max_up:
            down_values = [0.0, 0.0, 0.0]
            down_values[up_axis] = -1.0
            steer += Gf.Vec3d(*down_values) * _smoothstep(up_mid, max_up, head[up_axis])
        elif head[up_axis] < min_up:
            up_values = [0.0, 0.0, 0.0]
            up_values[up_axis] = 1.0
            steer += Gf.Vec3d(*up_values) * _smoothstep(min_up, up_mid, head[up_axis])

        return steer

    def _clamp_position(self, position, direction, head_length=0.0, fish=None, bounds=None):
        if bounds is not None:
            center, radius, min_up, max_up, up_axis, radial_axes = bounds
        elif fish is not None:
            center = fish.get("water_center", self._water_center)
            radius = float(fish.get("water_radius", self._water_radius))
            min_up = float(fish.get("water_min_up", self._water_min_z))
            max_up = float(fish.get("water_max_up", self._water_max_z))
            up_axis = int(fish.get("water_up_axis", self._water_up_axis))
            radial_axes = list(fish.get("water_radial_axes", self._water_radial_axes))
        else:
            center = self._water_center
            radius = self._water_radius
            min_up = self._water_min_z
            max_up = self._water_max_z
            up_axis = self._water_up_axis
            radial_axes = list(self._water_radial_axes)

        safe_radius = self._safe_radius(head_length, radius=radius)
        head = position + direction * head_length
        rel_values = [0.0, 0.0, 0.0]
        for axis in radial_axes:
            rel_values[axis] = head[axis] - center[axis]
        rel = Gf.Vec3d(*rel_values)
        radial = _length(rel)
        if radial > safe_radius:
            rel = _normalized(rel) * safe_radius
            head_values = [float(head[0]), float(head[1]), float(head[2])]
            for axis in radial_axes:
                head_values[axis] = float(center[axis] + rel[axis])
            head = Gf.Vec3d(*head_values)
            position = head - direction * head_length

        position_values = [float(position[0]), float(position[1]), float(position[2])]
        position_values[up_axis] = _clamp(position_values[up_axis], min_up, max_up)
        return Gf.Vec3d(*position_values)

    def _safe_radius(self, head_length, fish=None, radius=None):
        margin_ratio = float(get_global_config("FISH_BOUNDARY_MARGIN_RATIO", 0.12))
        water_radius = float(radius if radius is not None else fish.get("water_radius", self._water_radius) if fish is not None else self._water_radius)
        return max(water_radius * 0.2, water_radius * (1.0 - margin_ratio) - head_length)

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
        include_transforms = bool(get_global_config("STAGE_TOPOLOGY_INCLUDE_TRANSFORMS", True))
        include_bounds = bool(get_global_config("STAGE_TOPOLOGY_INCLUDE_BOUNDS", True))
        bbox_cache = None
        if include_bounds:
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
                useExtentsHint=True,
            )

        for prim in Usd.PrimRange.Stage(stage):
            if not prim or not prim.IsValid() or prim.GetPath().pathString == "/":
                continue

            path = prim.GetPath().pathString
            node = {
                "name": prim.GetName(),
                "path": path,
                "type_name": prim.GetTypeName(),
                "children": [],
            }
            if include_transforms:
                node.update(self._prim_transform_summary(prim))
            if bbox_cache is not None:
                node.update(self._prim_bounds_summary(bbox_cache, prim))
            nodes_by_path[path] = node

            parent_path = prim.GetPath().GetParentPath().pathString
            parent = nodes_by_path.get(parent_path)
            if parent:
                parent["children"].append(node)
            else:
                roots.append(node)

        return roots

    def _prim_transform_summary(self, prim):
        summary = {}
        try:
            xformable = UsdGeom.Xformable(prim)
        except Exception:
            return summary

        try:
            local_matrix = self._local_transform_matrix(xformable)
            if local_matrix is not None:
                summary["local_translation"] = self._translation_to_json(local_matrix.ExtractTranslation())
        except Exception:
            pass

        try:
            world_matrix = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            summary["world_translation"] = self._translation_to_json(world_matrix.ExtractTranslation())
        except Exception:
            pass

        return summary

    def _prim_bounds_summary(self, bbox_cache, prim):
        try:
            aligned = bbox_cache.ComputeWorldBound(prim).ComputeAlignedBox()
            minimum = aligned.GetMin()
            maximum = aligned.GetMax()
            if any(not math.isfinite(float(value)) for value in list(minimum) + list(maximum)):
                return {}
            center = Gf.Vec3d(
                (minimum[0] + maximum[0]) * 0.5,
                (minimum[1] + maximum[1]) * 0.5,
                (minimum[2] + maximum[2]) * 0.5,
            )
            return {
                "world_bbox_center": self._translation_to_json(center),
                "world_bbox_min": self._translation_to_json(minimum),
                "world_bbox_max": self._translation_to_json(maximum),
            }
        except Exception:
            return {}


    def _local_transform_matrix(self, xformable):
        for getter in (
            lambda: xformable.GetLocalTransformation(Usd.TimeCode.Default()),
            lambda: xformable.GetLocalTransformation(),
        ):
            try:
                result = getter()
            except TypeError:
                continue
            if isinstance(result, tuple):
                return result[0]
            return result
        return None

    def _translation_to_json(self, translation):
        precision = int(get_global_config("STAGE_TOPOLOGY_TRANSFORM_PRECISION", 6))
        return [round(float(translation[index]), precision) for index in range(3)]

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
        self._particle_sets = []
        self._particle_color_attr = None
        self._particle_display_color_attr = None
        self._particle_display_color_attrs = []
        self._particle_proto_indices_attr = None
        self._particle_prototype_color_attrs = []
        self._particle_color_palette = []
        self._particle_temperature_attr = None
        self._particle_heat_weights = []
        self._particle_positions = []
        self._particle_temperatures = []
        self._particle_water_radius = None
        self._particle_water_height = None
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
        self._particle_sets = []
        self._particle_color_attr = None
        self._particle_display_color_attr = None
        self._particle_display_color_attrs = []
        self._particle_proto_indices_attr = None
        self._particle_prototype_color_attrs = []
        self._particle_color_palette = []
        self._particle_temperature_attr = None
        self._particle_heat_weights = []
        self._particle_positions = []
        self._particle_temperatures = []
        self._particle_water_radius = None
        self._particle_water_height = None
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
            self._particle_sets = []
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
        water_prims = self._find_water_prims(stage)
        if not water_prims:
            self._warn_missing_water_once()
            self._water_prim = None
            self._particles_prim = None
            self._particle_sets = []
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            self._particle_positions = []
            self._particle_heat_weights = []
            return

        self._warned_missing_water = False
        self._water_prim = water_prims[0]
        if (
            self._particle_sets
            and len(self._particle_sets) == len(water_prims)
            and all(item.get("particles_prim") and item["particles_prim"].IsValid() for item in self._particle_sets)
        ):
            return

        particle_sets = []
        try:
            for water_prim in water_prims:
                self._author_temperature_particles(stage, water_prim)
                particle_sets.append(self._capture_particle_set(water_prim))
            self._particle_sets = particle_sets
            self._particle_positions = [pos for item in particle_sets for pos in item.get("positions", [])]
            self._particle_heat_weights = [weight for item in particle_sets for weight in item.get("heat_weights", [])]
            self._particle_temperatures = [temp for item in particle_sets for temp in item.get("temperatures", [])]
        except Exception as exc:
            carb.log_warn(f"[Aquacast Temp] Failed to author temperature particles: {exc}")
            self._particles_prim = None
            self._particle_sets = []
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            self._particle_positions = []
            self._particle_heat_weights = []

    def _capture_particle_set(self, water_prim):
        return {
            "water_prim": water_prim,
            "particles_prim": self._particles_prim,
            "color_attr": self._particle_color_attr,
            "display_color_attr": self._particle_display_color_attr,
            "display_color_attrs": list(self._particle_display_color_attrs or []),
            "proto_indices_attr": self._particle_proto_indices_attr,
            "prototype_color_attrs": list(self._particle_prototype_color_attrs or []),
            "color_palette": list(self._particle_color_palette or []),
            "temperature_attr": self._particle_temperature_attr,
            "positions": list(self._particle_positions or []),
            "heat_weights": list(self._particle_heat_weights or []),
            "temperatures": list(self._particle_temperatures or []),
            "water_radius": self._particle_water_radius,
            "water_height": self._particle_water_height,
            "last_update_time": self._last_particle_update_time,
            "elapsed": self._particle_elapsed,
        }

    def _restore_particle_set(self, particle_set):
        self._particles_prim = particle_set.get("particles_prim")
        self._particle_color_attr = particle_set.get("color_attr")
        self._particle_display_color_attr = particle_set.get("display_color_attr")
        self._particle_display_color_attrs = list(particle_set.get("display_color_attrs", []) or [])
        self._particle_proto_indices_attr = particle_set.get("proto_indices_attr")
        self._particle_prototype_color_attrs = list(particle_set.get("prototype_color_attrs", []) or [])
        self._particle_color_palette = list(particle_set.get("color_palette", []) or [])
        self._particle_temperature_attr = particle_set.get("temperature_attr")
        self._particle_positions = list(particle_set.get("positions", []) or [])
        self._particle_heat_weights = list(particle_set.get("heat_weights", []) or [])
        self._particle_temperatures = list(particle_set.get("temperatures", []) or [])
        self._particle_water_radius = particle_set.get("water_radius")
        self._particle_water_height = particle_set.get("water_height")
        self._last_particle_update_time = float(particle_set.get("last_update_time", 0.0) or 0.0)
        self._particle_elapsed = float(particle_set.get("elapsed", 0.0) or 0.0)

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

    def _find_water_prims(self, stage):
        configured = str(get_global_config("WATER_PRIM_PATH", "") or "").strip()
        if configured:
            prim = stage.GetPrimAtPath(configured)
            return [prim] if prim and prim.IsValid() else []

        water_prims = []
        seen = set()
        for path in _get_topology_paths_by_name("Water"):
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                continue
            path_string = prim.GetPath().pathString
            if "/Looks/" in path_string or "/Materials/" in path_string:
                continue
            seen.add(path_string)
            water_prims.append(prim)

        for prim in stage.Traverse():
            if not prim or not prim.IsValid() or prim.GetName() != "Water":
                continue
            path_string = prim.GetPath().pathString
            if path_string in seen or "/Looks/" in path_string or "/Materials/" in path_string:
                continue
            seen.add(path_string)
            water_prims.append(prim)

        return sorted(water_prims, key=lambda prim: prim.GetPath().pathString)

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
        parent = water_prim.GetPath().GetParentPath()
        configured = str(get_global_config("TEMP_PARTICLE_PRIM_PATH", "") or "").strip()
        child_name = str(get_global_config("TEMP_PARTICLE_PRIM_NAME", "") or "").strip()
        if configured:
            configured_name = configured.rstrip("/").split("/")[-1]
            if configured_name:
                child_name = child_name or configured_name
        child_name = child_name or "TemperatureParticlesInsideWater"
        return parent.AppendChild(child_name)

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
        particle_parent_prim = stage.GetPrimAtPath(particle_path.GetParentPath())
        parent_world = Gf.Matrix4d(1.0)
        if particle_parent_prim and particle_parent_prim.IsValid():
            try:
                parent_world = UsdGeom.Xformable(particle_parent_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            except Exception:
                parent_world = Gf.Matrix4d(1.0)
        parent_world_inv = parent_world.GetInverse()
        local_positions = []
        for position in positions:
            local_position = parent_world_inv.Transform(Gf.Vec3d(position))
            local_positions.append(Gf.Vec3f(float(local_position[0]), float(local_position[1]), float(local_position[2])))
        session_layer = stage.GetSessionLayer()
        edit_target = session_layer if session_layer is not None else stage.GetRootLayer()
        cyan = Gf.Vec3f(*thermal_dynamics.temperature_to_rgb(
            self._T,
            self._sorted_stops(get_global_config("TEMP_COLOR_STOPS", [])),
        ))
        color_bins = max(2, min(256, int(get_global_config("TEMP_PARTICLE_COLOR_BINS", 64))))
        color_palette = _sample_color_stops(get_global_config("TEMP_COLOR_STOPS", []), color_bins)
        initial_colors = [cyan] * len(positions)
        initial_proto_indices = _colors_to_proto_indices(initial_colors, color_palette)
        prototype_color_attrs = []
        with Usd.EditContext(stage, edit_target):
            if stage.GetPrimAtPath(particle_path).IsValid():
                stage.RemovePrim(particle_path)
            instancer = UsdGeom.PointInstancer.Define(stage, particle_path)
            instancer.CreateVisibilityAttr(UsdGeom.Tokens.inherited)
            instancer.CreatePurposeAttr(UsdGeom.Tokens.default_)

            prototypes_path = particle_path.AppendChild("Prototypes")
            UsdGeom.Xform.Define(stage, prototypes_path)
            prototype_targets = []
            for index, color in enumerate(color_palette):
                prototype_path = prototypes_path.AppendChild(f"Color_{index:03d}")
                prototype = UsdGeom.Sphere.Define(stage, prototype_path)
                prototype.CreateRadiusAttr(width)
                prototype.CreateVisibilityAttr(UsdGeom.Tokens.inherited)
                prototype.CreatePurposeAttr(UsdGeom.Tokens.default_)
                prototype_color_attrs.append(prototype.CreateDisplayColorAttr(Vt.Vec3fArray([color])))
                prototype.CreateDisplayOpacityAttr(Vt.FloatArray([1.0]))
                prototype_targets.append(prototype_path)

            instancer.CreatePrototypesRel().SetTargets(prototype_targets)
            proto_indices_attr = instancer.CreateProtoIndicesAttr(initial_proto_indices)
            instancer.CreatePositionsAttr(Vt.Vec3fArray(local_positions))
            min_corner = [
                min(float(pos[axis]) for pos in local_positions) - width
                for axis in range(3)
            ]
            max_corner = [
                max(float(pos[axis]) for pos in local_positions) + width
                for axis in range(3)
            ]
            instancer.CreateExtentAttr(Vt.Vec3fArray([
                Gf.Vec3f(min_corner[0], min_corner[1], min_corner[2]),
                Gf.Vec3f(max_corner[0], max_corner[1], max_corner[2]),
            ]))

            primvars_api = UsdGeom.PrimvarsAPI(instancer.GetPrim())
            color_primvar = primvars_api.CreatePrimvar(
                "displayColor",
                Sdf.ValueTypeNames.Color3fArray,
                UsdGeom.Tokens.vertex,
            )
            color_primvar.Set(Vt.Vec3fArray(initial_colors))
            temperature_primvar = primvars_api.CreatePrimvar(
                "temperature",
                Sdf.ValueTypeNames.FloatArray,
                UsdGeom.Tokens.vertex,
            )
            temperature_primvar.Set(Vt.FloatArray([float(self._T)] * len(positions)))

        self._particles_prim = stage.GetPrimAtPath(particle_path)
        self._particle_color_attr = color_primvar.GetAttr()
        self._particle_display_color_attr = self._particle_color_attr
        self._particle_display_color_attrs = []
        self._particle_proto_indices_attr = proto_indices_attr
        self._particle_prototype_color_attrs = prototype_color_attrs
        self._particle_color_palette = color_palette
        self._particle_temperature_attr = temperature_primvar.GetAttr()
        self._particle_positions = positions
        self._particle_heat_weights = heat_weights
        self._particle_water_radius = float(radius)
        self._particle_water_height = float(up_max - up_min)
        self._last_particle_update_time = 0.0
        self._particle_elapsed = 0.0
        self._write_particle_samples(stage, force=True)
        carb.log_info(
            f"[Aquacast Temp] Authored {count} temperature point instances at {particle_path} "
            f"as sibling of water={water_prim.GetPath()}"
        )

    def _write_particle_proto_colors(self, colors, palette=None):
        if not colors:
            return
        prototype_color_attrs = getattr(self, "_particle_prototype_color_attrs", []) or []
        if palette and prototype_color_attrs:
            for attr, color in zip(prototype_color_attrs, palette):
                attr.Set(Vt.Vec3fArray([color]))
            self._particle_color_palette = list(palette)

        proto_indices_attr = getattr(self, "_particle_proto_indices_attr", None)
        color_palette = getattr(self, "_particle_color_palette", []) or []
        if proto_indices_attr is not None and color_palette:
            proto_indices_attr.Set(_colors_to_proto_indices(colors, color_palette))

    def _write_particle_samples(self, stage, force=False):
        particle_sets = getattr(self, "_particle_sets", []) or []
        if particle_sets and not getattr(self, "_writing_particle_set", False):
            aggregate_positions = []
            aggregate_weights = []
            aggregate_temperatures = []
            for particle_set in particle_sets:
                self._restore_particle_set(particle_set)
                self._writing_particle_set = True
                try:
                    self._write_particle_samples(stage, force=force)
                finally:
                    self._writing_particle_set = False
                particle_set.update(self._capture_particle_set(particle_set.get("water_prim")))
                aggregate_positions.extend(particle_set.get("positions", []) or [])
                aggregate_weights.extend(particle_set.get("heat_weights", []) or [])
                aggregate_temperatures.extend(particle_set.get("temperatures", []) or [])
            if particle_sets:
                self._restore_particle_set(particle_sets[0])
                self._particle_sets = particle_sets
                self._particle_positions = aggregate_positions
                self._particle_heat_weights = aggregate_weights
                self._particle_temperatures = aggregate_temperatures
            return

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
        temp_palette = _sample_color_stops(stops, len(getattr(self, "_particle_color_palette", []) or []) or 64)
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
                            self._write_particle_proto_colors(colors, temp_palette)
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
                        self._write_particle_proto_colors(colors, temp_palette)
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

        del dt
        t_room = float(get_global_config("ROOM_TEMP_C", 22.0))
        t_inlet = float(get_global_config("INLET_WATER_TEMP_C", 14.0))
        k_room = float(get_global_config("THERMAL_K_ROOM", 0.012))
        k_inflow = float(get_global_config("THERMAL_K_INFLOW", 0.022))
        quality_controller = globals().get("_water_quality_controller")
        if quality_controller is not None and hasattr(quality_controller, "snapshot"):
            try:
                snapshot = quality_controller.snapshot()
                self._T = float(snapshot.get("temperature_c", self._T))
            except Exception:
                pass

        stops = self._sorted_stops(get_global_config("TEMP_COLOR_STOPS", []))
        stage = omni.usd.get_context().get_stage()
        if bool(get_global_config("ENABLE_PARTICLE_SYSTEM_TEMP_COLOR", False)) and stops and stage is not None:
            r, g, b = thermal_dynamics.temperature_to_rgb(self._T, stops)
            self._write_color(stage, r, g, b)
        if stage is not None and not bool(get_global_config("ENABLE_WATER_QUALITY", get_global_config("ENABLE_WATER_QUALITY_SIM", False))):
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
            self._particle_sets = []
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            self._particle_positions = []
            self._particle_heat_weights = []
            self._particle_temperatures = []
            self._particle_water_radius = None
            self._particle_water_height = None
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
            self._particle_sets = []
            self._particle_color_attr = None
            self._particle_display_color_attr = None
            self._particle_display_color_attrs = []
            self._particle_temperature_attr = None
            self._particle_positions = []
            self._particle_heat_weights = []
            self._particle_water_radius = None
            self._particle_water_height = None
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
        self._particle_register_signature = None
        self._warned_geometry_mismatch = False

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
        self._particle_register_signature = None
        self._warned_geometry_mismatch = False
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

        stage = omni.usd.get_context().get_stage()
        temp_controller = globals().get("_water_temp_controller")
        if stage is not None and temp_controller is not None:
            self._ensure_particles_registered(temp_controller)

        state = self._model.advance(dt)

        if temp_controller is not None:
            try:
                temp_controller._T = float(getattr(state, "temperature_c", temp_controller._T))
            except Exception:
                pass
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

    def _ensure_particles_registered(self, temp_controller):
        if self._model is None or not hasattr(self._model, "register_particles"):
            return
        positions = getattr(temp_controller, "_particle_positions", None) or []
        if not positions:
            return
        heat_weights = getattr(temp_controller, "_particle_heat_weights", None) or []
        signature = (
            len(positions),
            tuple(round(float(value), 4) for value in positions[0]),
            tuple(round(float(value), 4) for value in positions[-1]),
        )
        if self._particle_register_signature == signature:
            return
        try:
            result = self._model.register_particles(positions, heat_weights)
            self._particle_register_signature = signature
            carb.log_info(
                f"[Aquacast WQ] Registered backend temperature particles "
                f"count={result.get('count', len(positions))} graph={result.get('graph_hash', '')}"
            )
            self._warn_if_geometry_mismatch(temp_controller)
        except Exception as exc:
            carb.log_warn(f"[Aquacast WQ] Failed to register backend particles: {exc}")

    def _warn_if_geometry_mismatch(self, temp_controller):
        if self._warned_geometry_mismatch or self._model is None:
            return
        try:
            snap = self._model.snapshot()
            cfg_radius = float(snap.get("tank_radius_m", 0.0))
            cfg_height = float(snap.get("tank_water_height_m", 0.0))
            usd_radius = float(getattr(temp_controller, "_particle_water_radius", 0.0) or 0.0)
            usd_height = float(getattr(temp_controller, "_particle_water_height", 0.0) or 0.0)
            if cfg_radius <= 0.0 or cfg_height <= 0.0 or usd_radius <= 0.0 or usd_height <= 0.0:
                return
            radius_err = abs(usd_radius - cfg_radius) / cfg_radius
            height_err = abs(usd_height - cfg_height) / cfg_height
            if max(radius_err, height_err) > float(get_global_config("WQ_GEOMETRY_WARN_REL_TOL", 0.15)):
                carb.log_warn(
                    "[Aquacast Temp] USD water bounds differ from backend thermal geometry: "
                    f"usd_radius={usd_radius:.3f}m cfg_radius={cfg_radius:.3f}m, "
                    f"usd_height={usd_height:.3f}m cfg_height={cfg_height:.3f}m"
                )
                self._warned_geometry_mismatch = True
        except Exception:
            return

    def _write_particle_primvars(self, stage, now):
        update_interval = float(get_global_config("WQ_PARTICLE_UPDATE_INTERVAL_SECONDS", 1.0))
        if (
            update_interval > 0.0
            and now - self._last_particle_write_time < update_interval
            and not getattr(self, "_writing_particle_set", False)
        ):
            return
        field_interval = float(get_global_config("WQ_PARTICLE_FIELD_UPDATE_INTERVAL_SECONDS", 0.5))
        write_all_fields = (
            getattr(self, "_writing_particle_set", False)
            or now - self._last_particle_field_write_time >= max(0.0, field_interval)
        )

        temp_controller = globals().get("_water_temp_controller")
        if temp_controller is None:
            return

        particle_sets = getattr(temp_controller, "_particle_sets", []) or []
        if particle_sets and not getattr(self, "_writing_particle_set", False):
            restore_particle_set = getattr(temp_controller, "_restore_particle_set", None)
            capture_particle_set = getattr(temp_controller, "_capture_particle_set", None)
            aggregate_positions = []
            aggregate_weights = []
            aggregate_temperatures = []
            for particle_set in particle_sets:
                if callable(restore_particle_set):
                    restore_particle_set(particle_set)
                self._writing_particle_set = True
                self._particle_primvars = {}
                self._display_color_attr = None
                self._sphere_color_attrs = []
                try:
                    self._write_particle_primvars(stage, now)
                finally:
                    self._writing_particle_set = False
                if callable(capture_particle_set):
                    particle_set.update(capture_particle_set(particle_set.get("water_prim")))
                aggregate_positions.extend(particle_set.get("positions", []) or [])
                aggregate_weights.extend(particle_set.get("heat_weights", []) or [])
                aggregate_temperatures.extend(particle_set.get("temperatures", []) or [])
            if particle_sets and callable(restore_particle_set):
                restore_particle_set(particle_sets[0])
            temp_controller._particle_sets = particle_sets
            temp_controller._particle_positions = aggregate_positions
            temp_controller._particle_heat_weights = aggregate_weights
            temp_controller._particle_temperatures = aggregate_temperatures
            self._last_particle_write_time = now
            if write_all_fields:
                self._last_particle_field_write_time = now
            return

        heat_weights = getattr(temp_controller, "_particle_heat_weights", None) or []
        positions = getattr(temp_controller, "_particle_positions", None) or []
        if not heat_weights:
            return

        particles_prim = getattr(temp_controller, "_particles_prim", None)
        prim = particles_prim if particles_prim and particles_prim.IsValid() else None
        if not prim or not prim.IsValid():
            return

        self._ensure_particles_registered(temp_controller)
        if not self._particle_primvars and not self._sphere_color_attrs and self._display_color_attr is None:
            self._bind_particle_primvars(stage, prim)
        if not self._particle_primvars and not self._sphere_color_attrs and self._display_color_attr is None:
            return

        if not getattr(self, "_writing_particle_set", False) and hasattr(self._model, "registered_particle_values"):
            values = self._model.registered_particle_values()
        else:
            values = self._model.particle_values(heat_weights, positions)
        temperature_values = values.get("temperature") or []
        if temperature_values:
            temp_controller._particle_temperatures = [float(value) for value in temperature_values]
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
                        color_bins = len(getattr(temp_controller, "_particle_color_palette", []) or [])
                        if color_bins <= 0:
                            color_bins = max(2, min(256, int(get_global_config("TEMP_PARTICLE_COLOR_BINS", 64))))
                        palette = _sample_color_stops(self._color_stops_for_view(self._view_variable()), color_bins)
                        write_proto_colors = getattr(temp_controller, "_write_particle_proto_colors", None)
                        if callable(write_proto_colors):
                            write_proto_colors(display_colors, palette)
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
