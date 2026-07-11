# Whisp developer commands. Run inside WSL2 / Linux.
# The web/API venv lives in ./.venv ; the worker installs heavy deps separately.

PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
RUFF ?= .venv/bin/ruff
MYPY ?= .venv/bin/mypy

.PHONY: help venv install install-worker dev worker test test-worker test-all \
        lint fmt format typecheck check clean

help:
	@echo "Whisp make targets:"
	@echo "  make venv           - create ./.venv"
	@echo "  make install        - install web/API + dev deps into ./.venv"
	@echo "  make install-worker - install worker deps (heavy; run in WSL2)"
	@echo "  make dev            - run the FastAPI dev server (uvicorn) on :8000"
	@echo "  make worker         - run the transcription worker (from worker/)"
	@echo "  make test           - run the API/contract test suite"
	@echo "  make test-worker    - run the worker test suite"
	@echo "  make test-all       - run both test suites"
	@echo "  make lint           - ruff check"
	@echo "  make fmt            - ruff format (write)"
	@echo "  make typecheck      - mypy (whisp_api + main)"
	@echo "  make check          - lint + format-check + typecheck + all tests"

venv:
	python3 -m venv .venv

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

install-worker:
	cd worker && pip install -r requirements.txt

dev:
	$(PY) -m uvicorn main:app --reload --host 0.0.0.0 --port 8000

worker:
	cd worker && python run_worker.py

test:
	$(PY) -m pytest tests/ -q

test-worker:
	cd worker && ../$(PY) -m pytest -q

test-all: test test-worker

lint:
	$(RUFF) check .

fmt format:
	$(RUFF) format .

typecheck:
	$(MYPY)

check:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY)
	$(MAKE) test
	$(MAKE) test-worker
