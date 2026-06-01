from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kafka_payload as kp  # noqa: E402


_READING = {
    "sensor_name": "feed_zone_tan",
    "temperature_c": 14.02,
    "dissolved_oxygen_mg_l": 8.91,
    "do_mg_l": 8.91,
    "tan_mg_l": 0.42,
    "co2_mg_l": 5.1,
    "alkalinity_mg_l_as_caco3": 120.0,
    "ph": 7.21,
    "nh3_mg_l": 0.012,
    "nitrite_mg_l": 0.0,
    "nitrate_mg_l": 0.0,
}


def test_build_message_copies_only_measurement_keys_and_drops_do_mg_l():
    message = kp.build_message(_READING, tank_id="tank-01", event_time_ms=1717245296789, seq=421, sim_time_h=3.27)

    assert "do_mg_l" not in message["measurements"]
    assert message["measurements"]["dissolved_oxygen_mg_l"] == 8.91
    assert set(message["measurements"]) <= set(kp.MEASUREMENT_KEYS)
    assert message["sensor_name"] == "feed_zone_tan"
    assert message["seq"] == 421
    assert message["sim_time_h"] == 3.27
    assert message["source"] == "aquacast-backend"


def test_build_message_skips_explicit_non_ok_status():
    assert kp.build_message({**_READING, "status": "stale"}, tank_id="tank-01", event_time_ms=0, seq=1) is None


def test_build_message_absent_status_is_published():
    assert kp.build_message(_READING, tank_id="tank-01", event_time_ms=0, seq=1) is not None


def test_message_key_bytes():
    assert kp.message_key("tank-01", "feed_zone_tan") == b"tank-01:feed_zone_tan"


def test_serialize_is_sorted_compact_and_roundtrips():
    message = kp.build_message(_READING, tank_id="tank-01", event_time_ms=0, seq=1)
    raw = kp.serialize(message)

    assert b", " not in raw
    assert b": " not in raw
    assert json.loads(raw) == message


def test_iso_from_ms_known_value():
    assert kp.iso_from_ms(1717245296789) == "2024-06-01T12:34:56.789Z"
