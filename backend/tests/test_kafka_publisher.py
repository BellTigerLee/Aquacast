from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kafka_publisher  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.sensor_reads = 0

    def all_sensors(self):
        self.sensor_reads += 1
        return {
            "readings": [
                {
                    "sensor_name": "mixed_tank_outlet",
                    "tank_id": "Fishtank_1",
                    "tank_path": "/World/Fishtank_1/Water",
                    "dissolved_oxygen_mg_l": 9.0,
                }
            ]
        }

    def snapshot(self):
        return {"sim_time_h": 1.0}


def test_publish_state_honors_minimum_interval(monkeypatch):
    timestamps = iter([100.0, 100.25, 101.0])
    monkeypatch.setattr(kafka_publisher.time, "time", lambda: next(timestamps))
    publisher = kafka_publisher.KafkaPublisher(
        {
            "AQUACAST_DB_DISABLED": "1",
            "AQUACAST_KAFKA_PUBLISH_INTERVAL_SECONDS": "1.0",
        }
    )
    publisher._publish_threshold_alerts = lambda *_args, **_kwargs: None
    backend = FakeBackend()

    publisher.publish_state(backend)
    publisher.publish_state(backend)
    publisher.publish_state(backend)

    assert backend.sensor_reads == 2
    assert publisher._seq == 2


def test_publish_state_interval_can_be_disabled(monkeypatch):
    timestamps = iter([100.0, 100.01])
    monkeypatch.setattr(kafka_publisher.time, "time", lambda: next(timestamps))
    publisher = kafka_publisher.KafkaPublisher(
        {
            "AQUACAST_DB_DISABLED": "1",
            "AQUACAST_KAFKA_PUBLISH_INTERVAL_SECONDS": "0",
        }
    )
    publisher._publish_threshold_alerts = lambda *_args, **_kwargs: None
    backend = FakeBackend()

    publisher.publish_state(backend)
    publisher.publish_state(backend)

    assert backend.sensor_reads == 2
    assert publisher._seq == 2


def test_publish_state_skips_default_root_when_tank_readings_exist(monkeypatch):
    monkeypatch.setattr(kafka_publisher.time, "time", lambda: 100.0)
    publisher = kafka_publisher.KafkaPublisher(
        {
            "AQUACAST_DB_DISABLED": "1",
            "AQUACAST_KAFKA_TANK_ID": "tank-01",
        }
    )
    publisher._publish_threshold_alerts = lambda *_args, **_kwargs: None
    published = []
    publisher._publish_reading = lambda reading, _time_ms, _sim_h, _reference, tank_id: published.append(
        (tank_id, reading["sensor_name"])
    )

    class MultiTankBackend:
        def all_sensors(self):
            return {
                "readings": [
                    {"sensor_name": "inlet_reference"},
                    {"sensor_name": "mixed_tank_outlet"},
                    {"sensor_name": "inlet_reference", "tank_id": "Fishtank_1", "tank_path": "/World/Fishtank_1/Water"},
                    {"sensor_name": "mixed_tank_outlet", "tank_id": "Fishtank_1", "tank_path": "/World/Fishtank_1/Water"},
                    {"sensor_name": "inlet_reference", "tank_id": "Fishtank_2", "tank_path": "/World/Fishtank_2/Water"},
                ]
            }

        def snapshot(self):
            return {"sim_time_h": 1.0}

    publisher.publish_state(MultiTankBackend())

    assert published == [
        ("Fishtank_1", "inlet_reference"),
        ("Fishtank_1", "mixed_tank_outlet"),
        ("Fishtank_2", "inlet_reference"),
    ]


def test_publishable_readings_deduplicates_tank_sensor_keys():
    publisher = kafka_publisher.KafkaPublisher({"AQUACAST_DB_DISABLED": "1"})

    readings = publisher._publishable_readings(
        [
            {"sensor_name": "inlet_reference", "tank_path": "/World/Fishtank_2/Water"},
            {"sensor_name": "inlet_reference", "tank_id": "Fishtank_2"},
            {"sensor_name": "mixed_tank_outlet", "tank_path": "/World/Fishtank_2/Water"},
        ]
    )

    assert [publisher._reading_tank_id(reading) for reading in readings] == ["Fishtank_2", "Fishtank_2"]
    assert [reading["sensor_name"] for reading in readings] == ["inlet_reference", "mixed_tank_outlet"]
