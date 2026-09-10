# MMP — developer entrypoints. Every gate CI runs is runnable here first.
.DEFAULT_GOAL := help
PY := uv run
UVICORN := $(PY) uvicorn --reload --env-file .env

.PHONY: help setup check test lint fmt typecheck security audit sdk sdk-setup sdk-ios sdk-ios-device sdk-android \
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
check: lint typecheck security test sdk sdk-ios ## Every gate; CI adds the Android compile

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

SDK := sdks/react-native

sdk: ## Typecheck and test the React Native SDK
# Skipped rather than failed when node_modules is absent, so a Python-only
# checkout still runs `make check`.
#
# MMP_REQUIRE_SDK=1 turns that skip into a failure, and CI sets it. Otherwise a
# failed `npm ci` step would leave this reporting success and the SDK's tests
# would quietly stop running — the same trap as sdk-android, and the reason
# neither of them had been running in CI at all.
	@if [ -d $(SDK)/node_modules ]; then \
	  cd $(SDK) && npm run --silent typecheck && npm run --silent test; \
	elif [ -n "$$MMP_REQUIRE_SDK" ]; then \
	  echo "sdk: FAILED (node_modules absent)" >&2; exit 1; \
	else \
	  echo "sdk: skipped (run 'make sdk-setup' to install its dev dependencies)"; \
	fi

sdk-setup: ## Install the SDK's dev dependencies
	cd $(SDK) && npm install --no-audit --no-fund

sdk-ios-device: ## Run the iOS native core on a simulator and check its invariants
# Real execution, not a typecheck. This is what found that `uname` returns the
# host architecture on a simulator, so every simulator install was reporting a
# device model of "arm64" — a value that means nothing but looks like data.
#
# One shell block, like sdk-android: make gives each recipe line its own shell,
# so a skip in an earlier line cannot stop a later one.
	@device=$$(xcrun simctl list devices available 2>/dev/null \
	  | grep -m1 "iPhone" | grep -o "[0-9A-F-]\{36\}"); \
	if [ -z "$$device" ]; then \
	  echo "sdk-ios-device: skipped (no iOS simulator available)"; \
	else \
	  set -e; \
	  xcrun simctl bootstatus $$device -b >/dev/null 2>&1 || true; \
	  out=$$(mktemp -d); \
	  trap "rm -rf $$out" EXIT; \
	  xcrun --sdk iphonesimulator swiftc -target arm64-apple-ios16.0-simulator \
	    -o $$out/DeviceCheck \
	    $(SDK)/ios/MmpIdentifiers.swift $(SDK)/ios/DeviceCheck/main.swift; \
	  xcrun simctl spawn $$device $$out/DeviceCheck; \
	fi

sdk-android: ## Compile the Android native module
# One shell block on purpose: make runs each recipe line in its own shell, so an
# `exit 0` in an earlier line does not skip the later ones — which is exactly
# how the first version of this target ran gradle anyway on a machine that had
# deliberately been told it had no toolchain.
#
# Skipping keeps `make check` usable on a machine without an Android toolchain.
# In CI that is exactly wrong: a broken toolchain step would leave this target
# quietly reporting success and nothing would ever compile the Kotlin. Set
# MMP_REQUIRE_ANDROID=1 there and a missing toolchain fails instead.
	@sdk="$${ANDROID_HOME:-$${ANDROID_SDK_ROOT:-$$HOME/Library/Android/sdk}}"; \
	missing=""; \
	if [ -z "$$JAVA_HOME" ] || [ ! -x "$$JAVA_HOME/bin/java" ]; then \
	  missing="set JAVA_HOME to a JDK 17-21 (Gradle cannot run on 25)"; \
	elif [ ! -d "$$sdk" ]; then \
	  missing="no Android SDK; set ANDROID_HOME"; \
	elif ! command -v gradle >/dev/null 2>&1; then \
	  missing="gradle not on PATH"; \
	fi; \
	if [ -n "$$missing" ]; then \
	  if [ -n "$$MMP_REQUIRE_ANDROID" ]; then \
	    echo "sdk-android: FAILED ($$missing)" >&2; exit 1; \
	  fi; \
	  echo "sdk-android: skipped ($$missing)"; \
	else \
	  cd $(SDK)/android && \
	  echo "sdk.dir=$$sdk" > local.properties && \
	  gradle --console=plain compileReleaseKotlin lintRelease; \
	fi

sdk-ios: ## Typecheck the iOS native core against the real iOS SDK
# Only the core, which has no React dependency — the bridge shim needs React
# headers that exist only after a pod install. Skipped where Xcode is absent.
	@if xcrun --sdk iphoneos --show-sdk-path >/dev/null 2>&1; then \
	  xcrun --sdk iphoneos swiftc -typecheck -target arm64-apple-ios13.4 \
	    $(SDK)/ios/MmpIdentifiers.swift && echo "sdk-ios: typecheck clean"; \
	else \
	  echo "sdk-ios: skipped (no iOS SDK on this machine)"; \
	fi

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
