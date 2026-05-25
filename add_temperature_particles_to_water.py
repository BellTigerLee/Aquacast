#!/usr/bin/env python3
"""Add animated temperature sample particles inside an existing USD water cylinder."""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

np = None


NUM_PARTICLES = 2000
RANDOM_SEED = 42

T_COLD = 20.0
T_HOT = 90.0
FRAMES = 180
FPS = 30
STEPS_PER_FRAME = 4
K_NEIGHBORS = 12
DIFFUSION_RATE = 0.08
KIT_BOOTSTRAP_ENV = "AQUACAST_TEMPERATURE_PARTICLES_KIT_BOOTSTRAPPED"

Usd = None
UsdGeom = None
Sdf = None
Gf = None
Vt = None


@dataclass
class PrimCandidate:
    path: str
    name: str
    type_name: str
    score: int


@dataclass
class CylinderGeometry:
    space: str
    radius: float
    height: float
    axis: str
    center: np.ndarray

    @property
    def radius_eff(self) -> float:
        return self.radius * 0.96

    @property
    def height_eff(self) -> float:
        return self.height * 0.96


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add fixed animated temperature particles inside an existing USD water cylinder."
    )
    parser.add_argument("--usd", required=True, help="Existing USD/USDa/USDc scene file.")
    parser.add_argument("--topology", required=True, help="JSON file describing the stage topology.")
    parser.add_argument("--output", required=True, help="Output USD file to write.")
    parser.add_argument("--water-name", default="water", help="Target prim name. Default: water.")
    parser.add_argument("--water-path", default=None, help="Exact water prim path. Overrides topology search.")
    parser.add_argument(
        "--heating-mode",
        choices=("side", "bottom", "internal"),
        default="side",
        help="Temperature source placement.",
    )
    parser.add_argument(
        "--render-mode",
        choices=("points", "instancer"),
        default="points",
        help="Use UsdGeomPoints displayColor or PointInstancer color-bin fallback.",
    )
    parser.add_argument(
        "--allow-overwrite-input",
        action="store_true",
        help="Allow --output to be the same path as --usd.",
    )
    parser.add_argument(
        "--kit-app-template",
        default=os.environ.get("KIT_APP_TEMPLATE"),
        help=(
            "Kit app template root used to bootstrap pxr/numpy when running from a regular Python. "
            "Defaults to KIT_APP_TEMPLATE, ../kit-app-template, or ~/cs-project/kit-app-template."
        ),
    )
    return parser.parse_args()


def _can_import_runtime_modules() -> tuple[bool, str | None]:
    try:
        import numpy  # noqa: F401
        from pxr import Gf as _Gf  # noqa: F401
        from pxr import Sdf as _Sdf  # noqa: F401
        from pxr import Usd as _Usd  # noqa: F401
        from pxr import UsdGeom as _UsdGeom  # noqa: F401
        from pxr import Vt as _Vt  # noqa: F401
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


def _candidate_kit_roots(explicit_root: str | None) -> list[Path]:
    roots: list[Path] = []
    if explicit_root:
        roots.append(Path(explicit_root).expanduser())

    script_dir = Path(__file__).resolve().parent
    roots.extend(
        [
            script_dir.parent / "kit-app-template",
            Path.cwd().parent / "kit-app-template",
            Path.home() / "cs-project" / "kit-app-template",
            Path("/home/netai-sys/cs-project/kit-app-template"),
        ]
    )

    unique_roots: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve() if root.exists() else root
        if resolved not in seen:
            seen.add(resolved)
            unique_roots.append(root)
    return unique_roots


def _path_prepend(env: dict[str, str], key: str, values: list[Path]) -> None:
    parts = [str(value) for value in values if value.exists()]
    if not parts:
        return
    current = env.get(key)
    env[key] = os.pathsep.join(parts + ([current] if current else []))


