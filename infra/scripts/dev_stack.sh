#!/usr/bin/env bash
# Run the API and the dashboard together for local browsing.
#
# Two processes because that is how they run in production: the dashboard talks
# to the API over HTTP with the user's own session cookie, and running them as
# one process would let a mistake in the web layer reach the database directly.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a

uv run uvicorn mmp_api.app:app --port 8002 --log-level warning &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if curl -sf http://127.0.0.1:8002/health >/dev/null 2>&1; then break; fi
  sleep 0.2
done

MMP_API_BASE_URL=http://127.0.0.1:8002 \
  exec uv run uvicorn mmp_web.app:app --port 8003 --log-level warning
