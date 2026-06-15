#!/usr/bin/env python3
"""Dump a USD stage prim tree and fish-tank sensor summary to JSON."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pxr import Usd, UsdGeom


def _quit_kit():
    try:
        import omni.kit.app

        omni.kit.app.get_app().post_quit()
    except Exception:
        pass


_TANK_RE = re.compile(r"^Fishtank_\d+$")


def _authored_attribute_names(prim):
    names = []
    for attr in prim.GetAttributes():
        try:
            if attr.HasAuthoredValueOpinion():
                names.append(attr.GetName())
        except Exception:
            pass
    return names


def _prim_dict(prim):
    item = {
        "path": prim.GetPath().pathString,
        "name": prim.GetName(),
        "type": prim.GetTypeName(),
        "attributes": _authored_attribute_names(prim),
        "children": [_prim_dict(child) for child in prim.GetChildren()],
    }
    custom_data = dict(prim.GetCustomData())
    if custom_data:
        item["custom_data"] = custom_data
    return item


def _find_sensors_prim(prim):
    if prim.GetName() == "Sensors":
        return prim
    for child in prim.GetChildren():
        found = _find_sensors_prim(child)
        if found:
            return found
    return None


def _tank_summaries(stage):
    tanks = []
    for prim in stage.Traverse():
        if not prim or not prim.IsValid() or not _TANK_RE.match(prim.GetName()):
            continue
        sensors_prim = _find_sensors_prim(prim)
        tanks.append(
            {
                "name": prim.GetName(),
                "path": prim.GetPath().pathString,
                "has_sensors": sensors_prim is not None,
                "sensor_children": [child.GetName() for child in sensors_prim.GetChildren()] if sensors_prim else [],
            }
        )
    return tanks


def main():
    input_path = Path(os.environ.get("DUMP_INPUT", "~/cs-project/assets/scene.usd")).expanduser()
    output_path = Path(os.environ.get("DUMP_OUTPUT", "~/cs-project/scene_prim_structure.json")).expanduser()
    stage = Usd.Stage.Open(str(input_path))
    if stage is None:
        raise SystemExit(f"failed to open {input_path}")

    pseudo_root = stage.GetPseudoRoot()
    default_prim = stage.GetDefaultPrim()
    payload = {
        "input": str(input_path),
        "default_prim": default_prim.GetPath().pathString if default_prim else None,
        "pseudo_root_children": [child.GetPath().pathString for child in pseudo_root.GetChildren()],
        "tanks": _tank_summaries(stage),
        "tree": _prim_dict(pseudo_root),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[dump] wrote {output_path} tanks={len(payload['tanks'])}")


if __name__ == "__main__":
    try:
        main()
    finally:
        _quit_kit()
