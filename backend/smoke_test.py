"""Smoke test for the Aquacast water-quality backend."""

from __future__ import annotations

import argparse
import json
from urllib.request import Request, urlopen


def get_json(base_url: str, path: str) -> dict:
    with urlopen(f"{base_url.rstrip('/')}{path}", timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(base_url: str, path: str, payload: dict) -> dict:
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def assert_ok(payload: dict, label: str) -> dict:
    if payload.get("status") != "ok":
        raise AssertionError(f"{label} failed: {payload}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test Aquacast water-quality backend")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    assert_ok(get_json(args.url, "/health"), "health")
    assert_ok(get_json(args.url, "/snapshot"), "snapshot")
    advanced = assert_ok(post_json(args.url, "/advance", {"real_dt_s": 0.25, "temperature_c": 14.0}), "advance")
    for key in ("dissolved_oxygen_mg_l", "tan_mg_l", "co2_mg_l", "alkalinity_mg_l_as_caco3", "ph", "nh3_mg_l"):
        if key not in advanced:
            raise AssertionError(f"advance missing key: {key}")
    sensor = assert_ok(get_json(args.url, "/sensor?name=fish_core_do"), "sensor")
    if sensor.get("sensor_name") != "fish_core_do":
        raise AssertionError(f"unexpected sensor payload: {sensor}")
    particles = assert_ok(
        post_json(args.url, "/particle-values", {"heat_weights": [0.0, 0.5, 1.0], "positions": [[0, 0, 0], [1, 1, 1], [2, 2, 2]]}),
        "particle-values",
    )
    values = particles.get("values") or {}
    if sorted(values) != ["alkalinity", "co2", "dissolved_oxygen", "nh3", "ph", "tan", "temperature"]:
        raise AssertionError(f"unexpected particle fields: {sorted(values)}")
    print("aquacast water-quality backend smoke ok")


if __name__ == "__main__":
    main()
