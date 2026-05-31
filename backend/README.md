# Aquacast Water-Quality Backend

Local HTTP backend for the Aquacast water-quality computation model.

It owns the deterministic `WaterQualityModel` and exposes JSON endpoints. It does
not import Omniverse and does not write USD. Kit should still render and update
the stage on the main thread.

## Run Locally

```bash
python backend/water_quality_backend.py --env-file backend/aquacast-backend.env --host 127.0.0.1 --port 8765
```

Thermal/backend constants can be edited in `backend/aquacast-backend.env`. Individual model keys use
`AQUACAST_WQ_<UPPERCASE_KEY>`, for example `AQUACAST_WQ_TANK_RADIUS_M=1.2`.

## Docker

From the repository root:

```bash
docker build -f backend/Dockerfile -t aquacast-water-quality-backend:local .
docker run --rm --env-file backend/aquacast-backend.env -p 8765:8765 aquacast-water-quality-backend:local
```

In another shell:

```bash
python backend/smoke_test.py --url http://127.0.0.1:8765
```

Or:

```bash
docker compose -f backend/docker-compose.yml up --build
```

Make targets are also available:

```bash
cd backend
make build
make run
make smoke
```

## API

- `GET /health`
- `GET /snapshot`
- `GET /sensor?name=mixed_tank_outlet`
- `GET /sensors`
- `POST /advance` with `{"real_dt_s": 0.25}`; `temperature_c` is only a debug override
- `POST /action` with actions such as:
  - `{"type": "feed", "mass_kg": 1.0}`
  - `{"type": "set_biofilter", "enabled": false}`
  - `{"type": "set_water_exchange", "q_lph": 2000.0}`
  - `{"type": "set_heater", "power": 500.0}`
  - `{"type": "load_scenario", "name": "overfeed"}`
- `POST /particle-values` with `{"heat_weights": [...], "positions": [[x,y,z], ...]}`
- `POST /particles/register` with `{"positions": [[x,y,z], ...]}`
- `GET /particles/values`

## Kit Integration

Set in `global_variable.py`:

```python
WQ_BACKEND_ENABLED = True
WQ_BACKEND_URL = "http://127.0.0.1:8765"
```
