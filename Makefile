.PHONY: install codegen codegen-check check check-service check-monitor check-python test-service test-python format

export UV_CACHE_DIR ?= /tmp/tasklease-uv-cache

install:
	npm ci
	cd python && uv sync --locked

codegen:
	npm run codegen

codegen-check:
	npm run codegen:check

check: codegen-check check-service check-monitor check-python

check-service:
	npm run format:check
	npm run check:examples
	npm run check:service
	npm run lint
	npm run test:service

check-monitor:
	npm run check:monitor

check-python:
	cd python && uv run ruff check .
	cd python && uv run ruff format --check .
	cd python && uv run mypy src tests
	cd python && uv run pytest

test-service:
	npm run test:service

test-python:
	cd python && uv run pytest

format:
	npm run format
	cd python && uv run ruff format .
