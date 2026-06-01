# Kafka Deployment & Compose Stack — Design

**Date:** 2026-06-01
**Component:** `backend/docker-compose.yml`, `backend/Dockerfile` (deployment topology
for the water-quality backend + a bundled Apache Kafka broker + a web UI). Companion to
[`2026-06-01-water-quality-kafka-publish-design.md`](2026-06-01-water-quality-kafka-publish-design.md),
which specifies the **application logic** (the publisher inside the backend). This doc
specifies the **runtime/infra topology** that hosts it.
**Status:** Built & verified (the compose stack runs today); the in-backend **publisher
is still design-only** (no `kafka_publisher.py` yet). So this stack stands up a broker,
a UI, and a Kafka-wired backend that does **not yet emit** — it is the substrate the
publisher will plug into.

## 1. Background & relationship to the publish design

The publish design doc answers *"who emits, and what does the message look like"* and
concludes: **the backend is the source of truth, so it must be the publisher** (Kit only
displays values it pulled over HTTP). This doc answers the next question:
*"what does the deployment look like so that backend → Kafka can actually run locally?"*

That conclusion was re-verified from code while building this stack (not just trusted
from the doc) — see §2, because it directly justifies the topology.

### 1.1 Code-verified: the backend is the source of truth (with a caveat)

The Kit extension's `WaterQualityController._load_model()` builds `self._model` in **two
mutually-exclusive modes** (`main.py:3370-3396`):

| Mode | `WQ_BACKEND_ENABLED` | `self._model` is… | Sensing data computed in |
|---|---|---|---|
| **Backend** | `True` — **default** (`global_variable.py:155`) | `WaterQualityBackendClient` (pure HTTP client) | **backend** process |
| Local | `False` | `water_quality_model.load_model(...)` (in-Kit model) | **Kit** process |

Both objects expose the **identical duck-typed interface** (`advance()`, `snapshot()`,
`sensor_reading()`, `apply_feed()`, …), so `self._model.advance(dt)` (`main.py:3465`)
is an HTTP `POST /advance` in backend mode (`water_quality_backend_client.py:36`) and a
local call in local mode. The model code (`water_quality_model.py`) physically lives
under `extensions/` but the **backend imports and runs it** (`water_quality_backend.py:26`;
the `Dockerfile` copies it into the image).

**Consequence for deployment:** because the shipped default is **backend mode**, the
backend's in-process `WaterQualityModel` *is* the live twin, and publishing from the
backend captures every reading at the source. The **caveat** (under-specified in the
publish doc): if someone runs Kit with `WQ_BACKEND_ENABLED=False` and no backend, the
backend isn't in the loop and there is nothing for it to publish. Therefore this design
treats **"Kafka deployment" and "backend mode" as a single bundle** — which is already
the default, so no extra action is required.

## 2. Goals

- Stand up, with **one command**, a self-contained local stack: the water-quality
  **backend** + an **Apache Kafka broker** (`apache/kafka:4.2.1`, per request) + a **web
  UI** to inspect topics/messages.
- Make the broker reachable both **inside compose** (for the backend) and **from the
  host** (for CLI tools / external consumers).
- **Pre-wire** the backend at the in-compose broker via env, so the publisher (when
  implemented per the companion doc) works with zero further compose changes.
- Keep the backend image **Omniverse-free and lean** — strip anything not needed now
  that the broker is its own container.
- Default to a **frictionless dev posture**: ephemeral data, no auth, no manual setup.

## 3. Non-Goals

- **Implementing the publisher.** `kafka_publisher.py` / `kafka_payload.py`,
  `requirements.txt` (`confluent-kafka`), and the `advance()`/`reset()` hook are the
  companion doc's scope, not this one. This stack only provides the broker + wiring.
- **Production hardening.** No multi-broker replication, no TLS/SASL, no persistence, no
  resource limits, no schema registry. This is a **local dev / demo** topology (§9 lists
  the prod gaps).
- **Topic provisioning.** No init job pre-creates `aquacast.water_quality`; the broker's
  auto-create handles first publish (§6.4).
