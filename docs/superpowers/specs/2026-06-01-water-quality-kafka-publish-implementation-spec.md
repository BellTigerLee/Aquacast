# Water Quality → Kafka Publish — Implementation Spec

**Date:** 2026-06-01
**Component:** `backend/` — **new** `kafka_payload.py` (pure) + `kafka_publisher.py` (IO);
edits to `water_quality_backend.py`, `aquacast-backend.env`, `requirements.txt`; **new**
`backend/tests/test_kafka_payload.py`.
**Status:** Implementation spec — code-ready. This is the **HOW** companion to
[`2026-06-01-water-quality-kafka-publish-design.md`](2026-06-01-water-quality-kafka-publish-design.md)
(the WHAT/WHY) and runs on the topology in
[`2026-06-01-kafka-deployment-compose-design.md`](2026-06-01-kafka-deployment-compose-design.md).
Every insertion point below is grounded in the **current** `water_quality_backend.py`
(verified, not assumed). Goal: emit one JSON record per sensor to topic
**`aquacast.water_quality`** on each `/advance` and `/reset`, **without** blocking the
HTTP path and with **zero** behavior change when disabled.

> Read the design doc for rationale (cadence, ordering, reliability). This spec does not
> re-argue those; it pins the exact code.

## 0. Verified facts (grounding)

Confirmed by reading the live code — these drive the exact implementation:

| Fact | Source | Implication |
|---|---|---|
| `WaterQualityBackend.advance()` ends with `return self.snapshot()` | `water_quality_backend.py:63-66` | hook the publish call just before that return, inside `self._lock` |
| `WaterQualityBackend.reset()` ends with `return self.snapshot()` | `water_quality_backend.py:49-54` | same hook for the post-reset baseline |
| `self._lock` is an **RLock** | `water_quality_backend.py:46` | publisher may re-enter `all_sensors()`/`snapshot()` from the same thread safely |
| `all_sensors()["readings"]` items have **no `status` key** (only `sensor()` adds one) | `water_quality_backend.py:100-105` vs `93-98` | publisher must treat **missing status as ok** (publish), not skip |
| Reading keys: `sensor_name, temperature_c, dissolved_oxygen_mg_l, do_mg_l, tan_mg_l, co2_mg_l, alkalinity_mg_l_as_caco3, ph, nh3_mg_l, nitrite_mg_l, nitrate_mg_l` | `water_quality_model.py:52-67, 214-225` | `do_mg_l` is a dup of `dissolved_oxygen_mg_l` → exclude from `measurements` |
| `snapshot()` carries `sim_time_h` | `water_quality_model.py:38,64,171` | stamp it on every record of an advance |
| 6 sensors in `DEFAULT_SENSOR_NAMES` | `water_quality_model.py:20-27` | 6 records per advance by default |
| env loaded via `_load_env_file` before `build_server` | `water_quality_backend.py:217-228, 251-255` | new `AQUACAST_KAFKA_*` keys are in `os.environ` by the time the backend is built |
| `RequestHandler.backend` holds the singleton backend | `water_quality_backend.py:239` | `main()` shutdown can reach the publisher via `RequestHandler.backend.kafka` |

**Correction vs design doc §5.3:** the design says non-ok readings are skipped "via the
`status` the handler adds." In the `all_sensors()` path there is **no** `status` key, so
the skip rule is a no-op in practice. The publisher treats `status` absent **or** `"ok"`
as publishable; only an explicit non-ok status is skipped.

## 1. File 1 (new, pure): `backend/kafka_payload.py`

Zero kafka/omni imports. Deterministic (clock injected). Pytest-covered.

```python
"""Pure helpers to build/serialize the Kafka payload for a water-quality reading.

No kafka/omni imports; deterministic given inputs (event_time_ms is passed in, not
read from a clock here) so it stays unit-testable like fish_dynamics / water_quality_model.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

# Reading keys forwarded into measurements. do_mg_l excluded (duplicate of
# dissolved_oxygen_mg_l, verified water_quality_model.py:57).
MEASUREMENT_KEYS = (
    "temperature_c", "dissolved_oxygen_mg_l", "tan_mg_l", "nh3_mg_l",
    "co2_mg_l", "ph", "alkalinity_mg_l_as_caco3", "nitrite_mg_l", "nitrate_mg_l",
)

def iso_from_ms(event_time_ms: int) -> str:
    dt = datetime.fromtimestamp(event_time_ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

def message_key(tank_id: str, sensor_name: str) -> bytes:
    return f"{tank_id}:{sensor_name}".encode("utf-8")

def build_message(reading: dict, *, tank_id: str, event_time_ms: int, seq: int,
                  sim_time_h: float | None = None, schema_version: int = 1,
                  source: str = "aquacast-backend") -> dict | None:
    status = reading.get("status")
    if status is not None and status != "ok":          # absent == ok (see §0 correction)
        return None
    sensor_name = reading.get("sensor_name")
    if not sensor_name:
        return None
    measurements = {k: reading[k] for k in MEASUREMENT_KEYS if k in reading}
    msg = {
        "schema_version": schema_version,
        "source": source,
        "tank_id": tank_id,
        "sensor_name": sensor_name,
        "event_time": iso_from_ms(event_time_ms),
        "event_time_ms": event_time_ms,
        "seq": seq,
        "measurements": measurements,
    }
    if sim_time_h is not None:
        msg["sim_time_h"] = sim_time_h
    return msg

def serialize(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
```

