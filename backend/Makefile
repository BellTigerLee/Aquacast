IMAGE ?= aquacast-water-quality-backend:local
PORT ?= 8765
HOST ?= 127.0.0.1
ENV_FILE ?= aquacast-backend.env

.PHONY: build run compose-up smoke local clean

build:
	cd .. && docker build -f backend/Dockerfile -t $(IMAGE) .

run:
	docker run --rm --name aquacast-wq-backend --env-file $(ENV_FILE) -p $(PORT):8765 $(IMAGE)

compose-up:
	docker compose -f docker-compose.yml up --build

smoke:
	python smoke_test.py --url http://$(HOST):$(PORT)

local:
	python water_quality_backend.py --env-file $(ENV_FILE) --host $(HOST) --port $(PORT)

clean:
	-docker rm -f aquacast-wq-backend
