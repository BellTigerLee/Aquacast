# Water Quality Sensor → Kafka Publish — Design

**Date:** 2026-06-01
**Component:** `backend/` (the water-quality digital-twin service) — new
`kafka_publisher.py` + new pure `kafka_payload.py`; additions to
`water_quality_backend.py`, `aquacast-backend.env`, `requirements.txt`, `Dockerfile`,
`docker-compose.yml`.
**Status:** Design only — **no implementation in this document**. Logic spec for review.

## 1. Background & where the twin actually lives

The water-quality **digital twin** is split across two processes:

| Role | Where | What it does |
|---|---|---|
| Twin **brain** (simulation + sensing) | **`backend/water_quality_backend.py`** → `WaterQualityModel` | Computes temperature / dissolved oxygen / TAN / CO2 / pH / NH3 / alkalinity from a physical-chemical model and synthesizes the per-sensor readings. **Does not import Omniverse / touch USD** (file docstring). |
| Twin **body** (visualization) | **Omniverse Kit** `WaterQualityController` (`main.py`) | Pulls those values over HTTP (`water_quality_backend_client.py`) and renders them as particle colors + the *Water Quality Sensor* UI panel. |

So the **sensing data is produced by the backend twin**, not by Omniverse. Omniverse
only *displays* it. Therefore Kafka publishing belongs in the **backend** — emit at the
source of truth.

**Clock ownership (confirmed decision):** the simulation clock is driven by
**Omniverse**, kept as-is. The backend is passive — its `WaterQualityModel` only
advances when Kit POSTs `/advance` (`real_dt_s`) each frame; there is no self-running
loop in the backend today (`serve_forever()` only). **We do not add a backend tick
loop.** Consequence: data flows (and Kafka records are produced) **only while a Kit
session is running and calling `/advance`.** When no Kit viewer is connected, the twin
is frozen and the Kafka stream is idle by design.

## 2. Goals

- Publish the backend twin's water-quality readings (**temperature, dissolved oxygen,
  TAN, CO2, alkalinity, pH, NH3**) to Apache Kafka as structured JSON.
- Publish **from inside the backend process**, at the moment the model state changes —
  i.e. on each `/advance` (and on `/reset`, which also changes state). No new clock; the
  existing Kit-driven `/advance` cadence is the publish cadence.
- Reuse the backend's existing model accessors (`snapshot()`, `all_sensors()`) — **no
  change to `WaterQualityModel`**; only the HTTP handler is extended to also publish.
- Enrich each message with fields the readings lack: `timestamp`, `tank_id`,
  `schema_version`, `source`, plus the model's own `sim_time_h`.