## 2. File 2 (new, IO): `backend/kafka_publisher.py`

Owns the producer lifecycle. Inert unless `AQUACAST_KAFKA_ENABLED` is truthy. Never
raises into the HTTP path.

```python
"""Kafka producer for the water-quality backend. Inert unless AQUACAST_KAFKA_ENABLED.

produce() is async/non-blocking (librdkafka background thread); a down broker buffers +
warns, never stalls /advance. Owned by WaterQualityBackend; called on every advance/reset.
"""
from __future__ import annotations
import os, time, logging
import kafka_payload

log = logging.getLogger("aquacast.kafka")

def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}

class KafkaPublisher:
    def __init__(self, env: dict | None = None):
        env = env if env is not None else os.environ
        self.enabled = _truthy(env.get("AQUACAST_KAFKA_ENABLED"))
        self.bootstrap = env.get("AQUACAST_KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")
        self.topic = env.get("AQUACAST_KAFKA_TOPIC", "aquacast.water_quality")
        self.tank_id = env.get("AQUACAST_KAFKA_TANK_ID", "tank-01")
        self.client_id = env.get("AQUACAST_KAFKA_CLIENT_ID", "aquacast-wq-backend")
        self.acks = env.get("AQUACAST_KAFKA_ACKS", "1")
        self.compression = env.get("AQUACAST_KAFKA_COMPRESSION", "lz4")
        self.linger_ms = int(env.get("AQUACAST_KAFKA_LINGER_MS", "50"))
        self.delivery_timeout_ms = int(env.get("AQUACAST_KAFKA_DELIVERY_TIMEOUT_MS", "10000"))
        self.max_buffer = int(env.get("AQUACAST_KAFKA_MAX_BUFFER_MESSAGES", "100000"))
        self.flush_s = float(env.get("AQUACAST_KAFKA_FLUSH_ON_SHUTDOWN_SECONDS", "3.0"))
        self._producer = None
        self._seq = 0
        self._dropped = 0

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
        except Exception as exc:                       # lib absent → inert, keep serving
            log.error("[Aquacast Kafka] confluent_kafka unavailable (%s); inert", exc)
            self.enabled = False
            return
        self._producer = Producer({
            "bootstrap.servers": self.bootstrap,
            "client.id": self.client_id,
            "acks": self.acks,
            "compression.type": self.compression,
            "linger.ms": self.linger_ms,
            "message.timeout.ms": self.delivery_timeout_ms,
            "queue.buffering.max.messages": self.max_buffer,
        })
        log.info("[Aquacast Kafka] producer ready → %s topic=%s", self.bootstrap, self.topic)

    def publish_state(self, backend) -> None:
        if not self.enabled or self._producer is None:
            return
        ts = int(time.time() * 1000)
        try:
            readings = backend.all_sensors()["readings"]
            sim_h = backend.snapshot().get("sim_time_h")
        except Exception as exc:
            log.warning("[Aquacast Kafka] could not read backend state: %s", exc)
            return
        for r in readings:
            self._publish_reading(r, ts, sim_h)
        self._producer.poll(0)                          # serve delivery callbacks, non-blocking

    def _publish_reading(self, reading: dict, ts: int, sim_h) -> None:
        self._seq += 1
        msg = kafka_payload.build_message(reading, tank_id=self.tank_id,
                                          event_time_ms=ts, seq=self._seq, sim_time_h=sim_h)
        if msg is None:
            return
        try:
            self._producer.produce(
                self.topic,
                key=kafka_payload.message_key(self.tank_id, reading["sensor_name"]),
                value=kafka_payload.serialize(msg),
                on_delivery=self._on_delivery,
            )
        except BufferError:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                log.warning("[Aquacast Kafka] local queue full; dropped=%d", self._dropped)

    def _on_delivery(self, err, msg) -> None:
        if err is not None:
            log.warning("[Aquacast Kafka] delivery failed: %s", err)

    def close(self) -> None:
        if self._producer is not None:
            try:
                self._producer.flush(self.flush_s)
            finally:
                self._producer = None
```

