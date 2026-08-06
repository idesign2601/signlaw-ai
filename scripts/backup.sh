#!/usr/bin/env bash
#
# Back up the SignLaw AI database.
#
#   ./scripts/backup.sh                    write to backups/
#   ./scripts/backup.sh --output /path     write elsewhere
#   ./scripts/backup.sh --keep 10          retention (default 7)
#
# Postgres holds everything that cannot be regenerated: documents, the section
# tree, chunks, embeddings, amendment lineage, and the retrieval traces that let
# a disputed answer be reconstructed. On ephemeral GPU hosting this is the only
# data class whose loss is unrecoverable, so the dump is verified after writing
# rather than assumed good.
#
# The dump is stamped with the Alembic revision it came from. Restoring a dump
# into a database at a different schema version is the most likely way to
# corrupt a recovery, and the stamp is what makes restore.sh able to refuse.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
OUTPUT_DIR="$ROOT_DIR/backups"
KEEP=7

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output) OUTPUT_DIR="$2"; shift 2 ;;
        --keep)   KEEP="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'

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

# pg_dump, psql and pg_restore are all needed: the dump is verified and its row
# counts reported, and restore.sh needs the same toolchain. Install them
# together rather than discovering the gap halfway through a recovery.
#
# The client major version must be >= the server's (16 in docker-compose.yml);
# an older client refuses a newer server's dump format.
for tool in pg_dump pg_restore psql; do
    command -v "$tool" >/dev/null 2>&1 || die \
        "$tool not found. Install the Postgres client tools:

  Debian/Ubuntu:  sudo apt-get install -y postgresql-client-16
  macOS:          brew install libpq && brew link --force libpq

Alternatively dump from inside the container, though the dump then lands in
the container filesystem and the verification below is skipped:

  docker compose exec -T postgres pg_dump -U $DB_USER -Fc $DB_NAME > backup.dump"
done

mkdir -p "$OUTPUT_DIR"

# --- schema version ----------------------------------------------------------
# Recorded in the filename so a restore can check compatibility before touching
# anything.
REVISION="unknown"
if [[ -x "$BACKEND_DIR/.venv/bin/alembic" ]]; then
    REVISION="$(cd "$BACKEND_DIR" && .venv/bin/alembic current 2>/dev/null \
        | grep -oE '^[0-9a-f]+' | head -1)"
    REVISION="${REVISION:-unknown}"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$OUTPUT_DIR/signlaw-${STAMP}-rev${REVISION}.dump"

# --- dump --------------------------------------------------------------------
log "Backing up ${DB_NAME}@${DB_HOST}:${DB_PORT} (schema ${REVISION})"

# Custom format: compressed, and pg_restore can list contents, restore
# selectively, and run in parallel. Plain SQL offers none of that.
if ! pg_dump \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --dbname="$DB_NAME" \
        --format=custom \
        --compress=6 \
        --no-owner \
        --no-privileges \
        --file="$TARGET" 2>"$TARGET.err"; then
    warn "$(cat "$TARGET.err")"
    rm -f "$TARGET" "$TARGET.err"
    die "pg_dump failed."
fi
rm -f "$TARGET.err"

# --- verify ------------------------------------------------------------------
# A dump that cannot be read is worse than no dump, because it looks like
# protection. Verify before reporting success.
log "Verifying"

if ! pg_restore --list "$TARGET" >/dev/null 2>&1; then
    rm -f "$TARGET"
    die "The dump is unreadable and has been deleted."
fi

TABLES="$(pg_restore --list "$TARGET" | grep -c 'TABLE DATA' || true)"
SIZE="$(du -h "$TARGET" | cut -f1)"

if (( TABLES < 5 )); then
    warn "Only ${TABLES} tables carry data. Expected 10+ for a populated corpus."
    warn "If the database is genuinely empty this is fine; otherwise investigate."
fi

# Row counts for the tables whose loss actually hurts.
COUNTS="$(psql --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" \
    --dbname="$DB_NAME" --tuples-only --no-align --field-separator=' ' \
    --command="SELECT
        (SELECT count(*) FROM document),
        (SELECT count(*) FROM chunk),
        (SELECT count(*) FROM chat_message)" 2>/dev/null || echo "? ? ?")"
read -r DOCS CHUNKS TRACES <<< "$COUNTS"

printf '%s\n' "$(cat <<SUMMARY
${GREEN}Backup complete${RESET}
  file       $TARGET
  size       $SIZE
  schema     $REVISION
  documents  ${DOCS:-?}
  chunks     ${CHUNKS:-?}
  traces     ${TRACES:-?}
SUMMARY
)"

# --- retention ---------------------------------------------------------------
mapfile -t OLD < <(ls -1t "$OUTPUT_DIR"/signlaw-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))")
if (( ${#OLD[@]} > 0 )); then
    log "Removing ${#OLD[@]} backup(s) beyond the last $KEEP"
    rm -f "${OLD[@]}"
fi

# --- the part that actually matters ------------------------------------------
cat <<REMINDER

${YELLOW}This backup is still on the same machine as the database.${RESET}
On ephemeral hosting that protects against nothing. Copy it off:

  ${DIM}aws s3 cp "$TARGET" s3://your-bucket/signlaw/${RESET}
  ${DIM}rclone copy "$TARGET" remote:signlaw/${RESET}
  ${DIM}scp "$TARGET" you@persistent-host:/backups/${RESET}

REMINDER