- **Consuming from Kafka** or any Kafka→Aquacast inbound path (same exclusion as the
  publish doc).
- **Touching Kit / Omniverse.** No `main.py` / `extension.py` / `global_variable.py`
  changes. Kit is unaware of Kafka; it keeps talking HTTP to the backend.

## 4. Topology

Three services on one default compose network (`backend_default`), defined in
`backend/docker-compose.yml`:

```
                         ┌──────────────────────────────────────────────┐
   host:8765  ◄──────────┤ water-quality-backend  (aquacast-wq-backend)  │
   (Kit POSTs /advance)  │   image: aquacast-water-quality-backend:local │
                         │   depends_on: kafka (healthy)                 │
                         │   env: AQUACAST_KAFKA_BOOTSTRAP_SERVERS=kafka:9092
                         └───────────────┬──────────────────────────────┘
                                         │ (future) produce → kafka:9092
                                         ▼
   host:9092  ───────────────►  ┌──────────────────────────┐
   host:29092 ───────────────►  │ kafka  (aquacast-kafka)   │  apache/kafka:4.2.1
   (CLI / external consumers)   │   KRaft, single node      │  broker+controller
                                └───────────┬──────────────┘
                                            │ kafka:9092 (internal listener)
                                            ▼
   host:8080  ───────────────►  ┌──────────────────────────┐
   (browser UI)                 │ kafka-ui (aquacast-kafka-ui) │ kafbat/kafka-ui
                                └──────────────────────────┘
```

Startup ordering is enforced by **healthcheck-gated `depends_on`**: both `kafka-ui` and
`water-quality-backend` wait for `kafka` to report `service_healthy` before starting, so
neither races a broker that isn't accepting connections yet.

## 5. Service Designs

### 5.1 `kafka` — broker (`apache/kafka:4.2.1`, KRaft)

Kafka 4.x is **KRaft-only** (ZooKeeper removed), so this is a single node acting as
**combined broker + controller**. Decisions:

| Setting | Value | Why |
|---|---|---|
| `KAFKA_PROCESS_ROLES` | `broker,controller` | single-node combined mode |
| `KAFKA_NODE_ID` / `KAFKA_CONTROLLER_QUORUM_VOTERS` | `1` / `1@kafka:9093` | one-node quorum |
| `*_REPLICATION_FACTOR`, `*_MIN_ISR` | `1` | only one broker exists; higher would wedge topic creation |
| `KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS` | `0` | faster consumer-group startup in dev |
| `KAFKA_AUTO_CREATE_TOPICS_ENABLE` | `true` | first publish/consume creates the topic (§6.4) |

#### 5.1.1 Listener design (the one non-obvious part)

Three listeners, because a single advertised address can't serve both in-compose and
host clients correctly:

```
KAFKA_LISTENERS         = PLAINTEXT://0.0.0.0:9092, CONTROLLER://0.0.0.0:9093, PLAINTEXT_HOST://0.0.0.0:29092
KAFKA_ADVERTISED_LISTENERS = PLAINTEXT://kafka:9092,                          PLAINTEXT_HOST://localhost:29092
```

- **`PLAINTEXT` (9092)** advertises `kafka:9092` — the Docker DNS name. Used by the
  backend and `kafka-ui` **inside** the network.
- **`PLAINTEXT_HOST` (29092)** advertises `localhost:29092` — used by tools **on the
  host**. A client must connect to the address the broker *advertises*; advertising
  `kafka:9092` to a host process would fail (no such DNS outside compose), hence the
  second listener.
- **`CONTROLLER` (9093)** is the internal KRaft quorum listener, never client-facing.

`9092` is also published to the host for convenience, but host clients should prefer
`29092` (correctly advertised). `9093` is intentionally **not** published.

#### 5.1.2 Healthcheck

`kafka-broker-api-versions.sh --bootstrap-server localhost:9092` — succeeds only once
the broker answers API requests, which is the precise readiness signal `depends_on`
needs. `interval=10s timeout=5s retries=10 start_period=20s` tolerates KRaft's
first-boot format/elect.