## 3. Edits to `backend/water_quality_backend.py`

Four surgical edits. No routing/response change; HTTP output to Kit is byte-identical.

**3a. import (after line 26):**
```python
from kafka_publisher import KafkaPublisher  # noqa: E402
```
(`backend/` is the CWD of `python backend/water_quality_backend.py`, and is already on
`sys.path` as the script dir, so a flat import resolves — same as `water_quality_model`
via the `EXT_ROOT` insert. The two new files sit next to this one.)

**3b. `__init__` — construct + start (end of `__init__`, after line 47):**
```python
        self.kafka = KafkaPublisher()      # reads AQUACAST_KAFKA_* from os.environ
        self.kafka.start()
```

**3c. `advance()` — publish before returning (replace lines 63-66 body):**
```python
    def advance(self, real_dt_s: float, temperature_c: float | None = None) -> dict[str, Any]:
        with self._lock:
            self.model.advance(real_dt_s, temperature_c=temperature_c)
            snap = self.snapshot()
            self.kafka.publish_state(self)   # non-blocking; RLock re-entry is safe
            return snap
```

**3d. `reset()` — publish post-reset baseline (replace lines 49-54 body):**
```python
    def reset(self, scenario_name: str | None = None) -> dict[str, Any]:
        with self._lock:
            name = scenario_name or self.scenario_name
            self.model = load_model(self.constants_path, self.feed_rate_path, self.scenarios_path, name)
            self.scenario_name = name
            snap = self.snapshot()
            self.kafka.publish_state(self)
            return snap
```

**3e. `main()` — bounded flush on shutdown (in the `finally`, around lines 261-262):**
```python
    finally:
        try:
            RequestHandler.backend.kafka.close()
        except Exception:
            pass
        server.server_close()
```

> Note: `publish_state(self)` re-calls `all_sensors()`/`snapshot()` (one extra cheap
> recompute). Acceptable for the ~2 Hz cadence; if ever hot, pass `snap`/`readings` in.
> Keeping the design's `publish_state(self)` signature for now (simplicity).

## 4. `backend/requirements.txt`

```
numpy==2.4.6
confluent-kafka==2.14.0
```
`confluent-kafka` ships manylinux wheels with bundled librdkafka → no extra apt in the
(now-slim) Dockerfile; it rides the existing `COPY backend /app/backend`. If the wheel
ever fails on the target, fall back to `kafka-python` (design doc §11) — only
`kafka_publisher.start()`'s import line changes.

## 5. `backend/aquacast-backend.env`

Append the `AQUACAST_KAFKA_*` block (design doc §6). Defaults keep behavior unchanged
(`ENABLED=0`). For the compose stack, `docker-compose.yml` already sets
`AQUACAST_KAFKA_BOOTSTRAP_SERVERS=kafka:9092` + `AQUACAST_KAFKA_TOPIC`; to actually emit,
flip `ENABLED=1`.

```ini
AQUACAST_KAFKA_ENABLED=0
AQUACAST_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092
AQUACAST_KAFKA_TOPIC=aquacast.water_quality
AQUACAST_KAFKA_TANK_ID=tank-01
AQUACAST_KAFKA_CLIENT_ID=aquacast-wq-backend
AQUACAST_KAFKA_ACKS=1
AQUACAST_KAFKA_COMPRESSION=lz4
AQUACAST_KAFKA_LINGER_MS=50
AQUACAST_KAFKA_DELIVERY_TIMEOUT_MS=10000
AQUACAST_KAFKA_MAX_BUFFER_MESSAGES=100000
AQUACAST_KAFKA_FLUSH_ON_SHUTDOWN_SECONDS=3.0
```

(Snapshot stream §5.4 and security keys from the design doc are deferred; not needed for
the single-topic `aquacast.water_quality` goal.)

## 6. Tests — `backend/tests/test_kafka_payload.py` (pure)

Pure pytest, no kafka/Kit (run via the repo venv — see memory `wq-test-env`):
`./.venv/bin/python -m pytest backend/tests/ -v`.

