.PHONY: install dev test test-fast run docker-build docker-up docker-logs lint

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

install:
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt

dev:
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements-dev.txt

test: dev
	$(PY) -m pytest -q

test-fast: dev
	$(PY) -m pytest -q -m "not slow"

run:
	$(VENV)/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-logs:
	docker compose logs -f bridge
