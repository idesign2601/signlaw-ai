#!/usr/bin/env bash
#
# Milestone 1 — production validation.
#
# Runs the complete system against real BC bylaws and produces a report.
#
#   ./scripts/validate.sh
#
# Stages, in order. A failure at any stage stops the run, because a report
# built on a broken stage measures nothing:
#
#   1. docker builds
#   2. Postgres + pgvector, migrations applied
#   3. dependency health (Postgres, pgvector, embedding model, Ollama)
#   4. static gates (ruff, mypy, unit + e2e tests)
#   5. ingest the validation corpus
#   6. run the validation suite
#   7. write the report and spot-check worksheet
#
# Corpus: put the real bylaws in documents/bylaws/ before running.
#   burnaby_sign_bylaw.pdf
#   vancouver_sign_bylaw.pdf
#   surrey_sign_bylaw.pdf
#
# Download them from each municipality's bylaw page. The filename convention
# above helps metadata detection resolve the municipality when the cover page
# is ambiguous.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
VENV_BIN="$BACKEND_DIR/.venv/bin"
CORPUS_DIR="${CORPUS_DIR:-$ROOT_DIR/documents/bylaws}"
REPORT_DIR="${REPORT_DIR:-$ROOT_DIR/reports}"
STAMP="$(date +%Y%m%d-%H%M%S)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'

stage() { printf '\n%s━━━ %s%s\n' "$CYAN" "$1" "$RESET"; }
die()   { printf '\n%s✗ %s%s\n' "$RED" "$1" "$RESET" >&2; exit 1; }
ok()    { printf '%s✓ %s%s\n' "$GREEN" "$1" "$RESET"; }

mkdir -p "$REPORT_DIR"

# -----------------------------------------------------------------------------
stage "0/7  Preflight"
# -----------------------------------------------------------------------------
[[ -x "$VENV_BIN/python" ]] || die "Backend not installed. Run: make install"

expected=(burnaby vancouver surrey)
missing=()
for city in "${expected[@]}"; do
    if ! find "$CORPUS_DIR" -iname "*${city}*.pdf" 2>/dev/null | grep -q .; then
        missing+=("$city")
    fi
done

if (( ${#missing[@]} > 0 )); then
    die "Missing bylaws for: ${missing[*]}

Put the real PDFs in $CORPUS_DIR, named so the municipality is detectable:
  burnaby_sign_bylaw.pdf
  vancouver_sign_bylaw.pdf
  surrey_sign_bylaw.pdf

Download each from its municipality's bylaw page. This validation is
meaningless without the real documents."
fi
ok "corpus present in $CORPUS_DIR"

# -----------------------------------------------------------------------------
stage "1/7  Docker builds"
# -----------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
    (cd "$ROOT_DIR" && docker compose build) || die "Docker build failed."
    ok "images built"
else
    printf '%s! docker not installed — skipping image build%s\n' "$YELLOW" "$RESET"
fi

# -----------------------------------------------------------------------------
stage "2/7  Postgres + pgvector"
# -----------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
    (cd "$ROOT_DIR" && docker compose up -d postgres) || die "Postgres failed to start."
    printf '%s  waiting for Postgres…%s\n' "$DIM" "$RESET"
    for _ in {1..30}; do
        if (cd "$ROOT_DIR" && docker compose exec -T postgres pg_isready -q 2>/dev/null); then
            break
        fi
        sleep 2
    done
fi

(cd "$BACKEND_DIR" && "$VENV_BIN/alembic" upgrade head) || die "Migrations failed."
ok "schema at head"

# -----------------------------------------------------------------------------
stage "3/7  Dependency health"
# -----------------------------------------------------------------------------
"$VENV_BIN/signlaw" health || die "Dependencies are not ready — see fixes above."
ok "Postgres, pgvector, embedding model and Ollama all reachable"

# -----------------------------------------------------------------------------
stage "4/7  Static gates"
# -----------------------------------------------------------------------------
(cd "$BACKEND_DIR" && "$VENV_BIN/ruff" check app tests) || die "ruff failed."
(cd "$BACKEND_DIR" && "$VENV_BIN/mypy" app) || die "mypy failed."
(cd "$BACKEND_DIR" && "$VENV_BIN/pytest" tests/unit tests/e2e -q) || die "Tests failed."
ok "lint, types and tests pass"

# -----------------------------------------------------------------------------
stage "5/7  Ingesting the validation corpus"
# -----------------------------------------------------------------------------
INGEST_LOG="$REPORT_DIR/ingest-$STAMP.json"
"$VENV_BIN/signlaw" ingest "$CORPUS_DIR" --force --json > "$INGEST_LOG" \
    || die "Ingestion failed — see $INGEST_LOG"
ok "corpus indexed ($INGEST_LOG)"

# -----------------------------------------------------------------------------
stage "6/7  Running the validation suite"
# -----------------------------------------------------------------------------
REPORT_JSON="$REPORT_DIR/validation-$STAMP.json"
WORKSHEET="$REPORT_DIR/spot-check-$STAMP.md"

"$VENV_BIN/signlaw" validate --output "$REPORT_JSON" --worksheet "$WORKSHEET"
status=$?

# -----------------------------------------------------------------------------
stage "7/7  Report"
# -----------------------------------------------------------------------------
printf '  report      %s\n' "$REPORT_JSON"
printf '  worksheet   %s\n' "$WORKSHEET"
printf '  ingest log  %s\n\n' "$INGEST_LOG"

if (( status == 0 )); then
    printf '%s%s%s\n' "$GREEN" "Automatic thresholds passed." "$RESET"
else
    printf '%s%s%s\n' "$RED" "Automatic thresholds FAILED — see the report." "$RESET"
fi

printf '\n%s%s%s\n' "$BOLD" "Milestone 1 is not complete until:" "$RESET"
printf '  1. every automatic threshold passes\n'
printf '  2. the spot-check worksheet is filled in against the real bylaws\n'
printf '  3. no answer marked wrong carries HIGH confidence\n\n'
printf '%sStep 2 cannot be automated. It needs someone with the bylaws open.%s\n' \
    "$DIM" "$RESET"

exit $status
