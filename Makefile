.PHONY: install codegen codegen-check check check-service check-monitor check-python conformance build monitor test-service test-python format

export UV_CACHE_DIR ?= /tmp/tasklease-uv-cache

install:
	npm ci
	cd python && uv sync --locked

codegen:
	npm run codegen

codegen-check:
	npm run codegen:check

check: codegen-check check-service check-monitor check-python conformance

check-service:
	npm run format:check
	npm run check:examples
	npm run check:service
	npm run lint
	npm run test:service

check-monitor:
	npm run check:monitor
	npm run test:monitor

check-python:
	cd python && uv run ruff check --config pyproject.toml . ../conformance
	cd python && uv run ruff format --check --config pyproject.toml . ../conformance
	cd python && uv run mypy src tests hatch_build.py ../conformance
	cd python && uv run pytest

conformance:
	cd python && uv run pytest -c pyproject.toml ../conformance

build:
	cd python && uv build

monitor:
	node scripts/build-assets.mjs

test-service:
	npm run test:service

test-python:
	cd python && uv run pytest

format:
	npm run format
	cd python && uv run ruff format --config pyproject.toml . ../conformance
