.PHONY: help install dev test test-pg lint format typecheck migrate migration seed seed-demo audit smoke run-bot run-api run-worker up down clean

PY := .venv/bin/python
PIP := .venv/bin/pip

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install runtime + dev dependencies
	python3 -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -r requirements-dev.txt

test: ## Run the test suite (SQLite; PostgreSQL tests are skipped)
	$(PY) -m pytest tests -q

test-pg: ## Run everything including the PostgreSQL concurrency tests
	TEST_DATABASE_URL=$${TEST_DATABASE_URL:-postgresql+asyncpg://postgres:postgres@localhost:5432/commerce_test} \
	$(PY) -m pytest tests -q

lint: ## Lint with ruff
	.venv/bin/ruff check app tests scripts

format: ## Auto-format and fix lint issues
	.venv/bin/ruff format app tests scripts
	.venv/bin/ruff check --fix app tests scripts

typecheck: ## Static type check
	.venv/bin/mypy app --ignore-missing-imports

migrate: ## Apply migrations
	.venv/bin/alembic upgrade head

migration: ## Autogenerate a migration: make migration m="add x"
	.venv/bin/alembic revision --autogenerate -m "$(m)"

audit: ## Run the financial integrity audit against the configured database
	$(PY) -m scripts.audit_financial

smoke: ## Smoke test a running deployment: make smoke KEY=rt_live_... [URL=...]
	$(PY) -m scripts.smoke_test --base-url $${URL:-http://localhost:8000} --api-key $(KEY)

seed: ## Seed roles, providers and payment methods
	$(PY) -m scripts.seed

seed-demo: ## Seed including sample catalog data (development only)
	$(PY) -m scripts.seed --demo

run-bot: ## Run the Telegram bot
	$(PY) -m app.main bot

run-api: ## Run the reseller API
	$(PY) -m app.main api

run-worker: ## Run the background workers
	$(PY) -m app.main worker

up: ## Bring up the full local stack
	docker compose up -d --build

down: ## Tear down the local stack
	docker compose down

clean: ## Remove caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