def _capture_kit_env(setup_script: Path) -> dict[str, str]:
    command = f"source {shlex.quote(str(setup_script))} >/dev/null 2>&1; env -0"
    result = subprocess.run(
        ["bash", "-lc", command],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    env: dict[str, str] = {}
    for item in result.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode()] = value.decode()
    return env


def _kit_usd_lib_dirs(release_dir: Path) -> list[Path]:
    dirs: list[Path] = [release_dir]
    for extension_dir in sorted((release_dir / "extscache").glob("omni.usd.libs-*")):
        bin_dir = extension_dir / "bin"
        if bin_dir.exists():
            dirs.append(bin_dir.resolve())
    return dirs


def _bootstrap_kit_environment(explicit_root: str | None) -> None:
    if os.environ.get(KIT_BOOTSTRAP_ENV) == "1":
        return

    can_import, import_error = _can_import_runtime_modules()
    if can_import:
        return

    attempted: list[str] = []
    for root in _candidate_kit_roots(explicit_root):
        release_dir = root / "_build" / "linux-x86_64" / "release"
        setup_script = release_dir / "setup_python_env.sh"
        kit_python = release_dir / "kit" / "python" / "bin" / "python3"

        if not setup_script.exists() or not kit_python.exists():
            attempted.append(f"{root}: missing setup_python_env.sh or Kit python")
            continue

        try:
            env = _capture_kit_env(setup_script)
        except subprocess.CalledProcessError as exc:
            attempted.append(f"{root}: setup_python_env.sh failed: {exc.stderr.decode(errors='replace')}")
            continue

        _path_prepend(env, "LD_LIBRARY_PATH", _kit_usd_lib_dirs(release_dir))
        env[KIT_BOOTSTRAP_ENV] = "1"
        env["KIT_APP_TEMPLATE"] = str(root)

        check = subprocess.run(
            [
                str(kit_python),
                "-c",
                "import numpy; from pxr import Gf, Sdf, Usd, UsdGeom, Vt; print('ok')",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if check.returncode != 0:
            attempted.append(f"{root}: import check failed: {check.stderr.strip()}")
            continue

        print(f"Bootstrapping Kit Python environment: {root}", file=sys.stderr)
        os.execve(
            str(kit_python),
            [str(kit_python), str(Path(__file__).resolve()), *sys.argv[1:]],
            env,
        )

    details = "\n  ".join(attempted) if attempted else "no kit-app-template candidates found"
    raise RuntimeError(
        "Could not import required runtime modules and Kit bootstrap failed.\n"
        f"Original import error: {import_error}\n"
        f"Attempted:\n  {details}\n"
        "Pass --kit-app-template /path/to/kit-app-template if it is installed elsewhere."
    )


def import_numpy() -> None:
    global np

    try:
        import numpy as _np
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required for particle generation and heat diffusion. "
            f"Original import error: {exc}"
        ) from exc

    np = _np


def import_usd_modules() -> None:
    global Usd, UsdGeom, Sdf, Gf, Vt

    try:
        from pxr import Gf as _Gf
        from pxr import Sdf as _Sdf
        from pxr import Usd as _Usd
        from pxr import UsdGeom as _UsdGeom
        from pxr import Vt as _Vt
    except ImportError as exc:
        raise RuntimeError(
            "The pxr USD Python bindings are required. Run this in an Omniverse/USD Python environment. "
            f"Original import error: {exc}"
        ) from exc

    Usd = _Usd
    UsdGeom = _UsdGeom
    Sdf = _Sdf
    Gf = _Gf
    Vt = _Vt


def load_topology_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Topology root must be a JSON object: {path}")
    return data


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _final_path_component(path: str) -> str:
    stripped = path.rstrip("/")
    if not stripped:
        return ""
    return stripped.rsplit("/", 1)[-1]


def find_prim_candidates(topology_json: dict[str, Any], target_name: str) -> list[PrimCandidate]:
    target_lower = target_name.lower()
    candidates: dict[str, PrimCandidate] = {}

    path_keys = ("path", "prim_path", "primPath", "usdPath", "usd_path")
    name_keys = ("name", "prim_name", "primName")
    type_keys = ("typeName", "type_name", "type", "prim_type", "primType")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            path = _first_string(value, path_keys)
            name = _first_string(value, name_keys)
            type_name = _first_string(value, type_keys) or ""

            if path:
                final_name = _final_path_component(path)
                effective_name = name or final_name
                path_lower = path.lower()
                name_lower = effective_name.lower()
                final_lower = final_name.lower()

                is_candidate = (
                    name_lower == target_lower
                    or final_lower == target_lower
                    or f"/{target_lower}" in path_lower
                    or target_lower in path_lower
                )

                if is_candidate:
                    score = 0
                    if name_lower == target_lower:
                        score += 100
                    if final_lower == target_lower:
                        score += 100
                    if target_lower in path_lower:
                        score += 10
                    if "/group/" in path_lower:
                        score += 20
                    if any(token in path_lower for token in ("/looks/", "/materials/", "/shader")):
                        score -= 80
                    if any(token in type_name.lower() for token in ("cylinder", "mesh", "xform")):
                        score += 25

                    previous = candidates.get(path)
                    candidate = PrimCandidate(path, effective_name, type_name, score)
                    if previous is None or candidate.score > previous.score:
                        candidates[path] = candidate

            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(topology_json)
    return sorted(candidates.values(), key=lambda item: (-item.score, item.path))


def find_prim_path_in_topology(topology_json: dict[str, Any], target_name: str) -> str | None:
    candidates = find_prim_candidates(topology_json, target_name)
    if not candidates:
        return None
    return candidates[0].path


def select_water_path(
    candidates: list[PrimCandidate],
    explicit_water_path: str | None = None,
) -> str | None:
    if explicit_water_path:
        return explicit_water_path

    if not candidates:
        return None

    if len(candidates) > 1:
        print("Multiple water prim candidates found:")
        for candidate in candidates:
            type_note = f", type={candidate.type_name}" if candidate.type_name else ""
            print(f"  score={candidate.score:3d} path={candidate.path}{type_note}")

    return candidates[0].path


def normalize_axis(axis: Any) -> str:
    value = str(axis or "Z").upper()
    if value.endswith(".X") or value == "X":
        return "X"
    if value.endswith(".Y") or value == "Y":
        return "Y"
    return "Z"


def infer_water_cylinder_geometry(stage: Any, water_prim: Any) -> CylinderGeometry:
    type_name = str(water_prim.GetTypeName())
    is_cylinder = type_name == "Cylinder"
    if not is_cylinder:
        try:
            is_cylinder = bool(water_prim.IsA(UsdGeom.Cylinder))
        except TypeError:
            is_cylinder = False

    if is_cylinder:
        cylinder = UsdGeom.Cylinder(water_prim)
        radius = cylinder.GetRadiusAttr().Get()
        height = cylinder.GetHeightAttr().Get()
        axis = cylinder.GetAxisAttr().Get()

        radius = float(radius if radius is not None else 1.0)
        height = float(height if height is not None else 2.0)
        axis = normalize_axis(axis)

        if radius <= 0.0 or height <= 0.0:
            raise ValueError(f"Invalid Cylinder dimensions: radius={radius}, height={height}")

        return CylinderGeometry(
            space="local",
            radius=radius,
            height=height,
            axis=axis,
            center=np.zeros(3, dtype=np.float64),
        )

    bbox_cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
        True,
    )
    world_bound = bbox_cache.ComputeWorldBound(water_prim)
    world_range = world_bound.ComputeAlignedRange()
    minimum = np.array(world_range.GetMin(), dtype=np.float64)
    maximum = np.array(world_range.GetMax(), dtype=np.float64)
    size = maximum - minimum
    center = 0.5 * (minimum + maximum)

    radius = 0.5 * float(min(size[0], size[1]))
    height = float(size[2])

    if not np.all(np.isfinite(size)) or radius <= 0.0 or height <= 0.0:
        raise ValueError(
            f"Could not infer valid water bounds from {water_prim.GetPath()}: "
            f"min={minimum.tolist()}, max={maximum.tolist()}"
        )

    return CylinderGeometry(
        space="world",
        radius=radius,
        height=height,
        axis="Z",
        center=center,
    )