```python
import json
import kafka_payload as kp     # backend/ on sys.path when run from repo root via the venv

_READING = {"sensor_name": "feed_zone_tan", "temperature_c": 14.02,
            "dissolved_oxygen_mg_l": 8.91, "do_mg_l": 8.91, "tan_mg_l": 0.42,
            "co2_mg_l": 5.1, "alkalinity_mg_l_as_caco3": 120.0, "ph": 7.21,
            "nh3_mg_l": 0.012, "nitrite_mg_l": 0.0, "nitrate_mg_l": 0.0}

def test_build_message_copies_only_measurement_keys_and_drops_do_mg_l():
    m = kp.build_message(_READING, tank_id="tank-01", event_time_ms=1717245296789,
                         seq=421, sim_time_h=3.27)
    assert "do_mg_l" not in m["measurements"]
    assert m["measurements"]["dissolved_oxygen_mg_l"] == 8.91
    assert set(m["measurements"]) <= set(kp.MEASUREMENT_KEYS)
    assert m["sensor_name"] == "feed_zone_tan" and m["seq"] == 421
    assert m["sim_time_h"] == 3.27 and m["source"] == "aquacast-backend"

def test_build_message_skips_explicit_non_ok_status():
    assert kp.build_message({**_READING, "status": "stale"}, tank_id="t",
                            event_time_ms=0, seq=1) is None

def test_build_message_absent_status_is_published():
    assert kp.build_message(_READING, tank_id="t", event_time_ms=0, seq=1) is not None

def test_message_key_bytes():
    assert kp.message_key("tank-01", "feed_zone_tan") == b"tank-01:feed_zone_tan"

def test_serialize_is_sorted_compact_and_roundtrips():
    m = kp.build_message(_READING, tank_id="t", event_time_ms=0, seq=1)
    raw = kp.serialize(m)
    assert b", " not in raw and b": " not in raw          # compact
    assert json.loads(raw) == m

def test_iso_from_ms_known_value():
    assert kp.iso_from_ms(1717245296789) == "2026-06-01T12:34:56.789Z"
```
> The `iso_from_ms` expected string must be recomputed for the chosen epoch ms at
> implementation time (the value above is illustrative — assert against the real UTC
> conversion of whatever ms you pick).

Producer/IO code in `kafka_publisher.py` is **not** unit-tested (needs a broker); it is
covered by the integration smoke in §7.

## 7. Verification (ordered)

1. **Pure unit:** `./.venv/bin/python -m pytest backend/tests/test_kafka_payload.py -v` → green.
2. **Disabled-default no-op:** start backend with `AQUACAST_KAFKA_ENABLED=0`; `POST /advance`;
   confirm HTTP 200 snapshot **identical** to pre-change and **no** topic created.
3. **Enabled integration (compose):** `cd backend && docker compose up -d --build`;
   set `AQUACAST_KAFKA_ENABLED=1` (env file or compose `environment:`); drive `/advance`
   (Kit, or `curl -XPOST localhost:8765/advance -d '{"real_dt_s":0.5}'`); then
   `docker exec aquacast-kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic aquacast.water_quality --from-beginning --max-messages 6`
   → 6 records, keys `tank-01:<sensor>`, `measurements` matching the snapshot. Also visible
   in kafka-ui (`http://localhost:8080`).
4. **Broker-down resilience:** `docker compose stop kafka`; confirm `/advance` still
   returns on time, backend logs rate-limited warnings, memory bounded; `start kafka` →
   records resume.
5. **Shutdown flush:** stop the backend; confirm bounded flush (no hang > `FLUSH_*`).

## 8. Definition of Done

- [ ] `kafka_payload.py` + `kafka_publisher.py` added; 5 pure tests green.
- [ ] 4 edits in `water_quality_backend.py` (import, `__init__`, `advance`, `reset`, `main`).
- [ ] `requirements.txt` + `aquacast-backend.env` updated.
- [ ] `ENABLED=0` → byte-identical `/advance` response, no traffic (verified).
- [ ] `ENABLED=1` + compose → 6 records/advance on `aquacast.water_quality` (verified).
- [ ] Broker outage does not stall `/advance` (verified).

## 9. Files Touched

| File | Change |
|---|---|
| `backend/kafka_payload.py` | **new** — pure build/serialize/key/iso helpers (§1) |
| `backend/kafka_publisher.py` | **new** — `KafkaPublisher` lifecycle + publish-on-state (§2) |
| `backend/water_quality_backend.py` | import + `__init__` start + `advance`/`reset` hook + `main` close (§3) |
| `backend/requirements.txt` | `+confluent-kafka==2.14.0` (§4) |
| `backend/aquacast-backend.env` | `+AQUACAST_KAFKA_*` block (§5) |
| `backend/tests/test_kafka_payload.py` | **new** — pure pytest (§6) |

**Not touched:** any Kit/Omniverse file, `docker-compose.yml` (already wired in the
deployment spec — only `ENABLED` flips at runtime), `Dockerfile` (new modules ride the
existing `COPY backend`).
