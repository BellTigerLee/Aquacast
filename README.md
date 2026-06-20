# Aquacast

Aquacast is an Omniverse Kit-based aquaculture digital twin for simulating fish tanks, water-quality dynamics, thermal behavior, operator controls, and AI-assisted response workflows. The main runtime is implemented as the `aquacast.aquacast_composer_extensions` extension and is launched through the Aquacast Kit app profile.

## Core Features

- Omniverse tank visualization with fish, water volumes, sensors, actuators, and thermal particle feedback.
- Deterministic water-quality model for temperature, dissolved oxygen, TAN, NH3, CO2, pH, alkalinity, salinity, turbidity, nitrite, and nitrate.
- Per-tank simulation state so selected tanks can run independent scenarios.
- One-click beginner demo scenarios for common operating situations.
- Tank control panel for thermal, feeding, inflow, filtration, emergency, and scenario actions.
- Sensor overview panel for live tank readings and actuator state indicators.
- Metrics dashboard with healthy, warn, and critical bands for operator monitoring.
- Local LLM panel for beginner or expert explanations, RAG/SQLite context, and action proposal review.
- Automatic warn/critical alert detection that can generate AI proposals requiring operator confirmation.
- Water-quality backend with HTTP endpoints, SQLite history, smoke tests, and Docker Compose support.
- Dashboard integration through the Salmon Twin dashboard backend for AI proposal storage and confirmation workflows.

## Operator Experience

Aquacast supports two operator profiles.

- `beginner`: Uses simpler wording, fewer high-priority metrics, guided explanations, and one-click scenario labels such as `Normal State`, `Too Much Feed`, `Pump Off`, `Filter Failure`, and `Water Too Hot`.
- `expert`: Keeps the full technical context, threshold bands, trend deltas, actuator state, and RAG/SQLite evidence visible for deeper analysis.

The default mode is `beginner`, configured in `extensions/aquacast.aquacast_composer_extensions/global_variable.py` with `AQUACAST_OPERATOR_LEVEL = "beginner"`.

## Main UI Panels

- `Aquacast Tank Controls`: Select a tank and apply one-click scenarios or advanced control actions.
- `Aquacast First Steps`: Startup tutorial panel that guides new users through tank selection, scenario execution, metrics, and AI proposals.
- `Aquacast Sensor Overview`: Live sensor readings and actuator status for the selected tank.
- `Aquacast Metrics Dashboard`: Trend panels with health bands and threshold status.
- `Aquacast Actuator Overview`: Quick visual state of inlet, outlet, biofilter, mechanical filter, and heater.
- `Aquacast Local LLM Panel`: Local AI assistant and proposal inbox with Confirm/Reject workflow.

## Demo Scenarios

The one-click demo scenarios are designed for fast first-time exploration.

- `Normal State`: Safe baseline water-quality conditions.
- `Too Much Feed`: Extra feed increases organic load, turbidity, and ammonia risk.
- `Pump Off`: Water exchange stops, creating reduced circulation and quality drift.
- `Filter Failure`: Biofilter is disabled so ammonia can rise.
- `Water Too Hot`: Temperature jumps into a critical range.

Scenario definitions are stored in `extensions/aquacast.aquacast_composer_extensions/data/wq_scenarios.json`.

## AI-Assisted Operations

Aquacast can detect warn/critical threshold violations and request AI-generated operator proposals. The generated proposal includes the measured value, threshold condition, severity, tank target, and evidence snapshot. Operators must confirm or reject the proposal before actions are applied.

Duplicate same-event proposals are suppressed for a cooldown window, while severity changes such as `warn -> critical` are treated as new events.

## Project Layout

- `extensions/aquacast.aquacast_composer_extensions/`: Main Omniverse extension and runtime Python modules.
- `extensions/aquacast.aquacast_composer_extensions/main.py`: Runtime orchestration for fish, thermal, water quality, controls, and visuals.
- `extensions/aquacast.aquacast_composer_extensions/aquacast/aquacast_composer_extensions/extension.py`: Kit UI windows and extension startup logic.
- `extensions/aquacast.aquacast_composer_extensions/water_quality_model.py`: Omniverse-free deterministic water-quality model.
- `extensions/aquacast.aquacast_composer_extensions/water_quality_bands.py`: Shared healthy/warn/critical threshold band logic.
- `backend/`: Optional HTTP backend for water-quality simulation, history, thresholds, and smoke testing.
- `start_aquacast.sh`: Launch helper for composer or streaming modes.

## Run Aquacast

Launch the Composer profile:

```bash
./start_aquacast.sh --composer
```

Launch the streaming profile:

```bash
./start_aquacast.sh --streaming
```

Run the local water-quality backend:

```bash
cd backend
python3 water_quality_backend.py --env-file aquacast-backend.env --host 127.0.0.1 --port 8765
```

Run the backend with Docker Compose:

```bash
docker compose -f backend/docker-compose.yml up --build
```

## Tests

Run Omniverse-free Aquacast tests:

```bash
python3 -m pytest extensions/aquacast.aquacast_composer_extensions/tests -q
```

Run backend tests:

```bash
python3 -m pytest backend/tests -q
```

Kit-hosted tests must run through the Kit app host, not plain Python, because `carb`, `omni.*`, and `pxr` modules require the Kit runtime.

## Notes

- The model uses a salmon/RAS-style default startup temperature of `10.5C`.
- UI text intended for Omniverse panels is kept in English/ASCII to avoid font rendering issues.
- Generated local artifacts such as SQLite files, CSV exports, and streamer logs should be regenerated rather than manually edited.
