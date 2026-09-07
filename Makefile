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
