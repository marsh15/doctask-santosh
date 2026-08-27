.PHONY: install test test-postgres web-build dev compose-up compose-down

install:
	uv sync --all-extras
	npm --prefix apps/web ci

test:
	uv run pytest

test-postgres:
	TEST_DATABASE_URL="$${DATABASE_URL}" uv run pytest

web-build:
	npm --prefix apps/web run build

dev:
	uv run uvicorn doctask.main:app --reload

compose-up:
	docker compose up --build

compose-down:
	docker compose down
