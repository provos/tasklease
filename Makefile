.PHONY: install dependencies codegen codegen-check check check-service check-monitor check-python conformance build release monitor test-service test-python format

export UV_CACHE_DIR ?= /tmp/tasklease-uv-cache

install:
	node scripts/dependency-policy.mjs
	npm ci
	npm run check:dependencies
	cd python && uv sync --locked

dependencies:
	npm run check:dependencies
	npm run test:dependency-policy

codegen:
	npm run codegen

codegen-check:
	npm run codegen:check

check: dependencies codegen-check check-service check-monitor check-python conformance

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
	cd python && uv run ruff check --config pyproject.toml . ../conformance ../scripts
	cd python && uv run ruff format --check --config pyproject.toml . ../conformance ../scripts
	cd python && uv run mypy src tests hatch_build.py ../conformance ../scripts
	cd python && uv run pytest -c pyproject.toml tests ../scripts/test_acceptance_tools.py

conformance:
	cd python && uv run pytest -c pyproject.toml ../conformance

build:
	cd python && uv build

# Explicit publication command. Ordinary checks/builds never upload packages.
release: check build
	cd python && tasklease_release_version=$$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])') && uv publish "dist/tasklease-$${tasklease_release_version}-py3-none-any.whl" "dist/tasklease-$${tasklease_release_version}.tar.gz"

monitor:
	node scripts/build-assets.mjs

test-service:
	npm run test:service

test-python:
	cd python && uv run pytest -c pyproject.toml tests ../scripts/test_acceptance_tools.py

format:
	npm run format
	cd python && uv run ruff format --config pyproject.toml . ../conformance ../scripts