def generate_cylinder_particles_local(
    radius: float,
    height: float,
    axis: str,
    count: int = NUM_PARTICLES,
    seed: int = RANDOM_SEED,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    radius_eff = radius * 0.96
    height_eff = height * 0.96

    radial = radius_eff * np.sqrt(rng.random(count))
    theta = 2.0 * math.pi * rng.random(count)
    axial = rng.uniform(-0.5 * height_eff, 0.5 * height_eff, size=count)
    a = radial * np.cos(theta)
    b = radial * np.sin(theta)

    positions = np.zeros((count, 3), dtype=np.float32)
    axis = normalize_axis(axis)
    if axis == "X":
        positions[:, 0] = axial
        positions[:, 1] = a
        positions[:, 2] = b
    elif axis == "Y":
        positions[:, 0] = a
        positions[:, 1] = axial
        positions[:, 2] = b
    else:
        positions[:, 0] = a
        positions[:, 1] = b
        positions[:, 2] = axial

    return positions


def generate_cylinder_particles_world_fallback(
    center_world: np.ndarray,
    radius: float,
    height: float,
    count: int = NUM_PARTICLES,
    seed: int = RANDOM_SEED,
) -> np.ndarray:
    positions = generate_cylinder_particles_local(radius, height, "Z", count=count, seed=seed)
    positions = positions.astype(np.float64)
    positions += np.asarray(center_world, dtype=np.float64)
    return positions.astype(np.float32)


def _axis_and_radial(positions: np.ndarray, geometry: CylinderGeometry) -> tuple[np.ndarray, np.ndarray]:
    axis_index = {"X": 0, "Y": 1, "Z": 2}[geometry.axis]
    relative = positions.astype(np.float64) - geometry.center.reshape(1, 3)
    axis_coord = relative[:, axis_index]
    radial_axes = [index for index in range(3) if index != axis_index]
    radial = np.linalg.norm(relative[:, radial_axes], axis=1)
    return axis_coord, radial


def validate_particles_inside_local_cylinder(positions: np.ndarray, geometry: CylinderGeometry) -> None:
    axis_coord, radial = _axis_and_radial(positions, geometry)
    half_height = 0.5 * geometry.height_eff
    assert np.all(radial <= geometry.radius_eff + 1e-6)
    assert np.all(axis_coord >= -half_height - 1e-6)
    assert np.all(axis_coord <= half_height + 1e-6)


def compute_heater_mask(
    positions: np.ndarray,
    geometry: CylinderGeometry,
    heating_mode: str,
) -> np.ndarray:
    axis_coord, radial = _axis_and_radial(positions, geometry)
    relative = positions.astype(np.float64) - geometry.center.reshape(1, 3)

    if heating_mode == "side":
        mask = radial >= 0.90 * geometry.radius_eff
        if not np.any(mask):
            mask[np.argsort(radial)[-20:]] = True
        return mask

    if heating_mode == "bottom":
        bottom = -0.5 * geometry.height_eff
        mask = axis_coord <= bottom + 0.08 * geometry.height_eff
        if not np.any(mask):
            mask[np.argsort(axis_coord)[:20]] = True
        return mask

    if heating_mode == "internal":
        distance = np.linalg.norm(relative, axis=1)
        mask = distance <= 0.15 * geometry.radius_eff
        if not np.any(mask):
            mask[np.argsort(distance)[:20]] = True
        return mask

    raise ValueError(f"Unsupported heating mode: {heating_mode}")


def build_neighbor_graph(positions_for_distance: np.ndarray, k_neighbors: int = K_NEIGHBORS) -> np.ndarray:
    try:
        from scipy.spatial import cKDTree

        tree = cKDTree(positions_for_distance)
        _distances, indices = tree.query(positions_for_distance, k=k_neighbors + 1)
        return indices[:, 1:].astype(np.int32)
    except ImportError:
        positions = positions_for_distance.astype(np.float64)
        diff = positions[:, None, :] - positions[None, :, :]
        distance_sq = np.einsum("ijk,ijk->ij", diff, diff)
        np.fill_diagonal(distance_sq, np.inf)
        indices = np.argpartition(distance_sq, kth=k_neighbors, axis=1)[:, :k_neighbors]
        row = np.arange(positions.shape[0])[:, None]
        order = np.argsort(distance_sq[row, indices], axis=1)
        return np.take_along_axis(indices, order, axis=1).astype(np.int32)


def diffuse_temperature(T: np.ndarray, neighbors: np.ndarray, heater_mask: np.ndarray) -> np.ndarray:
    T_new = T.copy()
    neighbor_mean = T[neighbors].mean(axis=1)
    T_new += DIFFUSION_RATE * (neighbor_mean - T)
    T_new = np.clip(T_new, T_COLD, T_HOT)
    T_new[heater_mask] = T_HOT
    return T_new


def temperature_to_rgb(T: np.ndarray) -> np.ndarray:
    stops_t = np.array([20.0, 35.0, 50.0, 70.0, 90.0], dtype=np.float32)
    stops_rgb = np.array(
        [
            [0.0, 0.1, 1.0],
            [0.0, 0.9, 1.0],
            [0.0, 0.9, 0.2],
            [1.0, 0.9, 0.0],
            [1.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )

    colors = np.empty((T.shape[0], 3), dtype=np.float32)
    for channel in range(3):
        colors[:, channel] = np.interp(T, stops_t, stops_rgb[:, channel])

    assert colors.shape == (NUM_PARTICLES, 3)
    assert np.all(colors >= 0.0) and np.all(colors <= 1.0)
    return colors


def _vec3f_array(values: np.ndarray) -> Any:
    return Vt.Vec3fArray(
        [Gf.Vec3f(float(row[0]), float(row[1]), float(row[2])) for row in values]
    )


def _float_array(values: np.ndarray) -> Any:
    return Vt.FloatArray([float(value) for value in values])


def _int_array(values: np.ndarray) -> Any:
    return Vt.IntArray([int(value) for value in values])


def _create_vertex_primvar(prim: Any, name: str, type_name: Any) -> Any:
    primvars_api = UsdGeom.PrimvarsAPI(prim)
    primvar = primvars_api.CreatePrimvar(name, type_name, UsdGeom.Tokens.vertex)
    return primvar.GetAttr()


def _temperature_bin_indices(T: np.ndarray, bin_count: int) -> np.ndarray:
    normalized = np.clip((T - T_COLD) / (T_HOT - T_COLD), 0.0, 1.0)
    return np.rint(normalized * (bin_count - 1)).astype(np.int32)


def _temperature_bin_colors(bin_count: int) -> np.ndarray:
    values = np.linspace(T_COLD, T_HOT, bin_count, dtype=np.float32)
    return temperature_to_rgb(values) if bin_count == NUM_PARTICLES else _temperature_to_rgb_any(values)


def _temperature_to_rgb_any(T: np.ndarray) -> np.ndarray:
    stops_t = np.array([20.0, 35.0, 50.0, 70.0, 90.0], dtype=np.float32)
    stops_rgb = np.array(
        [
            [0.0, 0.1, 1.0],
            [0.0, 0.9, 1.0],
            [0.0, 0.9, 0.2],
            [1.0, 0.9, 0.0],
            [1.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    colors = np.empty((T.shape[0], 3), dtype=np.float32)
    for channel in range(3):
        colors[:, channel] = np.interp(T, stops_t, stops_rgb[:, channel])
    return colors


def _initial_temperature(heater_mask: np.ndarray) -> np.ndarray:
    T = np.full(NUM_PARTICLES, T_COLD, dtype=np.float32)
    T[heater_mask] = T_HOT
    return T


def _advance_temperature_samples(
    color_attr: Any,
    temperature_attr: Any,
    neighbors: np.ndarray,
    heater_mask: np.ndarray,
    proto_indices_attr: Any | None = None,
    proto_bin_count: int = 16,
) -> None:
    T = _initial_temperature(heater_mask)

    for frame in range(FRAMES):
        if frame > 0:
            for _step in range(STEPS_PER_FRAME):
                T = diffuse_temperature(T, neighbors, heater_mask)

        assert np.all(np.isfinite(T))
        assert np.all((T >= T_COLD) & (T <= T_HOT))

        colors = temperature_to_rgb(T)
        if color_attr is not None:
            color_attr.Set(_vec3f_array(colors), time=Usd.TimeCode(frame))
        if temperature_attr is not None:
            temperature_attr.Set(_float_array(T), time=Usd.TimeCode(frame))
        if proto_indices_attr is not None:
            proto_indices_attr.Set(
                _int_array(_temperature_bin_indices(T, proto_bin_count)),
                time=Usd.TimeCode(frame),
            )

        if frame == 0 or frame == FRAMES - 1 or frame % 30 == 0:
            print(f"Simulated frame {frame}/{FRAMES - 1}")


def author_temperature_points(
    stage: Any,
    temperature_particles_path: str,
    positions: np.ndarray,
    particle_width: float,
    neighbors: np.ndarray,
    heater_mask: np.ndarray,
    render_mode: str,
) -> None:
    if render_mode == "instancer":
        _author_temperature_instancer(
            stage,
            temperature_particles_path,
            positions,
            particle_width,
            neighbors,
            heater_mask,
        )
        return

    points = UsdGeom.Points.Define(stage, Sdf.Path(temperature_particles_path))
    points.CreatePointsAttr(_vec3f_array(positions))
    points.CreateWidthsAttr(Vt.FloatArray([float(particle_width)] * positions.shape[0]))

    color_attr = _create_vertex_primvar(points.GetPrim(), "displayColor", Sdf.ValueTypeNames.Color3fArray)
    temperature_attr = _create_vertex_primvar(points.GetPrim(), "temperature", Sdf.ValueTypeNames.FloatArray)

    _advance_temperature_samples(
        color_attr=color_attr,
        temperature_attr=temperature_attr,
        neighbors=neighbors,
        heater_mask=heater_mask,
    )


def _author_temperature_instancer(
    stage: Any,
    temperature_particles_path: str,
    positions: np.ndarray,
    particle_width: float,
    neighbors: np.ndarray,
    heater_mask: np.ndarray,
) -> None:
    bin_count = 16
    instancer = UsdGeom.PointInstancer.Define(stage, Sdf.Path(temperature_particles_path))
    instancer.CreatePositionsAttr(_vec3f_array(positions))
    scale = max(0.001, particle_width * 0.5)
    instancer.CreateScalesAttr(
        Vt.Vec3fArray([Gf.Vec3f(scale, scale, scale) for _ in range(positions.shape[0])])
    )

    prototype_scope_path = Sdf.Path(temperature_particles_path).AppendChild("Prototypes")
    UsdGeom.Scope.Define(stage, prototype_scope_path)
    prototype_paths = []
    colors = _temperature_bin_colors(bin_count)

    for index, color in enumerate(colors):
        prototype_path = prototype_scope_path.AppendChild(f"TemperatureSphere_{index:02d}")
        sphere = UsdGeom.Sphere.Define(stage, prototype_path)
        sphere.CreateRadiusAttr(1.0)
        sphere.CreateDisplayColorAttr(_vec3f_array(np.array([color], dtype=np.float32)))
        prototype_paths.append(prototype_path)

    instancer.CreatePrototypesRel().SetTargets(prototype_paths)
    temperature_attr = _create_vertex_primvar(instancer.GetPrim(), "temperature", Sdf.ValueTypeNames.FloatArray)
    proto_indices_attr = instancer.CreateProtoIndicesAttr()

    _advance_temperature_samples(
        color_attr=None,
        temperature_attr=temperature_attr,
        neighbors=neighbors,
        heater_mask=heater_mask,
        proto_indices_attr=proto_indices_attr,
        proto_bin_count=bin_count,
    )


def _set_display_color_opacity(gprim: Any, color: tuple[float, float, float], opacity: float) -> None:
    gprim.CreateDisplayColorAttr(_vec3f_array(np.array([color], dtype=np.float32)))
    gprim.CreateDisplayOpacityAttr(Vt.FloatArray([float(opacity)]))


def _clear_and_set_translate(prim: Any, translation: np.ndarray) -> None:
    xformable = UsdGeom.Xformable(prim)
    xformable.ClearXformOpOrder()
    xformable.AddTranslateOp().Set(
        Gf.Vec3d(float(translation[0]), float(translation[1]), float(translation[2]))
    )


def author_heater_indicator(
    stage: Any,
    heater_indicator_path: str,
    geometry: CylinderGeometry,
    heating_mode: str,
) -> None:
    path = Sdf.Path(heater_indicator_path)
    axis_index = {"X": 0, "Y": 1, "Z": 2}[geometry.axis]

    if heating_mode == "internal":
        sphere = UsdGeom.Sphere.Define(stage, path)
        sphere.CreateRadiusAttr(max(geometry.radius_eff * 0.15, 0.001))
        _set_display_color_opacity(sphere, (1.0, 0.0, 0.0), 0.45)
        _clear_and_set_translate(sphere.GetPrim(), geometry.center)
        return

    cylinder = UsdGeom.Cylinder.Define(stage, path)
    cylinder.CreateAxisAttr(geometry.axis)
    cylinder.CreateRadiusAttr(max(geometry.radius_eff, 0.001))

    translation = geometry.center.copy()
    if heating_mode == "bottom":
        disk_height = max(geometry.height_eff * 0.025, 0.001)
        cylinder.CreateHeightAttr(disk_height)
        translation[axis_index] += -0.5 * geometry.height_eff + 0.5 * disk_height
        _set_display_color_opacity(cylinder, (1.0, 0.0, 0.0), 0.35)
    else:
        cylinder.CreateHeightAttr(geometry.height_eff)
        _set_display_color_opacity(cylinder, (1.0, 0.0, 0.0), 0.16)

    _clear_and_set_translate(cylinder.GetPrim(), translation)


def choose_output_paths(water_path: str, geometry: CylinderGeometry) -> tuple[str, str]:
    water_sdf_path = Sdf.Path(water_path)
    if geometry.space == "local":
        return (
            str(water_sdf_path.AppendChild("TemperatureParticles")),
            str(water_sdf_path.AppendChild("TemperatureHeaterIndicator")),
        )

    parent = water_sdf_path.GetParentPath()
    if parent == Sdf.Path.absoluteRootPath:
        return "/TemperatureParticlesInsideWater", "/TemperatureHeaterIndicator"

    return (
        str(parent.AppendChild("TemperatureParticlesInsideWater")),
        str(parent.AppendChild("TemperatureHeaterIndicator")),
    )


def validate_paths(input_usd: Path, output_usd: Path, allow_overwrite_input: bool) -> None:
    if not input_usd.exists():
        raise FileNotFoundError(f"Input USD does not exist: {input_usd}")

    if input_usd.resolve() == output_usd.resolve() and not allow_overwrite_input:
        raise ValueError(
            "--output is the same as --usd. Use --allow-overwrite-input if you really want that."
        )


def main() -> int:
    args = parse_args()
    input_usd = Path(args.usd)
    topology_path = Path(args.topology)
    output_usd = Path(args.output)

    validate_paths(input_usd, output_usd, args.allow_overwrite_input)
    _bootstrap_kit_environment(args.kit_app_template)
    import_numpy()
    import_usd_modules()

    topology_json = load_topology_json(topology_path)
    print(f"Loaded USD: {input_usd}")
    print(f"Loaded topology: {topology_path}")

    candidates = find_prim_candidates(topology_json, args.water_name)
    for candidate in candidates:
        print(f"Found water prim candidate: {candidate.path}")

    water_path = select_water_path(candidates, explicit_water_path=args.water_path)
    if not water_path:
        raise ValueError(f"No prim named {args.water_name!r} was found in topology JSON.")

    stage = Usd.Stage.Open(str(input_usd))
    if stage is None:
        raise RuntimeError(f"Could not open USD stage: {input_usd}")

    water_prim = stage.GetPrimAtPath(water_path)
    if not water_prim or not water_prim.IsValid():
        raise ValueError(f"Selected water prim does not exist in USD stage: {water_path}")

    print(f"Selected water prim: {water_path}")
    print(f"Water prim type: {water_prim.GetTypeName()}")

    geometry = infer_water_cylinder_geometry(stage, water_prim)
    print(f"Inferred radius: {geometry.radius:.6g}")
    print(f"Inferred height: {geometry.height:.6g}")
    print(f"Inferred axis: {geometry.axis}")

    if geometry.space == "local":
        positions = generate_cylinder_particles_local(geometry.radius, geometry.height, geometry.axis)
        validate_particles_inside_local_cylinder(positions, geometry)
    else:
        positions = generate_cylinder_particles_world_fallback(
            geometry.center,
            geometry.radius,
            geometry.height,
        )

    assert positions.shape == (NUM_PARTICLES, 3)
    assert np.all(np.isfinite(positions))
    print(f"Generated {NUM_PARTICLES} particles inside water")

    heater_mask = compute_heater_mask(positions, geometry, args.heating_mode)
    assert len(heater_mask) == NUM_PARTICLES
    assert int(heater_mask.sum()) > 0
    print(f"Selected {int(heater_mask.sum())} heater particles")

    neighbors = build_neighbor_graph(positions, K_NEIGHBORS)
    print(f"Built neighbor graph with k={K_NEIGHBORS}")

    particle_width = max(0.005, min(0.04, 0.025 * geometry.radius))
    temperature_particles_path, heater_indicator_path = choose_output_paths(water_path, geometry)

    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(FRAMES - 1)
    stage.SetTimeCodesPerSecond(FPS)

    author_temperature_points(
        stage=stage,
        temperature_particles_path=temperature_particles_path,
        positions=positions,
        particle_width=particle_width,
        neighbors=neighbors,
        heater_mask=heater_mask,
        render_mode=args.render_mode,
    )
    author_heater_indicator(stage, heater_indicator_path, geometry, args.heating_mode)

    output_usd.parent.mkdir(parents=True, exist_ok=True)
    if not stage.GetRootLayer().Export(str(output_usd)):
        raise RuntimeError(f"Failed to export USD: {output_usd}")

    print(f"Saved {output_usd}")
    return 0


def _running_inside_kit_script_editor() -> bool:
    return "omni.kit.app" in sys.modules and not any(arg.startswith("--") for arg in sys.argv[1:])


if __name__ == "__main__":
    if _running_inside_kit_script_editor():
        print(
            "add_temperature_particles_to_water.py is a terminal CLI tool. "
            "The Aquacast extension now loads runtime temperature particles automatically; "
            "launch with ./start_aquacast.sh --streaming instead of running this in Script Editor."
        )
    else:
        try:
            raise SystemExit(main())
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1)
