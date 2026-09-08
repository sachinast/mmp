#!/usr/bin/env bash
# Prove the backup can be restored.
#
# A backup nobody has restored is a hypothesis. This dumps the database, restores
# it into a fresh one, and compares row counts and schema — so the answer to
# "can we recover?" is something someone has watched happen rather than something
# a runbook asserts.
#
# Run it against a real environment on a schedule. The drill is the deliverable;
# the dump file is a by-product.
#
#   ./infra/scripts/backup_restore_drill.sh mmp_dev
set -euo pipefail

SOURCE="${1:-mmp_dev}"
RESTORED="${SOURCE}_drill_$(date -u +%Y%m%d%H%M%S)"
DUMP="$(mktemp -t mmp-drill).dump"

cleanup() {
  rm -f "$DUMP"
  dropdb --if-exists "$RESTORED" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> Dumping $SOURCE"
START=$(date +%s)
# Custom format: parallel restore, and selective restore of a single table when
# someone deletes one row and needs it back without replaying everything.
pg_dump --format=custom --no-owner --no-privileges --file="$DUMP" "$SOURCE"
DUMP_SECONDS=$(( $(date +%s) - START ))
DUMP_SIZE=$(du -h "$DUMP" | cut -f1)
echo "    ${DUMP_SIZE} in ${DUMP_SECONDS}s"

echo "==> Restoring into $RESTORED"
createdb "$RESTORED"
START=$(date +%s)
# --jobs is what makes a restore of a large database finish in an outage window
# rather than a shift. Errors are surfaced rather than swallowed.
pg_restore --dbname="$RESTORED" --no-owner --no-privileges --jobs=4 "$DUMP"
RESTORE_SECONDS=$(( $(date +%s) - START ))
echo "    restored in ${RESTORE_SECONDS}s"

echo "==> Comparing"
FAILED=0

compare() {
  local label="$1" query="$2"
  local before after
  before=$(psql -tAX -d "$SOURCE" -c "$query")
  after=$(psql -tAX -d "$RESTORED" -c "$query")
  if [ "$before" = "$after" ]; then
    printf '    ok   %-22s %s\n' "$label" "$before"
  else
    printf '    FAIL %-22s source=%s restored=%s\n' "$label" "$before" "$after"
    FAILED=1
  fi
}

compare "tables"        "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
compare "indexes"       "SELECT count(*) FROM pg_indexes WHERE schemaname='public'"
compare "constraints"   "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public'"
compare "organizations" "SELECT count(*) FROM organizations"
compare "apps"          "SELECT count(*) FROM apps"
compare "events"        "SELECT count(*) FROM events"
compare "clicks"        "SELECT count(*) FROM clicks"
compare "attributions"  "SELECT count(*) FROM attributions"
compare "migration head" "SELECT version_num FROM alembic_version"

# Row-level security is not part of a data dump's happy path, and a restore that
# silently drops it would leave a database that works and leaks. Worth checking
# explicitly, every time.
compare "RLS tables"    "SELECT count(*) FROM pg_class WHERE relrowsecurity AND relnamespace='public'::regnamespace"
compare "RLS policies"  "SELECT count(*) FROM pg_policies WHERE schemaname='public'"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "==> PASS — dump ${DUMP_SECONDS}s, restore ${RESTORE_SECONDS}s, size ${DUMP_SIZE}"
  echo "    Record these timings: they are the recovery estimate, and they grow."
else
  echo "==> FAIL — the restore does not match the source. Do not rely on this backup."
  exit 1
fi
