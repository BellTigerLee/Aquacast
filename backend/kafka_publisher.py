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
        self.bootstrap = env.get("AQUACAST_KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")
        self.topic = env.get("AQUACAST_KAFKA_TOPIC", "aquacast.water_quality")
        self.tank_id = env.get("AQUACAST_KAFKA_TANK_ID", "tank-01")
        self.client_id = env.get("AQUACAST_KAFKA_CLIENT_ID", "aquacast-wq-backend")
        self.acks = env.get("AQUACAST_KAFKA_ACKS", "1")
        self.compression = env.get("AQUACAST_KAFKA_COMPRESSION", "lz4")
        self.linger_ms = _int_env(env, "AQUACAST_KAFKA_LINGER_MS", 50)
        self.delivery_timeout_ms = _int_env(env, "AQUACAST_KAFKA_DELIVERY_TIMEOUT_MS", 10000)
        self.max_buffer = _int_env(env, "AQUACAST_KAFKA_MAX_BUFFER_MESSAGES", 100000)
        self.flush_s = _float_env(env, "AQUACAST_KAFKA_FLUSH_ON_SHUTDOWN_SECONDS", 3.0)
        self.db_enabled = not _truthy(env.get("AQUACAST_DB_DISABLED"))
        self.db_path = aquacast_db.db_path_from_env(env)
        self._producer: Any | None = None
        self._store: aquacast_db.WideMessageStore | None = None
        self._seq = 0
        self._dropped = 0

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
        log.info("[Aquacast Kafka] producer ready -> %s topic=%s", self.bootstrap, self.topic)

    def publish_state(self, backend: Any) -> None:
        event_time_ms = int(time.time() * 1000)
        try:
            readings = backend.all_sensors()["readings"]
            sim_time_h = backend.snapshot().get("sim_time_h")
        except Exception as exc:
            log.warning("[Aquacast Kafka] could not read backend state: %s", exc)
            return

        for reading in readings:
            self._publish_reading(reading, event_time_ms, sim_time_h)
        if self._producer is not None:
            self._producer.poll(0)

    def _publish_reading(self, reading: dict, event_time_ms: int, sim_time_h: Any) -> None:
        self._seq += 1
        message = kafka_payload.build_message(
            reading,
            tank_id=self.tank_id,
            event_time_ms=event_time_ms,
            seq=self._seq,
            sim_time_h=sim_time_h,
        )
        if message is None:
            return

        message_key = kafka_payload.message_key(self.tank_id, reading["sensor_name"])
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