- **Never block or break the HTTP response path**: producing to Kafka must be
  non-blocking; a down/slow broker degrades to buffering + warnings, never stalls
  `/advance` (which would stall Kit's frame).
- Opt-in via a config flag (env), so default behavior is unchanged when disabled.
- Keep JSON-building logic in a **pure, pytest-able module** (repo convention).
- Fit the existing backend packaging (env-file config, Dockerfile, docker-compose).

## 3. Non-Goals

- Publishing **from Omniverse/Kit**. Rejected: Kit would be re-publishing values it
  received over HTTP (one hop removed), and would force `confluent-kafka` into the Kit
  runtime + a non-blocking render-loop design. The backend is the correct source.
- Adding a **self-running simulation loop** to the backend (clock stays Kit-driven; §1).
- Consuming from Kafka, or any Kafka→Aquacast inbound control path.
- Persisting/queuing to disk across backend restarts (in-memory producer buffer only).
- Exactly-once semantics. Target is **at-least-once** with idempotent-friendly keys.
- A schema registry / Avro / Protobuf. v1 is plain JSON UTF-8 (Avro noted in §13).
- Publishing particle heat fields or fish data — only water-quality sensor readings
  (and optionally the aggregate tank snapshot; §5.4).
- Multi-tank fan-out. The model is single-tank today; `tank_id` is a config constant
  now and becomes per-tank when multi-tank lands (§13).

## 4. Architecture

The backend already runs a `ThreadingHTTPServer` (`water_quality_backend.py`). The
publisher is owned by the `WaterQualityBackend` object and invoked from the request
handler after a state-changing call. Same pure/IO split the repo enforces.

| Responsibility | Where | Notes |
|---|---|---|
| Build/serialize/enrich the Kafka payload from a reading dict | `backend/kafka_payload.py` (**new, pure**) | No kafka/omni imports; pytest-covered |
| Kafka producer lifecycle, publish-on-state-change, delivery callbacks, buffering | `backend/kafka_publisher.py` (**new**) | Imports the kafka client; owned by `WaterQualityBackend` |
| Call the publisher after `advance`/`reset` | `water_quality_backend.py` (`WaterQualityBackend`) | One call site each; reuses `all_sensors()`/`snapshot()` |
| Config | `aquacast-backend.env` + `_load_env_file` (existing pattern) | All `AQUACAST_KAFKA_*` |
| Packaging | `requirements.txt`, `Dockerfile`, `docker-compose.yml` | Add client lib + broker wiring |

Data flow:

```
Omniverse Kit  ──HTTP POST /advance {real_dt_s}──►  WaterQualityBackend.advance()
                                                        │  model.advance(real_dt_s)        (existing)
                                                        │  snap = self.snapshot()          (existing)
                                                        │  publisher.publish_state(self)    (NEW, non-blocking)
                                                        │       readings = self.all_sensors()["readings"]
                                                        │       sim_h    = snap["sim_time_h"]
                                                        │       for r in readings:
                                                        │           msg = kafka_payload.build_message(r, tank_id, ts, seq, sim_h)
                                                        │           producer.produce(topic, key, value, on_delivery=cb)
                                                        │       producer.poll(0)            # serve callbacks, non-blocking
                                                        ▼
                                                   HTTP 200 (snapshot) returns to Kit  ← unaffected by Kafka
                                                        │
                                                        ▼
                                                   Kafka broker(s)
```

The HTTP response to Kit is computed and returned exactly as today; the Kafka produce
is an enqueue (non-blocking) that happens before returning. A broker problem cannot
delay the `/advance` response (§8–9).

## 5. Message Design

### 5.0 Publish cadence — on every sensor update (confirmed decision)

- **Publish on every model update**, i.e. once per `/advance` (and once on `/reset`).
  No publisher-side time gate, **no batching**. Each update emits **one Kafka record per
  sensor** (6 records with the default 6 sensors), streamed individually.
- **The cadence is governed by the existing twin clock, not by the publisher.** Kit's
  `WaterQualityController._on_update` already throttles how often it POSTs `/advance`:
  it only advances when `dt ≥ WQ_UPDATE_INTERVAL_SECONDS` (default **0.5 s**, scaled by
  `WQ_TIME_SCALE`) — see `main.py` `_on_update`/`_advance`. So "every sensor update" is
  naturally ~2 Hz at the default, **not** the 30–60 Hz frame rate. The backend simply
  publishes whenever it is asked to advance; the rate follows `WQ_UPDATE_INTERVAL_SECONDS`.
- This **replaces** the earlier 1 Hz time-gate / `PUBLISH_EVERY_N_ADVANCE` design. The
  publisher does **not** gate or decimate; it publishes the fresh sensor values produced
  by each advance. (An optional safety min-interval knob is offered in §6, defaulting to
  `0` = off, for the rare case the advance rate is cranked very high.)
- Invoked inline from the existing `/advance`/`/reset` handler — **no new thread**, and
  if Kit stops calling `/advance` the stream stops with it (expected; the twin is frozen,
  §1).

### 5.1 Topic & record granularity

- **One Kafka record per sensor per update.** Each advance emits one record per sensor —
  each a single point measurement (time-series-sink friendly), partitionable by sensor.
  No batching/array record (a single combined-snapshot record was considered and
  rejected in favor of plain per-sensor streaming).
- **Topic:** `KAFKA_TOPIC` (default `aquacast.water_quality`). Single topic; sensor +
  tank are encoded in the key/payload, not the topic name.

### 5.2 Record key

- **Key = `"{tank_id}:{sensor_name}"`** (UTF-8). Guarantees all readings for a given
  (tank, sensor) land on the **same partition → strict per-sensor ordering**, and is a
  natural compaction key for a "latest value per sensor" topic.

### 5.3 Value (JSON, one sensor reading)

```jsonc
{
  "schema_version": 1,
  "source": "aquacast-backend",
  "tank_id": "tank-01",                      // from config (single-tank today)
  "sensor_name": "feed_zone_tan",
  "event_time": "2026-06-01T12:34:56.789Z",  // ISO-8601 UTC, ms precision (wall clock)
  "event_time_ms": 1717245296789,            // epoch millis (sink-friendly)
  "sim_time_h": 3.27,                          // model's own clock, from snapshot()
  "seq": 421,                                  // monotonic per-publisher counter
  "measurements": {
    "temperature_c": 14.02,
    "dissolved_oxygen_mg_l": 8.91,
    "tan_mg_l": 0.42,
    "nh3_mg_l": 0.012,
    "co2_mg_l": 5.10,
    "ph": 7.21,
    "alkalinity_mg_l_as_caco3": 120.0,
    "nitrite_mg_l": 0.0,
    "nitrate_mg_l": 0.0
  }
}
```

**Verified against the backend reading schema** (`WaterQualityModel.sensor_reading(...)
.as_dict()`, used by `WaterQualityBackend.all_sensors()`). Each per-sensor reading
contains: `sensor_name, temperature_c, dissolved_oxygen_mg_l, do_mg_l, tan_mg_l,
co2_mg_l, alkalinity_mg_l_as_caco3, ph, nh3_mg_l, nitrite_mg_l, nitrate_mg_l`
(+ `status` added by the handler). Notes:

- `measurements` mirrors the reading's numeric keys **verbatim** (no renaming). There is
  **no** `*_saturation_pct` field — DO is carried as `dissolved_oxygen_mg_l` only.
- `do_mg_l` is a duplicate of `dissolved_oxygen_mg_l` → **dropped** from `measurements`.
- `nitrite_mg_l`/`nitrate_mg_l` are currently `0.0` but included for forward-compat.
- `sim_time_h` comes from `snapshot()` (a snapshot-level field). The publisher reads the
  snapshot once per `/advance` and stamps the same `sim_time_h` on every sensor record
  of that call → each record carries both wall-clock (`event_time_ms`) and sim-clock.
- `source` is `"aquacast-backend"` to make the origin explicit (vs the prior Kit design).
- A reading whose `status != "ok"` is **skipped** (logged once).

### 5.4 Optional aggregate snapshot record

Behind `KAFKA_PUBLISH_SNAPSHOT` (default off): one extra record per `/advance` to
`KAFKA_SNAPSHOT_TOPIC` (default `aquacast.water_quality.snapshot`) carrying the full
`snapshot()` (well-mixed state + `fish_count`, `flow_lph`, `biofilter_on`,
`inflow_enabled`, thermal `q_*`, `scenario`, `sim_time_h`, …). Same envelope, snapshot
dict under `state`, key = `tank_id`.

## 6. Configuration — `aquacast-backend.env`

Added to the existing env file (loaded by `_load_env_file`; backend reads
`os.environ`). All `AQUACAST_KAFKA_*`:

```ini
AQUACAST_KAFKA_ENABLED=0                       # master opt-in (default off → no behavior change)
AQUACAST_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092
AQUACAST_KAFKA_TOPIC=aquacast.water_quality
AQUACAST_KAFKA_PUBLISH_SNAPSHOT=0
AQUACAST_KAFKA_SNAPSHOT_TOPIC=aquacast.water_quality.snapshot
AQUACAST_KAFKA_TANK_ID=tank-01                 # single-tank identity (see §13)
AQUACAST_KAFKA_CLIENT_ID=aquacast-wq-backend
AQUACAST_KAFKA_ACKS=1                           # 0 | 1 | all
AQUACAST_KAFKA_COMPRESSION=lz4                  # none|gzip|snappy|lz4|zstd
AQUACAST_KAFKA_MAX_BUFFER_MESSAGES=100000       # local producer queue cap
AQUACAST_KAFKA_DELIVERY_TIMEOUT_MS=10000
AQUACAST_KAFKA_LINGER_MS=50
AQUACAST_KAFKA_FLUSH_ON_SHUTDOWN_SECONDS=3.0
# Publish on every model update (every /advance). The natural rate is set by Kit's
# WQ_UPDATE_INTERVAL_SECONDS (~0.5 s default), not by the publisher (see 5.0).
# Optional safety throttle: minimum seconds between publishes; 0 = publish every update.
AQUACAST_KAFKA_MIN_PUBLISH_INTERVAL_SECONDS=0
# Security (optional; blank = PLAINTEXT)
AQUACAST_KAFKA_SECURITY_PROTOCOL=
AQUACAST_KAFKA_SASL_MECHANISM=
AQUACAST_KAFKA_SASL_USERNAME=
AQUACAST_KAFKA_SASL_PASSWORD=
```

**Cadence note:** publishing happens on every model update. Kit does **not** POST
`/advance` per frame — `WaterQualityController._on_update` gates it to
`WQ_UPDATE_INTERVAL_SECONDS` (default 0.5 s), so the natural stream is ~2 records/sec
per sensor, set by the twin's own update rate. `AQUACAST_KAFKA_MIN_PUBLISH_INTERVAL_SECONDS`
(default `0` = off) is only an optional ceiling if that rate is ever pushed high enough
to need throttling; normally it is unused.

## 7. Pure Module — `backend/kafka_payload.py`

Zero kafka/omni imports. Deterministic given inputs. Pytest-covered.

```python
# Exact reading keys to forward (verified against SensorReading.as_dict).
# do_mg_l intentionally excluded (duplicate of dissolved_oxygen_mg_l).
MEASUREMENT_KEYS = (
    "temperature_c", "dissolved_oxygen_mg_l", "tan_mg_l", "nh3_mg_l",
    "co2_mg_l", "ph", "alkalinity_mg_l_as_caco3", "nitrite_mg_l", "nitrate_mg_l",
)

def build_message(reading: dict, *, tank_id: str, event_time_ms: int, seq: int,
                  sim_time_h: float | None = None,
                  schema_version: int = 1, source: str = "aquacast-backend") -> dict | None
# Pulls sensor_name + present MEASUREMENT_KEYS into the §5.3 envelope.
# Adds event_time (ISO from event_time_ms) + event_time_ms + seq + optional sim_time_h.
# Returns None for a reading whose status != "ok" (caller skips + logs).

def message_key(tank_id: str, sensor_name: str) -> bytes      # b"{tank_id}:{sensor_name}"
def serialize(message: dict) -> bytes                          # compact, sorted-key UTF-8 JSON
def iso_from_ms(event_time_ms: int) -> str                     # epoch ms → ISO-8601 UTC, ms precision
```

`event_time_ms` is **passed in** (not read from a clock inside the pure module) so it
stays deterministic and tests don't depend on wall time — mirroring how
`water_quality_model`/`dynamic_fish_spawn` keep injectable seeds.

## 8. Producer Layer — `backend/kafka_publisher.py`

### 8.1 Client choice

- **`confluent-kafka`** (librdkafka). `Producer.produce()` is **asynchronous and
  non-blocking** — it enqueues into librdkafka's background I/O thread and returns
  immediately. Critical: the `/advance` handler (which Kit awaits) must not block on the
  network. `kafka-python` is an acceptable pure-Python fallback (plan-B; §11).
- **No extra threads added by us.** Publishing happens inline on the HTTP handler thread
  (the backend already uses `ThreadingHTTPServer`, so each request is its own thread).
  `produce()` + `poll(0)` are cheap and non-blocking.

### 8.2 Lifecycle (owned by `WaterQualityBackend`)

```python
class KafkaPublisher:
    def __init__(self, config: dict): ...      # parse env; _producer=None, _seq=0, _advance_count=0
    def start(self):
        # if not enabled: stay inert (publish_state() becomes a no-op)
        # lazily build confluent_kafka.Producer(self._producer_config())
    def publish_state(self, backend):           # called on EVERY advance()/reset()
        # if disabled/inert: return
        # ts = now_ms()
        # optional safety throttle (default off, min_interval_ms == 0):
        #   if min_interval_ms and ts - self._last_publish_ms < min_interval_ms: return
        #   self._last_publish_ms = ts
        # readings = backend.all_sensors()["readings"]; snap = backend.snapshot()
        # sim_h = snap.get("sim_time_h")
        # for r in readings: self._publish_reading(r, ts, sim_h)   # one record per sensor
        # if publish_snapshot: self._publish_snapshot(snap, ts)
        # self._producer.poll(0)
    def _publish_reading(self, reading, ts, sim_h):
        # msg = kafka_payload.build_message(reading, tank_id=..., event_time_ms=ts,
        #                                   seq=self._next_seq(), sim_time_h=sim_h)
        # if msg is None: log-once skip; return
        # self._producer.produce(topic,
        #     key=kafka_payload.message_key(tank_id, reading["sensor_name"]),
        #     value=kafka_payload.serialize(msg), on_delivery=self._on_delivery)
    def _on_delivery(self, err, msg):           # err → rate-limited warn; else optional debug
    def close(self):                            # producer.flush(flush_timeout); _producer=None
```

`now_ms()` lives in the publisher (impure) and feeds the pure builder; the pure module
itself never calls a clock.

### 8.3 Wiring into `water_quality_backend.py`

- `WaterQualityBackend.__init__`: construct `self.kafka = KafkaPublisher(<env config>)`
  and call `self.kafka.start()`.
- `WaterQualityBackend.advance(...)`: after computing `snap = self.snapshot()`, call
  `self.kafka.publish_state(self)` **before returning** `snap` (still non-blocking).
- `WaterQualityBackend.reset(...)`: same one-line `self.kafka.publish_state(self)` so
  consumers see the post-reset baseline.
- `main()` shutdown path: call `backend.kafka.close()` so buffered messages get a
  bounded flush on exit (next to the existing `serve_forever()`/`KeyboardInterrupt`
  handling).

No change to `RequestHandler` routing or to the JSON returned to Kit.

### 8.4 Producer config mapping

`_producer_config()` builds the librdkafka dict from §6: `bootstrap.servers,
client.id, acks, compression.type, linger.ms, message.timeout.ms
(=DELIVERY_TIMEOUT_MS), queue.buffering.max.messages (=MAX_BUFFER_MESSAGES)`, and
security props only when `SECURITY_PROTOCOL` is set (`security.protocol,
sasl.mechanism, sasl.username, sasl.password`).

## 9. Reliability, Backpressure, Failure Handling

| Condition | Behavior |
|---|---|
| `AQUACAST_KAFKA_ENABLED=0` | Publisher inert; `publish_state()` is a no-op. Zero overhead, identical baseline. |
| `confluent_kafka` import fails (lib absent) | `start()` logs one error, stays inert, backend keeps serving HTTP normally. |
| Broker unreachable | `Producer()` construction doesn't block (lazy connect); messages buffer up to `queue.buffering.max.messages`; librdkafka retries. **`/advance` still returns on time.** |
| Local queue full (`BufferError`) | Drop the message, increment a dropped counter, **rate-limited** warning. Never block the HTTP thread to wait for space. |
| Per-message delivery failure | `on_delivery(err, msg)` logs rate-limited; librdkafka already retried within `message.timeout.ms`. |
| Non-ok / partial reading | Skip that sensor, log-once. |
| Backend shutdown | `producer.flush(FLUSH_ON_SHUTDOWN_SECONDS)` (bounded) then release. |
| Bad config (empty bootstrap servers) | `start()` validates; if invalid → log error + stay inert. |

**Backpressure principle:** the `/advance` HTTP response is on the critical path of
Kit's frame loop and must return promptly. Kafka is best-effort downstream — under a
broker outage we **drop with visible warnings**, we never stall `/advance` and never
grow memory unbounded (librdkafka queue cap + warn-on-drop).

## 10. Ordering, Delivery Semantics, Idempotency

- **Ordering:** per-(tank,sensor) ordering via key→partition (§5.2). Cross-sensor order
  not guaranteed nor needed.
- **Delivery:** at-least-once (`acks=1` default; `acks=all` via config). Duplicates
  possible on retry.
- **Consumer idempotency:** dedupe on `(tank_id, sensor_name, seq)` or
  `(key, event_time_ms)`. `seq` is monotonic within a backend run and resets on restart;
  `event_time_ms` is the cross-restart tiebreaker. `sim_time_h` lets consumers align to
  the twin's own clock independent of wall time / pauses.

## 11. Dependency / Packaging

The backend's only dep today is `numpy` (`requirements.txt`) and it runs on
`python:3.12-slim` (Dockerfile). Adding Kafka:

1. **`requirements.txt`**: add `confluent-kafka` (preferred; bundles librdkafka wheels
   for slim). Fallback `kafka-python` if a native wheel is problematic on the target.
2. **`Dockerfile`**: no system packages needed for the `confluent-kafka` manylinux
   wheel; `COPY backend/kafka_publisher.py backend/kafka_payload.py` ride along with the
   existing `COPY backend /app/backend`.
3. **`docker-compose.yml`**: add the `AQUACAST_KAFKA_*` env (or rely on `env_file`), and
   optionally a `kafka` broker service + `depends_on` for a self-contained local stack;
   for an external/managed broker just point `AQUACAST_KAFKA_BOOTSTRAP_SERVERS` at it.

Backend stays Omniverse-free — Kafka does not change that boundary. (Kit's
`global_variable.py`/`extension.py` are **not touched** by this design.)

## 12. Testing

- **Pytest — `backend/tests/test_kafka_payload.py`** (pure; no kafka/Kit):
  - `build_message`: copies only present `MEASUREMENT_KEYS`; drops `do_mg_l`; adds
    envelope + `sim_time_h`; `status != "ok"` → `None`.
  - `message_key`: exact `b"{tank}:{sensor}"` bytes.
  - `serialize`: stable sorted-key compact JSON; round-trips via `json.loads`.
  - `iso_from_ms`: known epoch ms → exact ISO-8601 UTC, ms precision.
  Run: `python -m pytest backend/tests/ -v` (no Kit/USD; runnable like the backend).
- **`smoke_test.py` extension** (optional): with a local broker + `AQUACAST_KAFKA_ENABLED=1`,
  POST `/advance` and assert a consumer receives N sensor records.
- **Producer / IO code** — not unit-tested (integration-level; needs a broker).
- **Manual integration smoke** (broker up):
  1. `AQUACAST_KAFKA_ENABLED=1`, broker at `AQUACAST_KAFKA_BOOTSTRAP_SERVERS`; start
     backend; start Kit (which drives `/advance`).
  2. `kafka-console-consumer --topic aquacast.water_quality --from-beginning` shows one
     batch of 6 records (one per sensor) **on every model update** — ~2/sec per sensor
     at the default `WQ_UPDATE_INTERVAL_SECONDS=0.5`. Keys `tank-01:<sensor>`,
     `measurements` matching the UI panel values.
  3. Stop the broker → backend `/advance` keeps responding, Kit keeps rendering;
     rate-limited drop/connect warnings in backend log; memory bounded.
  4. Restart broker → records resume.
  5. Stop Kit → `/advance` calls stop → Kafka stream goes idle (expected; clock is
     Kit-driven, §1).
  6. `AQUACAST_KAFKA_ENABLED=0` → no producer, no traffic, identical baseline.

## 13. Forward Compatibility

- **Multi-tank:** `AQUACAST_KAFKA_TANK_ID` becomes a per-tank lookup once the model is
  multi-tank; key (`{tank_id}:{sensor}`) and payload already carry tank identity →
  consumers unchanged.
- **Self-running twin:** if the clock decision is later reversed (backend drives its own
  tick), `publish_state()` simply moves from the `/advance` handler to that loop — the
  publisher and payload are unchanged. (Captured so the seam is obvious.)
- **Schema evolution:** `schema_version` in every record; bump for breaking changes.
  Avro + Schema Registry is the natural next step — `build_message` would feed the Avro
  serializer instead of `json`.
- **Nitrite/Nitrate / NO2:** already in `MEASUREMENT_KEYS` (currently `0.0`); become
  meaningful automatically when the model populates them. A new key needs only a
  `MEASUREMENT_KEYS` addition.

## 14. Files Touched (at implementation time — not done here)

| File | Change |
|---|---|
| `backend/kafka_payload.py` | **new** — pure payload build/serialize/key helpers |
| `backend/kafka_publisher.py` | **new** — `KafkaPublisher` + producer lifecycle |
| `backend/water_quality_backend.py` | construct/start publisher in `WaterQualityBackend.__init__`; `publish_state(self)` call in `advance()` and `reset()`; `close()` on shutdown. **No routing/response change.** |
| `backend/aquacast-backend.env` | `+AQUACAST_KAFKA_*` (§6) |
| `backend/requirements.txt` | `+confluent-kafka` |
| `backend/Dockerfile` | new modules ride existing `COPY backend`; no extra apt |
| `backend/docker-compose.yml` | `AQUACAST_KAFKA_*` env; optional local `kafka` service |
| `backend/tests/test_kafka_payload.py` | **new** — pure pytest coverage |
| `backend/smoke_test.py` | optional Kafka assertion when enabled |

**Not touched:** any Omniverse/Kit file (`main.py`, `extension.py`,
`global_variable.py`, `water_quality_backend_client.py`). The Kit side is unaware of
Kafka.

## 15. Open Questions

**Resolved:** publish cadence = **on every sensor update (every `/advance`/`/reset`),
one record per sensor, no batching, no publisher-side gate** (§5.0, §5.1). The natural
rate is governed by Kit's `WQ_UPDATE_INTERVAL_SECONDS` (~0.5 s / ~2 Hz default);
`AQUACAST_KAFKA_MIN_PUBLISH_INTERVAL_SECONDS` defaults to `0` (off).

Still open:

1. **Broker coordinates & security** for the target env (PLAINTEXT local vs SASL_SSL
   managed cluster) — drives `AQUACAST_KAFKA_SECURITY_*` defaults.
2. **Dependency:** `confluent-kafka` acceptable, or must it be pure-Python
   `kafka-python`?
3. **Snapshot stream (§5.4):** publish the aggregate tank snapshot too, or sensors only?
4. **Local broker in compose:** bundle a `kafka` service for a self-contained dev stack,
   or always point at an external broker?
