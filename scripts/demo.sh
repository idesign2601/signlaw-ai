#!/usr/bin/env bash
#
# End-to-end local demo: health -> ingest -> ask.
#
# Proves the whole pipeline runs on one machine with no external API:
# Postgres with pgvector, a local embedding model, and Ollama for generation.
#
#   ./scripts/demo.sh                          use documents/bylaws/
#   ./scripts/demo.sh path/to/a_bylaw.pdf      use one file

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
SIGNLAW="$BACKEND_DIR/.venv/bin/signlaw"

CORPUS="${1:-$ROOT_DIR/documents/bylaws}"
QUESTION="${QUESTION:-What is the maximum fascia sign area?}"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
GREEN=$'\033[32m'; CYAN=$'\033[36m'; RESET=$'\033[0m'

step() { printf '\n%s==> %s%s\n' "$CYAN" "$1" "$RESET"; }
die()  { printf '%s%s%s\n' "$RED" "$1" "$RESET" >&2; exit 1; }

[[ -x "$SIGNLAW" ]] || die "signlaw not installed. Run: make install"

if [[ ! -e "$CORPUS" ]]; then
    die "No corpus at $CORPUS
Put a bylaw PDF there, for example:
  mkdir -p documents/bylaws
  cp ~/Downloads/burnaby_sign_bylaw.pdf documents/bylaws/"
fi

# -----------------------------------------------------------------------------
step "1/3  Checking dependencies"
# -----------------------------------------------------------------------------
if ! "$SIGNLAW" health; then
    printf '\n%sDependencies are not ready. Common fixes:%s\n' "$BOLD" "$RESET"
    printf '  %sdocker compose up -d postgres%s   start the database\n' "$DIM" "$RESET"
    printf '  %smake migrate%s                    apply the schema\n' "$DIM" "$RESET"
    printf '  %smake fetch-models%s               download embedding weights\n' "$DIM" "$RESET"
    printf '  %sollama serve%s                    start local generation\n' "$DIM" "$RESET"
    printf '  %sollama pull qwen2.5:14b-instruct%s\n' "$DIM" "$RESET"
    exit 1
fi

# -----------------------------------------------------------------------------
step "2/3  Indexing $CORPUS"
# -----------------------------------------------------------------------------
"$SIGNLAW" ingest "$CORPUS" || die "Ingestion failed."

# -----------------------------------------------------------------------------
step "3/3  Asking: $QUESTION"
# -----------------------------------------------------------------------------
"$SIGNLAW" ask "$QUESTION" --trace
status=$?

printf '\n%s%s%s\n' "$GREEN" "Demo complete — everything ran locally." "$RESET"
printf '%sTry another: signlaw ask "Are projecting signs allowed?" --trace%s\n' \
    "$DIM" "$RESET"

exit $status
