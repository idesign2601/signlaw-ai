#!/usr/bin/env bash
#
# Full local verification for SignLaw AI.
#
#   ./scripts/verify.sh            run everything
#   ./scripts/verify.sh --no-docker   skip the image builds (fastest)
#   ./scripts/verify.sh --no-db       skip anything needing Postgres
#
# Steps, in order:
#   1. docker compose build
#   2. database migrations: upgrade -> downgrade -> upgrade
#   3. ruff check + ruff format --check
#   4. mypy --strict
#   5. pytest (unit + e2e)
#   6. pytest (integration)          [requires Postgres]
#
# Every step runs even if an earlier one fails, so a single run tells you
# everything that is broken rather than only the first thing.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
VENV_BIN="$BACKEND_DIR/.venv/bin"

RUN_DOCKER=true
RUN_DB=true

for arg in "$@"; do
    case "$arg" in
        --no-docker) RUN_DOCKER=false ;;
        --no-db)     RUN_DB=false ;;
        -h|--help)   sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)           printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'

declare -a STEP_NAMES=()
declare -a STEP_RESULTS=()
FAILURES=0

step() {
    local name="$1"; shift
    printf '\n%s==> %s%s\n' "$CYAN" "$name" "$RESET"

    if "$@"; then
        STEP_NAMES+=("$name"); STEP_RESULTS+=("pass")
        printf '%s    passed%s\n' "$GREEN" "$RESET"
    else
        local code=$?
        STEP_NAMES+=("$name"); STEP_RESULTS+=("FAIL")
        FAILURES=$((FAILURES + 1))
        printf '%s    failed (exit %d)%s\n' "$RED" "$code" "$RESET"
    fi
}

skip() {
    STEP_NAMES+=("$1"); STEP_RESULTS+=("skip")
    printf '\n%s==> %s%s\n%s    skipped: %s%s\n' \
        "$CYAN" "$1" "$RESET" "$YELLOW" "$2" "$RESET"
}

require_venv() {
    if [[ ! -x "$VENV_BIN/python" ]]; then
        printf '%sThe backend virtualenv is missing. Run: make install%s\n' "$RED" "$RESET" >&2
        exit 1
    fi
}

# -----------------------------------------------------------------------------
# 1. Docker images
# -----------------------------------------------------------------------------
if $RUN_DOCKER; then
    if command -v docker >/dev/null 2>&1; then
        step "docker compose build" bash -c "cd '$ROOT_DIR' && docker compose build"
    else
        skip "docker compose build" "docker is not installed"
    fi
else
    skip "docker compose build" "--no-docker"
fi

# -----------------------------------------------------------------------------
# 2. Migrations
# -----------------------------------------------------------------------------
migrations_roundtrip() {
    cd "$BACKEND_DIR" || return 1
    # A migration that cannot be rolled back cannot be safely deployed.
    "$VENV_BIN/alembic" upgrade head \
        && "$VENV_BIN/alembic" downgrade base \
        && "$VENV_BIN/alembic" upgrade head \
        && "$VENV_BIN/alembic" check
}

require_venv

if $RUN_DB; then
    step "database migrations (up -> down -> up -> check)" migrations_roundtrip
else
    skip "database migrations" "--no-db"
fi

# -----------------------------------------------------------------------------
# 3-5. Static analysis and tests
# -----------------------------------------------------------------------------
step "ruff check" bash -c "cd '$BACKEND_DIR' && '$VENV_BIN/ruff' check app tests"
step "ruff format --check" bash -c "cd '$BACKEND_DIR' && '$VENV_BIN/ruff' format --check app tests"
step "mypy --strict" bash -c "cd '$BACKEND_DIR' && '$VENV_BIN/mypy' app"
step "pytest (unit + e2e)" bash -c "cd '$BACKEND_DIR' && '$VENV_BIN/pytest' tests/unit tests/e2e -q"

if $RUN_DB; then
    step "pytest (integration)" bash -c "cd '$BACKEND_DIR' && '$VENV_BIN/pytest' tests/integration -q"
else
    skip "pytest (integration)" "--no-db"
fi

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
printf '\n%s%s\n' "$BOLD" "Verification summary"
printf '%s\n' "--------------------------------------------------------"

for index in "${!STEP_NAMES[@]}"; do
    result="${STEP_RESULTS[$index]}"
    case "$result" in
        pass) colour="$GREEN" ;;
        FAIL) colour="$RED" ;;
        *)    colour="$YELLOW" ;;
    esac
    printf '  %s%-6s%s %s\n' "$colour" "$result" "$RESET" "${STEP_NAMES[$index]}"
done
printf '%s%s\n' "$RESET" "--------------------------------------------------------"

if (( FAILURES > 0 )); then
    printf '%s%d step(s) failed.%s\n' "$RED" "$FAILURES" "$RESET"
    exit 1
fi

printf '%sAll checks passed.%s\n' "$GREEN" "$RESET"
