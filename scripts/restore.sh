#!/usr/bin/env bash
#
# Restore the SignLaw AI database from a pg_dump backup.
#
#   ./scripts/restore.sh backups/signlaw-20260804T120000Z-rev0003.dump
#   ./scripts/restore.sh --latest
#   ./scripts/restore.sh --latest --force        overwrite a populated database
#
# Two guards, both deliberate:
#
#   * A restore into a populated database is refused without --force. Recovery
#     usually happens under pressure, and "I meant the other environment" is a
#     mistake with no undo.
#   * Schema revisions are compared. Restoring a dump taken at one Alembic
#     revision into a database at another produces a corpus that queries but is
#     subtly wrong, which is worse than a failed restore.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
BACKUP_DIR="$ROOT_DIR/backups"
DUMP=""
FORCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --latest) DUMP="latest"; shift ;;
        --force)  FORCE=true; shift ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) DUMP="$1"; shift ;;
    esac
done

DIM=$'\033[2m'; BOLD=$'\033[1m'; RED=$'\033[31m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'

log()  { printf '%s==>%s %s\n' "$CYAN" "$RESET" "$*"; }
warn() { printf '%s!!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%sxx%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# --- configuration -----------------------------------------------------------
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a && source "$ENV_FILE" && set +a
fi

DB_HOST="${DB__HOST:-localhost}"
DB_PORT="${DB__PORT:-5432}"
DB_USER="${DB__USER:-signlaw}"
DB_NAME="${DB__NAME:-signlaw}"
export PGPASSWORD="${DB__PASSWORD:-signlaw}"

command -v pg_restore >/dev/null 2>&1 || die "pg_restore not found (postgresql-client)."

# --- locate the dump ---------------------------------------------------------
if [[ "$DUMP" == "latest" ]]; then
    DUMP="$(ls -1t "$BACKUP_DIR"/signlaw-*.dump 2>/dev/null | head -1)"
    [[ -n "$DUMP" ]] || die "No backups found in $BACKUP_DIR"
fi

[[ -n "$DUMP" ]] || die "Specify a dump file, or use --latest."
[[ -f "$DUMP" ]] || die "Not found: $DUMP"

pg_restore --list "$DUMP" >/dev/null 2>&1 || die "Unreadable dump: $DUMP"

# --- schema compatibility ----------------------------------------------------
DUMP_REVISION="$(basename "$DUMP" | grep -oE 'rev[0-9a-f]+' | sed 's/^rev//' || echo "unknown")"

CURRENT_REVISION="unknown"
if [[ -x "$BACKEND_DIR/.venv/bin/alembic" ]]; then
    CURRENT_REVISION="$(cd "$BACKEND_DIR" && .venv/bin/alembic current 2>/dev/null \
        | grep -oE '^[0-9a-f]+' | head -1)"
    CURRENT_REVISION="${CURRENT_REVISION:-unknown}"
fi

log "Dump    $DUMP"
log "Schema  dump=${DUMP_REVISION}  database=${CURRENT_REVISION}"

if [[ "$DUMP_REVISION" != "unknown" && "$CURRENT_REVISION" != "unknown" \
      && "$DUMP_REVISION" != "$CURRENT_REVISION" ]]; then
    warn "Schema revisions differ."
    warn "Restoring across revisions can produce a corpus that queries but is"
    warn "subtly wrong. Move the database to ${DUMP_REVISION} first:"
    warn "    cd backend && .venv/bin/alembic upgrade ${DUMP_REVISION}"
    $FORCE || die "Refusing. Re-run with --force only if you are certain."
fi

# --- refuse to clobber -------------------------------------------------------
EXISTING="$(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
    --dbname="$DB_NAME" --tuples-only --no-align \
    --command="SELECT count(*) FROM document" 2>/dev/null || echo "0")"

if [[ "${EXISTING:-0}" =~ ^[0-9]+$ ]] && (( EXISTING > 0 )); then
    warn "The target database already holds ${EXISTING} document(s)."
    warn "Restoring will DROP and replace them, including every retrieval trace."
    if ! $FORCE; then
        die "Refusing. Re-run with --force to overwrite."
    fi
    printf '\n%sOverwriting %s documents in %s@%s in 5 seconds. Ctrl-C to abort.%s\n' \
        "$RED" "$EXISTING" "$DB_NAME" "$DB_HOST" "$RESET"
    sleep 5
fi

# --- restore -----------------------------------------------------------------
# The vector extension must exist before restoring tables that use it; pg_dump
# does not always recreate extensions in a usable order.
log "Ensuring the vector extension exists"
psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
    --dbname="$DB_NAME" --quiet \
    --command="CREATE EXTENSION IF NOT EXISTS vector" >/dev/null 2>&1 \
    || warn "Could not create the vector extension — restore may fail."

log "Restoring"

JOBS="${RESTORE_JOBS:-4}"
if ! pg_restore \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --dbname="$DB_NAME" \
        --clean --if-exists \
        --no-owner --no-privileges \
        --jobs="$JOBS" \
        "$DUMP" 2>"$DUMP.restore.err"; then
    # pg_restore reports non-fatal noise on --clean when objects are absent, so
    # a non-zero exit is not automatically a failure. Verify instead of guessing.
    warn "pg_restore reported errors:"
    tail -20 "$DUMP.restore.err" >&2
    warn "Verifying whether the data landed anyway…"
fi
rm -f "$DUMP.restore.err"

# --- verify ------------------------------------------------------------------
log "Verifying"

COUNTS="$(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
    --dbname="$DB_NAME" --tuples-only --no-align --field-separator=' ' \
    --command="SELECT
        (SELECT count(*) FROM document),
        (SELECT count(*) FROM chunk),
        (SELECT count(*) FROM chat_message),
        (SELECT count(*) FROM embedding_collection WHERE status = 'active')" \
    2>/dev/null || echo "0 0 0 0")"
read -r DOCS CHUNKS TRACES ACTIVE <<< "$COUNTS"

printf '%s\n' "$(cat <<SUMMARY

${GREEN}Restore complete${RESET}
  documents           ${DOCS:-0}
  chunks              ${CHUNKS:-0}
  traces              ${TRACES:-0}
  active collections  ${ACTIVE:-0}
SUMMARY
)"

if [[ "${DOCS:-0}" == "0" ]]; then
    die "No documents restored. The dump may be empty or the restore failed."
fi

if [[ "${ACTIVE:-0}" == "0" ]]; then
    warn "No active embedding collection — retrieval will report index_not_ready."
    warn "Check: SELECT name, status FROM embedding_collection;"
fi

printf '\n%sConfirm the system works end to end:%s\n' "$BOLD" "$RESET"
printf '  %ssignlaw health%s\n' "$DIM" "$RESET"
printf '  %ssignlaw ask "What is the maximum fascia sign area?"%s\n\n' "$DIM" "$RESET"
