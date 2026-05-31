#!/usr/bin/env python3
"""Rebake USD mesh point offsets into per-mesh prim translations.

Run with an Omniverse Kit app so pxr modules are available, for example:
  aquacast.aquacast_composer.kit.sh --exec tools/rebake_mesh_origins.py -- --input /path/scene.usd --apply
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import shutil
import sys
import time

from pxr import Gf, Usd, UsdGeom, Vt


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default=None, help='USD file to inspect or modify; env REBAKE_INPUT also works')
    parser.add_argument('--apply', action='store_true', help='Write changes. Default is dry-run; env REBAKE_APPLY=1 also works.')
    parser.add_argument('--backup', action='store_true', default=True, help='Create timestamped .bak copy before writing')
    parser.add_argument('--min-offset', type=float, default=1e-4, help='Skip meshes whose origin-to-bbox distance is below this')
    parser.add_argument('--limit', type=int, default=0, help='Optional max number of meshes to rebake')
    parser.add_argument('--print-samples', type=int, default=20, help='Number of changed mesh samples to print')
    args, unknown = parser.parse_known_args()
    if unknown and unknown[0] == '--':
        unknown = unknown[1:]
    if unknown:
        # Kit may forward args after -- differently; parse once more from the useful suffix.
        args = parser.parse_args(unknown)

    import os
    if args.input is None:
        args.input = os.environ.get('REBAKE_INPUT')
    if os.environ.get('REBAKE_APPLY', '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        args.apply = True
    if os.environ.get('REBAKE_LIMIT'):
        args.limit = int(os.environ['REBAKE_LIMIT'])
    if os.environ.get('REBAKE_PRINT_SAMPLES'):
        args.print_samples = int(os.environ['REBAKE_PRINT_SAMPLES'])
    if args.input is None:
        parser.error('the following arguments are required: --input or REBAKE_INPUT')
    return args


def _vec_to_tuple(vec):
    return tuple(round(float(vec[i]), 6) for i in range(3))


def _is_finite_vec(vec):
    return all(math.isfinite(float(vec[i])) for i in range(3))


def _distance(a, b):
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def _local_transform(xformable):
    for call in (
        lambda: xformable.GetLocalTransformation(Usd.TimeCode.Default()),
        lambda: xformable.GetLocalTransformation(),
    ):
        try:
            result = call()
        except TypeError:
            continue
        return result[0] if isinstance(result, tuple) else result
    return Gf.Matrix4d(1.0)


def _parent_world_matrix(prim):
    parent = prim.GetParent()
    if not parent or not parent.IsValid():
        return Gf.Matrix4d(1.0)
    try:
        return UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    except Exception:
        return Gf.Matrix4d(1.0)


def _set_local_transform(xformable, matrix):
    xformable.ClearXformOpOrder()
    xformable.AddTransformOp(precision=UsdGeom.XformOp.PrecisionDouble).Set(matrix)


def _rebake_mesh(stage, bbox_cache, prim):
    mesh = UsdGeom.Mesh(prim)
    points_attr = mesh.GetPointsAttr()
    points = points_attr.Get()
    if not points:
        return None

    xformable = UsdGeom.Xformable(prim)
    old_world = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    old_origin_world = old_world.ExtractTranslation()

    aligned = bbox_cache.ComputeWorldBound(prim).ComputeAlignedBox()
    minimum = aligned.GetMin()
    maximum = aligned.GetMax()
    if not (_is_finite_vec(minimum) and _is_finite_vec(maximum)):
        return None

    center_world = Gf.Vec3d(
        (minimum[0] + maximum[0]) * 0.5,
        (minimum[1] + maximum[1]) * 0.5,
        (minimum[2] + maximum[2]) * 0.5,
    )
    offset = _distance(old_origin_world, center_world)
    if offset <= _rebake_mesh.min_offset:
        return None

    parent_world = _parent_world_matrix(prim)
    local_center = parent_world.GetInverse().Transform(center_world)
    new_local = Gf.Matrix4d(_local_transform(xformable))
    new_local.SetTranslateOnly(local_center)

    if not _rebake_mesh.apply:
        return {
            'path': prim.GetPath().pathString,
            'offset': offset,
            'old_origin_world': _vec_to_tuple(old_origin_world),
            'new_origin_world': _vec_to_tuple(center_world),
            'point_count': len(points),
        }

    _set_local_transform(xformable, new_local)
    new_world = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    new_world_inv = new_world.GetInverse()

    # For imported CAD scenes in this project, mesh vertices are already baked in
    # parent/world coordinates while the mesh prim origin is zero. Move that baked
    # offset into the prim transform and subtract the same offset from points.
    new_points = []
    for point in points:
        shifted = Gf.Vec3d(point) - Gf.Vec3d(local_center)
        new_points.append(Gf.Vec3f(float(shifted[0]), float(shifted[1]), float(shifted[2])))
    if not points_attr.Set(Vt.Vec3fArray(new_points)):
        raise RuntimeError(f'failed to set points for {prim.GetPath()}')

    extent_attr = mesh.GetExtentAttr()
    extent = extent_attr.Get()
    if extent:
        new_extent = []
        for value in extent:
            shifted = Gf.Vec3d(value) - Gf.Vec3d(local_center)
            new_extent.append(Gf.Vec3f(float(shifted[0]), float(shifted[1]), float(shifted[2])))
        if not extent_attr.Set(Vt.Vec3fArray(new_extent)):
            raise RuntimeError(f'failed to set extent for {prim.GetPath()}')

    validation_cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
        useExtentsHint=True,
    )
    validation_box = validation_cache.ComputeWorldBound(prim).ComputeAlignedBox()
    validation_center = Gf.Vec3d(
        (validation_box.GetMin()[0] + validation_box.GetMax()[0]) * 0.5,
        (validation_box.GetMin()[1] + validation_box.GetMax()[1]) * 0.5,
        (validation_box.GetMin()[2] + validation_box.GetMax()[2]) * 0.5,
    )
    validation_error = _distance(validation_center, center_world)
    if validation_error > 1e-3:
        raise RuntimeError(
            f'rebake validation failed for {prim.GetPath()}: '
            f'expected bbox center {_vec_to_tuple(center_world)} got {_vec_to_tuple(validation_center)} '
            f'error={validation_error:.6f}'
        )

    return {
        'path': prim.GetPath().pathString,
        'offset': offset,
        'old_origin_world': _vec_to_tuple(old_origin_world),
        'new_origin_world': _vec_to_tuple(center_world),
        'point_count': len(points),
    }


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
        raise SystemExit(f'input not found: {input_path}')

    stage = Usd.Stage.Open(str(input_path))
    if stage is None:
        raise SystemExit(f'failed to open stage: {input_path}')

    if args.apply and args.backup:
        stamp = time.strftime('%Y%m%d_%H%M%S')
        backup_path = input_path.with_name(f'{input_path.name}.bak-{stamp}')
        shutil.copy2(input_path, backup_path)
        print(f'[rebake] backup={backup_path}')

    _rebake_mesh.apply = bool(args.apply)
    _rebake_mesh.min_offset = float(args.min_offset)

    bbox_cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
        useExtentsHint=True,
    )

    changed = []
    mesh_count = 0
    for prim in stage.Traverse():
        if not prim or not prim.IsValid() or prim.GetTypeName() != 'Mesh':
            continue
        mesh_count += 1
        info = _rebake_mesh(stage, bbox_cache, prim)
        if info is None:
            continue
        changed.append(info)
        if args.limit and len(changed) >= args.limit:
            break

    if args.apply:
        root_layer = stage.GetRootLayer()
        root_layer.Save()
        print(f'[rebake] saved={input_path}')
    else:
        print('[rebake] dry_run=true')

    print(f'[rebake] meshes={mesh_count} changed={len(changed)} min_offset={args.min_offset}')
    for item in changed[:max(0, args.print_samples)]:
        print(
            '[rebake] sample '
            f'offset={item["offset"]:.6f} points={item["point_count"]} '
            f'old={item["old_origin_world"]} new={item["new_origin_world"]} path={item["path"]}'
        )

    _quit_kit()


if __name__ == '__main__':
    try:
        main()
    finally:
        _quit_kit()
