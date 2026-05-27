FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AQUACAST_BACKEND_HOST=0.0.0.0 \
    AQUACAST_BACKEND_PORT=8765

WORKDIR /app

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY backend /app/backend
COPY extensions/aquacast.aquacast_composer_extensions/water_quality_dynamics.py /app/extensions/aquacast.aquacast_composer_extensions/
COPY extensions/aquacast.aquacast_composer_extensions/water_quality_model.py /app/extensions/aquacast.aquacast_composer_extensions/
COPY extensions/aquacast.aquacast_composer_extensions/thermal_dynamics.py /app/extensions/aquacast.aquacast_composer_extensions/
COPY extensions/aquacast.aquacast_composer_extensions/data/wq_constants.json /app/extensions/aquacast.aquacast_composer_extensions/data/
COPY extensions/aquacast.aquacast_composer_extensions/data/wq_feed_rate.json /app/extensions/aquacast.aquacast_composer_extensions/data/
COPY extensions/aquacast.aquacast_composer_extensions/data/wq_scenarios.json /app/extensions/aquacast.aquacast_composer_extensions/data/

EXPOSE 8765

HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8765/health', timeout=2).read()"

CMD ["python", "backend/water_quality_backend.py"]
