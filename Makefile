UV ?= uv

.PHONY: setup lock lock-check lint format typecheck test build check

setup:
	$(UV) sync --locked

# Dependency updates are explicit; setup and checks must not rewrite the lock.
lock:
	$(UV) lock

lock-check:
	$(UV) lock --check

lint:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

format:
	$(UV) run --locked ruff check --fix .
	$(UV) run --locked ruff format .

typecheck:
	$(UV) run --locked ty check

test:
	$(UV) run --locked pytest

build:
	$(UV) build --no-sources

check: lock-check lint typecheck test
