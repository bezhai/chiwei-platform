#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB_DIR="$ROOT_DIR/scripts/db"

usage() {
  cat <<'USAGE'
Usage:
  DATABASE_URL=postgres://user:pass@host:5432/db scripts/db/run-common-layer-migration.sh [schema|backfill|verify|all] [backfill options]

Alternatively set PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD.

This runner connects directly to PostgreSQL. It never uses kubectl, pods,
services, or cluster-local port-forwarding.

Backfill options are forwarded to 002-backfill-common-layer.mjs:
  --dry-run              Default. Run transactions and roll back.
  --apply                Commit the backfill.
  --batch-size <n>       conversation_messages batch size, default 1000.
  --limit <n>            Rehearsal limit.
USAGE
}

mode="${1:-all}"
shift || true
case "$mode" in
  schema|backfill|verify|all) ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required" >&2
  exit 1
fi
if ! command -v bun >/dev/null 2>&1; then
  echo "bun is required" >&2
  exit 1
fi

run_sql() {
  local file="$1"
  echo "==> running ${file#$ROOT_DIR/}"
  psql --set ON_ERROR_STOP=on -f "$file"
}

run_backfill() {
  echo "==> running scripts/db/002-backfill-common-layer.mjs $*"
  bun "$DB_DIR/002-backfill-common-layer.mjs" "$@"
}

if [[ -z "${DATABASE_URL:-}" ]]; then
  : "${PGHOST:?PGHOST or DATABASE_URL is required}"
  : "${PGDATABASE:?PGDATABASE or DATABASE_URL is required}"
  : "${PGUSER:?PGUSER or DATABASE_URL is required}"
fi

case "$mode" in
  schema)
    run_sql "$DB_DIR/001-common-layer-schema.sql"
    ;;
  backfill)
    run_backfill "$@"
    ;;
  verify)
    run_sql "$DB_DIR/003-verify-common-layer.sql"
    ;;
  all)
    run_sql "$DB_DIR/001-common-layer-schema.sql"
    run_backfill "$@"
    run_sql "$DB_DIR/003-verify-common-layer.sql"
    ;;
esac
