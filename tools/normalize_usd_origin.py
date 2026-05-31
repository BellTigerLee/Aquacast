#!/usr/bin/env python3
"""Move a USD subtree so its bounds are centered on the stage origin.

Run inside an Omniverse Kit app so pxr modules are available, for example:
  aquacast.aquacast_composer.kit.sh --exec tools/normalize_usd_origin.py
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import time

from pxr import Gf, Usd, UsdGeom


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=os.environ.get("NORMALIZE_INPUT"))
    parser.add_argument("--target-path", default=os.environ.get("NORMALIZE_TARGET_PATH", "/Root/scene"))
    parser.add_argument(
        "--anchor",
        choices=("center", "bottom-center"),
        default=os.environ.get("NORMALIZE_ANCHOR", "center"),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--print-samples", type=int, default=int(os.environ.get("NORMALIZE_PRINT_SAMPLES", "12")))
    args, unknown = parser.parse_known_args()
    if unknown and unknown[0] == "--":
        unknown = unknown[1:]
    if unknown:
        args = parser.parse_args(unknown)
    if os.environ.get("NORMALIZE_APPLY", "").strip().lower() in {"1", "true", "yes", "on"}:
        args.apply = True
    if not args.input:
        parser.error("the following arguments are required: --input or NORMALIZE_INPUT")
    return args


def _vec_tuple(vec):
    return tuple(round(float(vec[i]), 6) for i in range(3))


def _set_local_transform(xformable, matrix):
    xformable.ClearXformOpOrder()
    xformable.AddTransformOp(precision=UsdGeom.XformOp.PrecisionDouble).Set(matrix)


def _quit_kit():
    try:
        import omni.kit.app

        omni.kit.app.get_app().post_quit()
    except Exception:
        pass


def main():
    args = _parse_args()
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"input not found: {input_path}")

    stage = Usd.Stage.Open(str(input_path))
    if stage is None:
        raise SystemExit(f"failed to open stage: {input_path}")

    target = stage.GetPrimAtPath(args.target_path)
    if not target or not target.IsValid():
        raise SystemExit(f"target prim not found: {args.target_path}")

    bbox_cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
        useExtentsHint=True,
    )
    aligned = bbox_cache.ComputeWorldBound(target).ComputeAlignedBox()
    minimum = aligned.GetMin()
    maximum = aligned.GetMax()
    center = Gf.Vec3d(
        (minimum[0] + maximum[0]) * 0.5,
        (minimum[1] + maximum[1]) * 0.5,
        (minimum[2] + maximum[2]) * 0.5,
    )
    if args.anchor == "bottom-center":
        anchor = Gf.Vec3d(center[0], minimum[1], center[2])
    else:
        anchor = center
    world_shift = Gf.Vec3d(-anchor[0], -anchor[1], -anchor[2])

    changed = []
    for prim in Usd.PrimRange(target):
        if prim.GetTypeName() != "Mesh":
            continue
        xformable = UsdGeom.Xformable(prim)
        old_world = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        parent = prim.GetParent()
        parent_world = UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        new_world = Gf.Matrix4d(old_world)
        old_translation = old_world.ExtractTranslation()
        new_world.SetTranslateOnly(
            Gf.Vec3d(
                old_translation[0] + world_shift[0],
                old_translation[1] + world_shift[1],
                old_translation[2] + world_shift[2],
            )
        )
        new_local = new_world * parent_world.GetInverse()
        changed.append(
            {
                "path": prim.GetPath().pathString,
                "old": _vec_tuple(old_world.ExtractTranslation()),
                "new": _vec_tuple(new_world.ExtractTranslation()),
            }
        )
        if args.apply:
            _set_local_transform(xformable, new_local)

    if args.apply:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = input_path.with_name(f"{input_path.name}.bak-{stamp}")
        shutil.copy2(input_path, backup_path)
        stage.GetRootLayer().Save()
        print(f"[normalize] backup={backup_path}")
        print(f"[normalize] saved={input_path}")
    else:
        print("[normalize] dry_run=true")

    print(f"[normalize] target={args.target_path} anchor={args.anchor}")
    print(f"[normalize] bbox_min={_vec_tuple(minimum)} bbox_max={_vec_tuple(maximum)}")
    print(f"[normalize] anchor_world={_vec_tuple(anchor)} shift={_vec_tuple(world_shift)}")
    print(f"[normalize] meshes={len(changed)}")
    for item in changed[: max(0, args.print_samples)]:
        print(f"[normalize] sample old={item['old']} new={item['new']} path={item['path']}")

    if args.apply:
        validation_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(),
            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
            useExtentsHint=True,
        )
        validation = validation_cache.ComputeWorldBound(target).ComputeAlignedBox()
        print(
            f"[normalize] new_bbox_min={_vec_tuple(validation.GetMin())} "
            f"new_bbox_max={_vec_tuple(validation.GetMax())}"
        )

    _quit_kit()


if __name__ == "__main__":
    try:
        main()
    finally:
        _quit_kit()