### 5.2 `kafka-ui` — web console (`kafbat/kafka-ui`)

Chosen over Redpanda Console / AKHQ (user pick). Read/write UI for topics, messages, and
consumer groups at **`http://localhost:8080`**. Connects over the **internal** listener
(`KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS: kafka:9092`), so it does not depend on host port
mapping. `DYNAMIC_CONFIG_ENABLED: true` allows adding clusters from the UI. Convenience
only — **not** on any data path; can be removed without affecting backend↔broker.

> Note: pinned to `:latest` for dev. Pin to a digest/tag for reproducibility if this
> graduates beyond local use (§9).

### 5.3 `water-quality-backend` — the twin backend

Unchanged build (`context: ..`, `dockerfile: backend/Dockerfile`); the Kafka-relevant
additions are:

- `depends_on: kafka (service_healthy)` — don't start before the broker is reachable.
- Env wiring (the bridge to the companion doc):
  ```ini
  AQUACAST_KAFKA_BOOTSTRAP_SERVERS: kafka:9092       # in-compose broker address
  AQUACAST_KAFKA_TOPIC: aquacast.water_quality
  ```
  These match the publish doc §6 keys. **`AQUACAST_KAFKA_ENABLED` is deliberately left
  unset (→ default `0`)**: until `kafka_publisher.py` exists, the backend ignores these
  and behaves exactly as before. Flipping `ENABLED=1` after the publisher lands turns the
  stream on with no topology change.

### 5.4 `Dockerfile` cleanup (done today)

The prior image **downloaded Kafka 3.7.1 binaries + a JRE** into the backend image (an
artifact of an earlier embedded-broker idea). With the broker now its own container and
the backend being pure-Python (`numpy` only), that was dead weight. Removed:

- `ARG KAFKA_VERSION` / `ARG SCALA_VERSION`
- `ENV KAFKA_HOME` and the `/opt/kafka/bin` `PATH` entry
- the entire `apt-get` layer: `ca-certificates`, `curl`, `default-jre-headless`, and the
  `curl … kafka_*.tgz | tar` download (the healthcheck uses Python `urllib`, not `curl`)

Result: smaller image, no build-time dependency on `archive.apache.org`, and a clean
responsibility split (broker = `kafka` container; backend = Kafka **client** only, once
the publisher adds `confluent-kafka` per the companion doc §11).

## 6. Configuration & Conventions

### 6.1 Data persistence — **ephemeral (confirmed decision)**

No named volumes. `docker compose down` clears all topics and offsets. Rationale: the
publish doc's producer is in-memory/best-effort anyway, the twin is reconstructable from
scenario config, and a named volume on `apache/kafka` (runs as uid 1000) invites
volume-ownership friction. Persistence is a documented future toggle (§9).

### 6.2 Security — none (PLAINTEXT)

All listeners are PLAINTEXT; no SASL/TLS. Matches the publish doc's "blank =
PLAINTEXT" default. SASL_SSL is a prod concern (§9) and maps onto the publish doc's
`AQUACAST_KAFKA_SECURITY_*` knobs when needed.

### 6.3 Networking

Single implicit `backend_default` network. Internal service-to-service uses Docker DNS
names (`kafka`, `water-quality-backend`); host access uses published ports
(8765/9092/29092/8080).

### 6.4 Topic creation

No pre-provisioning. `KAFKA_AUTO_CREATE_TOPICS_ENABLE=true` means `aquacast.water_quality`
springs into existence on the first produce once the publisher is live. (Pre-creating via
an init container is a future option — §9 / open question.)

## 7. Operations

```bash
cd backend
docker compose up -d --build     # build backend, pull kafka + ui, start all three
docker compose ps                # all three should be Up; kafka (healthy)
docker compose logs -f kafka     # broker logs
docker compose down              # stop all; ephemeral → topics gone
```

