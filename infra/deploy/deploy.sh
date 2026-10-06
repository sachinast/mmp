#!/usr/bin/env bash
# Build and (re)start the stack on the server. Idempotent: safe to run on every
# deploy. Run from the repository root on the host, as root.
#
#   DOMAIN=mmp.adaptsmedia.info ./infra/deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="${DOMAIN:-mmp.adaptsmedia.info}"
COMPOSE=(docker compose -p mmp -f docker-compose.prod.yml --env-file secrets.env)
hex() { openssl rand -hex 32; }

# ------------------------------------------------------------- secrets (once)
if [ ! -f secrets.env ]; then
  umask 077
  cat > secrets.env <<EOF
PG_SUPERUSER_PASSWORD=$(hex)
PG_OWNER_PASSWORD=$(hex)
PG_API_PASSWORD=$(hex)
PG_TRACKER_PASSWORD=$(hex)
PG_WORKER_PASSWORD=$(hex)
EOF
fi
if [ ! -f .env.prod ]; then
  umask 077
  cat > .env.prod <<EOF
MMP_ENVIRONMENT=staging
MMP_LOG_LEVEL=info
MMP_LOG_JSON=true
MMP_REDIS_URL=redis://redis:6379/0
MMP_TRACKING_DOMAIN=https://${DOMAIN}
MMP_API_KEY_PEPPER=$(hex)
MMP_IP_HASH_PEPPER=$(hex)
MMP_SESSION_SECRET=$(hex)
EOF
fi
set -a; . ./secrets.env; set +a

# ------------------------------------------------------------- build + data
"${COMPOSE[@]}" build api
"${COMPOSE[@]}" up -d --wait postgres redis

psql_su() {
  local db="${1:-postgres}"; shift || true
  "${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d "$db" "$@"
}

# A non-superuser owner. The migrations create the application roles, so it
# needs CREATEROLE — but a superuser owner would let any role granted
# membership in it escalate to superuser.
psql_su <<SQL
SELECT 'CREATE ROLE mmp_owner LOGIN CREATEROLE'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mmp_owner')\gexec
ALTER ROLE mmp_owner WITH LOGIN CREATEROLE NOSUPERUSER PASSWORD '${PG_OWNER_PASSWORD}';
SELECT 'CREATE DATABASE mmp OWNER mmp_owner'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mmp')\gexec
SQL
psql_su mmp <<SQL
ALTER SCHEMA public OWNER TO mmp_owner;
SQL

"${COMPOSE[@]}" run --rm migrate

psql_su mmp <<SQL
ALTER ROLE mmp_api     WITH LOGIN PASSWORD '${PG_API_PASSWORD}';
ALTER ROLE mmp_tracker WITH LOGIN PASSWORD '${PG_TRACKER_PASSWORD}';
ALTER ROLE mmp_worker  WITH LOGIN PASSWORD '${PG_WORKER_PASSWORD}';
SQL

# Partition and rollup maintenance creates tables, which requires ownership of
# the parent. Membership in the (non-superuser) owner provides exactly that.
# PG16 makes a role's creator a member of it, and membership cannot be
# circular, so that implicit grant is dropped first. (A future migration that
# alters mmp_worker itself would then need to run as the superuser.)
if [ "$(psql_su mmp -tA -c "SELECT pg_has_role('mmp_worker','mmp_owner','MEMBER')" | tr -d '[:space:]')" != "t" ]; then
  psql_su mmp <<SQL
REVOKE mmp_worker FROM mmp_owner GRANTED BY postgres;
GRANT mmp_owner TO mmp_worker;
SQL
fi

# ------------------------------------------------------------- services
"${COMPOSE[@]}" up -d --wait api tracker web
"${COMPOSE[@]}" up -d worker
"${COMPOSE[@]}" ps
