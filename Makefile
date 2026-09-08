# MMP — developer entrypoints. Every gate CI runs is runnable here first.
.DEFAULT_GOAL := help
PY := uv run
UVICORN := $(PY) uvicorn --reload --env-file .env

.PHONY: help setup check test lint fmt typecheck security audit \
        tracker api web worker db-create db-reset infra-up infra-down

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | \
	 awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install the workspace
	uv sync
	@test -f .env || (echo "!! No .env — copy .env.example and fill the peppers" && exit 1)

# ---------------------------------------------------------------- services
tracker: ## Run the tracking edge on :8001
	$(UVICORN) mmp_tracker.app:app --port 8001

api: ## Run the business API on :8002
	$(UVICORN) mmp_api.app:app --port 8002

web: ## Run the dashboard on :8003
	$(UVICORN) mmp_web.app:app --port 8003

worker: ## Run the background worker
	set -a; . ./.env; set +a; $(PY) python -m mmp_worker.main

# ---------------------------------------------------------------- quality
check: lint typecheck security test ## Everything CI runs

lint: ## Ruff lint + format check
	$(PY) ruff check .
	$(PY) ruff format --check .

fmt: ## Autofix and format
	$(PY) ruff check --fix .
	$(PY) ruff format .

typecheck: ## mypy --strict
	$(PY) mypy packages services

security: ## Static security scan
	$(PY) bandit -q -r packages services

audit: ## Dependency CVE scan against the lockfile
	uv export --no-emit-workspace --no-dev --format requirements-txt -q -o .audit-requirements.txt
	$(PY) pip-audit -r .audit-requirements.txt --strict
	@rm -f .audit-requirements.txt

test: ## Run the test suite
	$(PY) pytest

# ---------------------------------------------------------------- data
db-create: ## Create the local development database
	createdb mmp_dev || true
	createdb mmp_test || true

db-reset: ## Drop and recreate the development database
	dropdb --if-exists mmp_dev && createdb mmp_dev

infra-up: ## Start Postgres and Redis in containers (alternative to brew services)
	docker compose -f infra/compose/docker-compose.yml up -d

infra-down: ## Stop the containers
	docker compose -f infra/compose/docker-compose.yml down

# ---------------------------------------------------------------- migrations
.PHONY: migrate migrate-down migration dev-roles

migrate: ## Apply all migrations
	cd packages/mmp_db && set -a && [ -f ../../.env ] && . ../../.env; set +a; uv run alembic upgrade head

migrate-down: ## Roll back one migration
	cd packages/mmp_db && set -a && . ../../.env && set +a && uv run alembic downgrade -1

migration: ## Autogenerate a migration: make migration m="add widgets"
	cd packages/mmp_db && set -a && . ../../.env && set +a && \
	  uv run alembic revision --autogenerate -m "$(m)"

dev-roles: ## Give the application roles a login for local development
	psql -d mmp_dev -f infra/scripts/bootstrap_dev_roles.sql
	psql -d mmp_test -f infra/scripts/bootstrap_dev_roles.sql

# ---------------------------------------------------------------- load
.PHONY: bench bench-baseline

bench: ## Latency regression gate against the recorded baseline
	$(PY) python infra/load/ingest_benchmark.py --requests 800 --batch 20 --concurrency 8

bench-baseline: ## Re-record the latency baseline
	$(PY) python infra/load/ingest_benchmark.py --requests 800 --batch 20 \
	  --concurrency 8 --update-baseline

.PHONY: dev-stack
dev-stack: ## Run the API and the dashboard together (for local browsing)
	./infra/scripts/dev_stack.sh

.PHONY: backup-drill
backup-drill: ## Dump, restore into a scratch database, and compare
	./infra/scripts/backup_restore_drill.sh mmp_dev