- Backend health: `curl http://localhost:8765/health` → `{"status":"ok",…}`
- Broker (host): bootstrap `localhost:29092`
- UI: `http://localhost:8080`
- **Name-collision caveat:** the `Makefile`'s `make run` starts a container also named
  `aquacast-wq-backend`. Running it **and** compose collides on the name. Use one or the
  other; if compose errors with a name conflict, `docker rm -f aquacast-wq-backend`
  first. (Observed and resolved during bring-up.)

## 8. Verification (performed 2026-06-01)

Evidence collected against the running stack, not asserted:

| Check | Result |
|---|---|
| `docker compose config` | valid |
| `docker build -f backend/Dockerfile` (post-cleanup) | builds OK |
| `docker compose ps` | `kafka` Up **(healthy)**, `kafka-ui` Up, `water-quality-backend` Up **(healthy)** |
| Backend `GET /health` | `{"status":"ok","service":"aquacast-water-quality"}` |
| Kafka produce→consume (`smoke.test`, console tools) | round-tripped `hello-aquacast`; topic then deleted |
| UI `GET /` | HTTP 200 |
| UI `GET /api/clusters` | cluster `aquacast` **ONLINE**, `brokerCount:1`, `version:4.2-IV1`, `controller:KRAFT` |
| User topics after smoke | none (only internal `__consumer_offsets`) — confirms **nothing publishes on startup** |

## 9. Forward Compatibility / Prod Gaps

- **Publisher turn-on:** add `kafka_publisher.py`/`kafka_payload.py` + `confluent-kafka`
  (companion doc), then set `AQUACAST_KAFKA_ENABLED=1` in `aquacast-backend.env` or the
  compose `environment:`. No topology change.
- **Persistence:** add a named volume + `KAFKA_LOG_DIRS`, handling uid-1000 ownership
  (init `chown` or `user:`), if topics must survive `down`.
- **Topic pre-creation:** a one-shot init service running `kafka-topics.sh --create`
  (partitions/retention tuned) instead of relying on auto-create.
- **Security:** switch listeners to SASL_SSL and populate the publish doc's
  `AQUACAST_KAFKA_SECURITY_PROTOCOL` / `SASL_*` for a managed/remote cluster.
- **HA:** multi-broker + replication factor ≥3 / `min.insync.replicas` ≥2; today's
  single-node `RF=1` has no durability.
- **Image pinning / limits:** pin `kafka-ui` (and consider pinning `apache/kafka` by
  digest); add `deploy.resources` limits before any shared/CI use.
- **External broker:** to target a managed cluster instead of the bundled one, drop the
  `kafka` service and point `AQUACAST_KAFKA_BOOTSTRAP_SERVERS` at it; `kafka-ui` and the
  backend wiring are otherwise unchanged.

## 10. Files Touched

| File | Change |
|---|---|
| `backend/docker-compose.yml` | added `kafka` (apache/kafka:4.2.1, KRaft) + `kafka-ui` (kafbat) services; added `depends_on` + `AQUACAST_KAFKA_*` env to `water-quality-backend` |
| `backend/Dockerfile` | removed bundled Kafka binaries + JRE + curl/ca-certificates download layer and the `KAFKA_HOME`/`PATH` env (§5.4) |

**Not touched:** any Kit/Omniverse file, `aquacast-backend.env` (the `AQUACAST_KAFKA_*`
block from the publish doc §6 is added at publisher-implementation time, not here),
`requirements.txt` (gets `confluent-kafka` with the publisher), `water_quality_backend.py`.

## 11. Open Questions

1. **Persistence:** keep ephemeral, or add a volume now so demo topics survive restarts?
   (Chosen: ephemeral for now — §6.1.)
2. **Pre-create `aquacast.water_quality`** via an init job, or keep relying on
   auto-create at first publish?
3. **UI exposure:** is `kafka-ui` on `8080` wanted in every environment, or
   dev/demo-only (drop it for headless/CI)?
4. **Single-stack coupling:** should the compose also carry a flipped
   `AQUACAST_KAFKA_ENABLED=1` once the publisher lands, or stay off-by-default so the
   stack is safe to run before the broker matters?
```
