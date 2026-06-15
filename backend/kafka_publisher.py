"""Kafka publisher for the water-quality backend."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import kafka_payload
import aquacast_db


log = logging.getLogger("aquacast.kafka")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(env: dict[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(env: dict[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class KafkaPublisher:
    """Best-effort Kafka producer, inert unless explicitly enabled."""

    def __init__(self, env: dict[str, str] | None = None):
        env = env if env is not None else os.environ
        self.enabled = _truthy(env.get("AQUACAST_KAFKA_ENABLED"))
        self.bootstrap = env.get("AQUACAST_KAFKA_BOOTSTRAP_SERVERS", "localhost:29092")
        self.topic = env.get("AQUACAST_KAFKA_TOPIC", "aquacast.water_quality")
        self.threshold_alert_topic = env.get("AQUACAST_KAFKA_THRESHOLD_ALERT_TOPIC", "aquacast.threshold_alert")
        self.publish_interval_s = _float_env(
            env,
            "AQUACAST_KAFKA_PUBLISH_INTERVAL_SECONDS",
            _float_env(env, "AQUACAST_KAFKA_MIN_PUBLISH_INTERVAL_SECONDS", 1.0),
        )
        self.tank_id = env.get("AQUACAST_KAFKA_TANK_ID", "tank-01")
        self.client_id = env.get("AQUACAST_KAFKA_CLIENT_ID", "aquacast-wq-backend")
        self.acks = env.get("AQUACAST_KAFKA_ACKS", "1")
        self.compression = env.get("AQUACAST_KAFKA_COMPRESSION", "lz4")
        self.linger_ms = _int_env(env, "AQUACAST_KAFKA_LINGER_MS", 50)
        self.delivery_timeout_ms = _int_env(env, "AQUACAST_KAFKA_DELIVERY_TIMEOUT_MS", 10000)
        self.max_buffer = _int_env(env, "AQUACAST_KAFKA_MAX_BUFFER_MESSAGES", 100000)
        self.flush_s = _float_env(env, "AQUACAST_KAFKA_FLUSH_ON_SHUTDOWN_SECONDS", 3.0)
        self.threshold_alert_repeat_s = _float_env(env, "AQUACAST_THRESHOLD_ALERT_REPEAT_SECONDS", 60.0)
        self.db_enabled = not _truthy(env.get("AQUACAST_DB_DISABLED"))
        self.db_path = aquacast_db.db_path_from_env(env)
        self._producer: Any | None = None
        self._store: aquacast_db.WideMessageStore | None = None
        self._seq = 0
        self._dropped = 0
        self._last_state_publish_ms: int | None = None
        self._threshold_alert_state: dict[str, tuple[tuple[str, ...], int]] = {}

        if self.db_enabled:
            try:
                self._store = aquacast_db.WideMessageStore(self.db_path)
                log.info("[Aquacast DB] writer ready -> %s", self.db_path)
            except Exception as exc:
                log.error("[Aquacast DB] writer unavailable (%s); DB insert disabled", exc)
                self._store = None

    def start(self) -> None:
        if not self.enabled:
            log.info("[Aquacast Kafka] disabled (AQUACAST_KAFKA_ENABLED not set)")
            return
        if not self.bootstrap:
            log.error("[Aquacast Kafka] empty bootstrap servers; staying inert")
            self.enabled = False
            return

        try:
            from confluent_kafka import Producer
        except Exception as exc:
            log.error("[Aquacast Kafka] confluent_kafka unavailable (%s); inert", exc)
            self.enabled = False
            return

        try:
            self._producer = Producer(
                {
                    "bootstrap.servers": self.bootstrap,
                    "client.id": self.client_id,
                    "acks": self.acks,
                    "compression.type": self.compression,
                    "linger.ms": self.linger_ms,
                    "message.timeout.ms": self.delivery_timeout_ms,
                    "queue.buffering.max.messages": self.max_buffer,
                }
            )
        except Exception as exc:
            log.error("[Aquacast Kafka] producer config failed (%s); inert", exc)
            self.enabled = False
            return
        log.info(
            "[Aquacast Kafka] producer ready -> %s topic=%s threshold_topic=%s",
            self.bootstrap,
            self.topic,
            self.threshold_alert_topic,
        )

    def publish_state(self, backend: Any) -> None:
        event_time_ms = int(time.time() * 1000)
        if not self._state_publish_due(event_time_ms):
            return
        try:
            readings = self._publishable_readings(backend.all_sensors().get("readings", []))
            root_snapshot = backend.snapshot()
            sim_time_h = root_snapshot.get("sim_time_h")
        except Exception as exc:
            log.warning("[Aquacast Kafka] could not read backend state: %s", exc)
            return

        self._last_state_publish_ms = event_time_ms
        references = {
            self._reading_tank_id(reading): reading
            for reading in readings
            if reading.get("sensor_name") == "inlet_reference"
        }
        for reading in readings:
            tank_id = self._reading_tank_id(reading)
            self._publish_reading(reading, event_time_ms, reading.get("sim_time_h", sim_time_h), references.get(tank_id), tank_id)
        self._publish_threshold_alerts(backend, root_snapshot, event_time_ms)
        if self._producer is not None:
            self._producer.poll(0)

    def _state_publish_due(self, event_time_ms: int) -> bool:
        interval_ms = max(0, int(self.publish_interval_s * 1000.0))
        if interval_ms <= 0 or self._last_state_publish_ms is None:
            return True
        if event_time_ms < self._last_state_publish_ms:
            return True
        return event_time_ms - self._last_state_publish_ms >= interval_ms

    def _publishable_readings(self, readings: Any) -> list[dict]:
        if not isinstance(readings, list):
            return []

        tank_readings = [reading for reading in readings if self._has_tank_identity(reading)]
        candidates = tank_readings
        publishable = []
        seen = set()
        for reading in candidates:
            if not isinstance(reading, dict):
                continue
            sensor_name = str(reading.get("sensor_name") or "").strip()
            if not sensor_name:
                continue
            key = (self._reading_tank_id(reading), sensor_name)
            if key in seen:
                continue
            seen.add(key)
            publishable.append(reading)
        return publishable

    def _has_tank_identity(self, reading: Any) -> bool:
        return isinstance(reading, dict) and bool(str(reading.get("tank_id") or reading.get("tank_path") or "").strip())

    def _reading_tank_id(self, reading: dict) -> str:
        tank_id = str(reading.get("tank_id") or "").strip()
        if tank_id:
            return tank_id
        tank_path = str(reading.get("tank_path") or "").strip()
        if tank_path:
            return self._derive_tank_id(tank_path)
        return str(self.tank_id)

    def _derive_tank_id(self, tank_path: str) -> str:
        parts = [part for part in str(tank_path).strip("/").split("/") if part]
        if parts and parts[-1] == "Water":
            parts = parts[:-1]
        generic = {"Root", "scene", "Meshes", "Model", "Components", "Component", "Water"}
        for part in reversed(parts):
            if part in generic:
                continue
            if part.startswith("Group") and part[5:].isdigit():
                continue
            return part
        return str(self.tank_id)

    def _publish_reading(self, reading: dict, event_time_ms: int, sim_time_h: Any, reference: dict | None, tank_id: str) -> None:
        self._seq += 1
        message = kafka_payload.build_message(
            reading,
            tank_id=tank_id,
            event_time_ms=event_time_ms,
            seq=self._seq,
            sim_time_h=sim_time_h,
            reference_reading=reference,
        )
        if message is None:
            return

        message_key = kafka_payload.message_key(tank_id, reading["sensor_name"])
        if self._store is not None:
            try:
                self._store.insert_kafka_message(message, topic=self.topic)
            except Exception as exc:
                log.warning("[Aquacast DB] insert failed: %s", exc)

        if not self.enabled or self._producer is None:
            return

        try:
            self._producer.produce(
                self.topic,
                key=message_key,
                value=kafka_payload.serialize(message),
                on_delivery=self._on_delivery,
            )
        except BufferError:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                log.warning("[Aquacast Kafka] local queue full; dropped=%d", self._dropped)
        except Exception as exc:
            log.warning("[Aquacast Kafka] produce failed: %s", exc)

    def _publish_threshold_alerts(self, backend: Any, root_snapshot: dict, event_time_ms: int) -> None:
        try:
            thresholds = backend.thresholds().get("thresholds", {})
        except Exception as exc:
            log.warning("[Aquacast Kafka] could not read thresholds: %s", exc)
            return

        active_tanks = set()
        for snapshot in self._threshold_alert_snapshots(root_snapshot):
            tank_id = str(snapshot.get("tank_id") or self.tank_id)
            active_tanks.add(tank_id)
            self._seq += 1
            message = kafka_payload.build_threshold_alert(
                snapshot,
                thresholds,
                tank_id=tank_id,
                tank_name=str(snapshot.get("tank_name") or snapshot.get("tank_id") or tank_id),
                tank_path=str(snapshot.get("tank_path") or ""),
                event_time_ms=event_time_ms,
                seq=self._seq,
            )
            if message is None:
                self._threshold_alert_state.pop(tank_id, None)
                continue
            violated = tuple(sorted(str(name) for name in message.get("violated_parameter_names", [])))
            if not self._should_emit_threshold_alert(tank_id, violated, event_time_ms):
                continue
            self._publish_threshold_alert(message)

        for tank_id in list(self._threshold_alert_state):
            if tank_id not in active_tanks:
                self._threshold_alert_state.pop(tank_id, None)

    def _threshold_alert_snapshots(self, root_snapshot: dict) -> list[dict]:
        tank_snapshots = root_snapshot.get("tank_snapshots") if isinstance(root_snapshot, dict) else None
        if isinstance(tank_snapshots, dict) and tank_snapshots:
            return [dict(snapshot) for _key, snapshot in sorted(tank_snapshots.items()) if isinstance(snapshot, dict)]
        return []

    def _should_emit_threshold_alert(self, tank_id: str, violated: tuple[str, ...], event_time_ms: int) -> bool:
        previous = self._threshold_alert_state.get(tank_id)
        repeat_ms = max(0, int(self.threshold_alert_repeat_s * 1000.0))
        should_emit = True
        if previous is not None:
            previous_violated, previous_event_time_ms = previous
            same_violation_set = previous_violated == violated
            within_repeat_window = repeat_ms > 0 and event_time_ms - previous_event_time_ms < repeat_ms
            should_emit = not (same_violation_set and within_repeat_window)
        if should_emit:
            self._threshold_alert_state[tank_id] = (violated, event_time_ms)
        return should_emit

    def _publish_threshold_alert(self, message: dict) -> None:
        tank_id = str(message.get("tank_id") or self.tank_id)
        message_key = kafka_payload.threshold_alert_key(tank_id)
        if self._store is not None:
            try:
                self._store.insert_threshold_alert(message, topic=self.threshold_alert_topic, message_key=message_key)
            except Exception as exc:
                log.warning("[Aquacast DB] threshold alert insert failed: %s", exc)

        if not self.enabled or self._producer is None:
            return

        try:
            self._producer.produce(
                self.threshold_alert_topic,
                key=message_key,
                value=kafka_payload.serialize(message),
                on_delivery=self._on_delivery,
            )
        except BufferError:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                log.warning("[Aquacast Kafka] local queue full; dropped=%d", self._dropped)
        except Exception as exc:
            log.warning("[Aquacast Kafka] threshold alert produce failed: %s", exc)

    def _on_delivery(self, err: Any, msg: Any) -> None:
        del msg
        if err is not None:
            log.warning("[Aquacast Kafka] delivery failed: %s", err)

    def close(self) -> None:
        if self._producer is not None:
            try:
                self._producer.flush(self.flush_s)
            finally:
                self._producer = None
        if self._store is not None:
            try:
                self._store.close()
            finally:
                self._store = None
