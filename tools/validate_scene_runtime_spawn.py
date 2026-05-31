#!/usr/bin/env python3
"""Validate Aquacast runtime fish and temperature particle authoring on a USD stage."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

from pxr import Gf, Usd, UsdGeom


def _quit_kit():
    try:
        import omni.kit.app

        omni.kit.app.get_app().post_quit()
    except Exception:
        pass


def _load_main():
    repo = Path(os.environ.get("AQUACAST_REPO", "/home/netai-sys/cs-project/Aquacast"))
    main_path = repo / "extensions/aquacast.aquacast_composer_extensions/main.py"
    sys.path.insert(0, str(main_path.parent))
    spec = importlib.util.spec_from_file_location("aquacast_runtime_main_validate", main_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def main():
    input_path = Path(os.environ.get("VALIDATE_INPUT", "/home/netai-sys/cs-project/assets/scene.usd"))
    stage = Usd.Stage.Open(str(input_path))
    if stage is None:
        raise SystemExit(f"failed to open {input_path}")

    main_mod = _load_main()
    spawner = main_mod.DynamicFishSpawner()
    tanks = spawner._discover_tanks(stage)
    print(f"[validate] tanks={tanks}")
    if not tanks:
        raise SystemExit("no tanks discovered")

    count = int(os.environ.get("VALIDATE_FISH_COUNT", "2"))
    salmon_scales = (
        float(main_mod.get_global_config("DYNAMIC_FISH_SALMON_1_SCALE", 1.0)),
        float(main_mod.get_global_config("DYNAMIC_FISH_SALMON_2_SCALE", 1.0)),
    )
    asset_paths = (
        main_mod.dynamic_fish_spawn.resolve_asset_path(None, str(main_mod.get_global_config("DYNAMIC_FISH_SALMON_1_PATH", ""))),
        main_mod.dynamic_fish_spawn.resolve_asset_path(None, str(main_mod.get_global_config("DYNAMIC_FISH_SALMON_2_PATH", ""))),
    )
    all_created = []
    for offset, tank in enumerate(tanks):
        created = main_mod._spawn_fish_in_tank(
            stage,
            tank,
            count,
            salmon_scales=salmon_scales,
            asset_paths=asset_paths,
            mix_ratio=float(main_mod.get_global_config("DYNAMIC_FISH_SALMON_1_RATIO", 0.5)),
            seed=12345 + offset * 1009,
        )
        print(f"[validate] fish_created[{tank}]={created}")
        if len(created) != count:
            raise SystemExit(f"expected {count} fish for {tank}, got {len(created)}")
        water_prim = main_mod._find_water_prim_for_tank(stage, tank)
        if not water_prim or not water_prim.IsValid():
            raise SystemExit(f"water prim not found for {tank}")
        expected_parent = water_prim.GetPath().GetParentPath().AppendChild("Fishes")
        if any(stage.GetPrimAtPath(path).GetPath().GetParentPath() != expected_parent for path in created):
            raise SystemExit(f"fish were not authored under sibling Fishes for {tank}")
        all_created.extend(created)

    temp = main_mod.WaterTempController()
    temp._bind_temperature_particles(stage)
    particle_sets = getattr(temp, "_particle_sets", [])
    print(f"[validate] particle_sets={len(particle_sets)}")
    if len(particle_sets) != len(tanks):
        raise SystemExit(f"expected {len(tanks)} particle sets, got {len(particle_sets)}")
    for particle_set in particle_sets:
        water_prim = particle_set.get("water_prim")
        particle_prim = particle_set.get("particles_prim")
        print(f"[validate] water={water_prim.GetPath() if water_prim else None}")
        print(f"[validate] particles={particle_prim.GetPath() if particle_prim and particle_prim.IsValid() else None}")
        if not water_prim or not water_prim.IsValid():
            raise SystemExit("particle set water prim invalid")
        if not particle_prim or not particle_prim.IsValid():
            raise SystemExit("temperature particles not authored")
        if particle_prim.GetPath().GetParentPath() != water_prim.GetPath().GetParentPath():
            raise SystemExit("temperature particles are not siblings of Water")
        sphere_children = [child for child in particle_prim.GetChildren() if child.GetTypeName() == "Sphere"]
        authoring_mode = str(main_mod.get_global_config("TEMP_PARTICLE_AUTHORING_MODE", "") or "")
        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy], useExtentsHint=True)
        water_box = bbox_cache.ComputeWorldBound(water_prim).ComputeAlignedBox()
        particle_box = bbox_cache.ComputeWorldBound(particle_prim).ComputeAlignedBox()
        print(f"[validate] water_bbox_min={tuple(round(float(water_box.GetMin()[i]), 3) for i in range(3))}")
        print(f"[validate] water_bbox_max={tuple(round(float(water_box.GetMax()[i]), 3) for i in range(3))}")
        print(f"[validate] particle_bbox_min={tuple(round(float(particle_box.GetMin()[i]), 3) for i in range(3))}")
        print(f"[validate] particle_bbox_max={tuple(round(float(particle_box.GetMax()[i]), 3) for i in range(3))}")
        if sphere_children:
            first = sphere_children[0]
            first_xf = UsdGeom.Xformable(first).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            first_pos = first_xf.ExtractTranslation()
            radius_attr = UsdGeom.Sphere(first).GetRadiusAttr().Get()
            print(f"[validate] first_sphere={first.GetPath()} world_pos={tuple(round(float(first_pos[i]), 3) for i in range(3))} radius={radius_attr}")
        if authoring_mode == "sphere_prims":
            print(f"[validate] sphere_particle_children={len(sphere_children)}")
            expected_count = int(main_mod.get_global_config("TEMP_PARTICLE_COUNT", 0))
            if len(sphere_children) != expected_count:
                raise SystemExit(f"expected {expected_count} sphere particle prims, got {len(sphere_children)}")

    swim = main_mod.FishSwimController()
    swim._water_bounds_by_fishes_parent = {}
    for tank in tanks:
        water_prim = main_mod._find_water_prim_for_tank(stage, tank)
        bounds = swim._read_water_bounds_values(water_prim)
        swim._water_bounds_by_fishes_parent[water_prim.GetPath().GetParentPath().AppendChild("Fishes").pathString] = bounds
    swim._apply_water_bounds(next(iter(swim._water_bounds_by_fishes_parent.values())))
    states = [swim._make_fish_state(stage.GetPrimAtPath(path), index) for index, path in enumerate(all_created)]
    print(f"[validate] fish_swim_states={len(states)}")
    if len(states) != len(all_created):
        raise SystemExit("fish swim states were not created for all fish")


if __name__ == "__main__":
    try:
        main()
    finally:
        _quit_kit()
