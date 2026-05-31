#!/usr/bin/env python3
"""Inspect selected prim names and large bbox candidates in a USD stage."""

from __future__ import annotations

import os
from pathlib import Path

from pxr import Usd, UsdGeom


def _quit_kit():
    try:
        import omni.kit.app

        omni.kit.app.get_app().post_quit()
    except Exception:
        pass


def _vec3(value):
    return tuple(round(float(value[index]), 3) for index in range(3))


def main():
    input_path = Path(os.environ.get("INSPECT_INPUT", "/home/netai-sys/cs-project/assets/scene.usd")).expanduser()
    stage = Usd.Stage.Open(str(input_path))
    if stage is None:
        raise SystemExit(f"failed to open {input_path}")

    names = {
        token.strip().lower()
        for token in os.environ.get(
            "INSPECT_NAMES",
            "Water,Fishtank,FishTank,Aquarium,InWater,Fishes,ParticleSystem,Isosurface,Sensor,inlet_reference",
        ).split(",")
        if token.strip()
    }
    bbox_cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
        useExtentsHint=True,
    )

    matches = []
    mesh_candidates = []
    type_counts = {}
    for prim in stage.Traverse():
        if not prim or not prim.IsValid():
            continue
        type_name = prim.GetTypeName()
        type_counts[type_name] = type_counts.get(type_name, 0) + 1
        path = prim.GetPath().pathString
        name = prim.GetName()
        if name.lower() in names or any(token in path.lower() for token in names):
            matches.append((path, type_name))
        if type_name == "Mesh":
            try:
                box = bbox_cache.ComputeWorldBound(prim).ComputeAlignedBox()
                minimum = box.GetMin()
                maximum = box.GetMax()
                size = [float(maximum[i] - minimum[i]) for i in range(3)]
                volume_hint = size[0] * size[1] * size[2]
                mesh_candidates.append((volume_hint, path, _vec3(minimum), _vec3(maximum), _vec3(size)))
            except Exception:
                pass

    print(f"[inspect] input={input_path}")
    print(f"[inspect] default_prim={stage.GetDefaultPrim().GetPath() if stage.GetDefaultPrim() else None}")
    print(f"[inspect] pseudo_root_children={[child.GetPath().pathString for child in stage.GetPseudoRoot().GetChildren()]}")
    print(f"[inspect] type_counts={dict(sorted(type_counts.items()))}")
    print(f"[inspect] name_matches={len(matches)}")
    for path, type_name in matches[:80]:
        print(f"[inspect] match type={type_name} path={path}")
    print("[inspect] largest_mesh_bboxes")
    for _volume, path, minimum, maximum, size in sorted(mesh_candidates, reverse=True)[:30]:
        print(f"[inspect] mesh size={size} min={minimum} max={maximum} path={path}")


if __name__ == "__main__":
    try:
        main()
    finally:
        _quit_kit()
