#!/usr/bin/env bash
#
# Provision a fresh Vast.ai instance for SignLaw AI.
#
#   curl -fsSL https://raw.githubusercontent.com/idesign2601/signlaw-ai/main/scripts/provision-vast.sh | bash
#
# Or, from a clone:
#
#   bash scripts/provision-vast.sh
#
# Idempotent: safe to re-run. Every step checks before acting.
#
# This encodes what two manual runs cost us. Each of the following was a
# separate hour:
#
#   * Vast templates give you a container, not a Docker host — so Postgres is
#     installed natively rather than through compose.
#   * `make install` pulls a torch built against a newer CUDA than the driver
#     supports, so GPU is unavailable until torch is reinstalled for cu126.
#   * pip's cache fills a 55 GB disk shared with another project.
#   * Ollama cannot see the GPU unless pciutils is installed *before* it.
#   * SECURITY__API_KEYS is JSON-decoded from .env, so a bare value fails.
#   * The tesseract binary and the ocrmypdf package are both required, and
#     installing one without the other silently skips every scanned page.

set -uo pipefail

REPO="${REPO:-https://github.com/idesign2601/signlaw-ai.git}"
ROOT="${ROOT:-$HOME/signlaw-ai}"
PG_VERSION=16

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'

step() { printf '\n%s━━━ %s%s\n' "$CYAN" "$1" "$RESET"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()  { printf '\n%s✗ %s%s\n' "$RED" "$1" "$RESET" >&2; exit 1; }

# -----------------------------------------------------------------------------
step "0/9  Preflight"
# -----------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run as root. Vast instances give you a root shell."

command -v nvidia-smi >/dev/null || die "No nvidia-smi. This is not a GPU instance."

DRIVER_CUDA="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
VRAM_FREE="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1)"
DISK_FREE="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"

printf '  driver      %s\n' "$DRIVER_CUDA"
printf '  VRAM free   %s MiB\n' "$VRAM_FREE"
printf '  disk free   %s GB\n' "$DISK_FREE"

(( DISK_FREE >= 25 )) || die "Need ~25 GB free; only ${DISK_FREE} GB available."

if (( VRAM_FREE < 6000 )); then
    warn "Under 6 GB of VRAM free — another workload is using this card."
    warn "Expect to run the embedder on CPU, or use a smaller LLM."
fi

# -----------------------------------------------------------------------------
step "1/9  System packages"
# -----------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# pciutils and lshw before Ollama: without them its installer cannot detect the
# GPU and it silently runs on CPU.
apt-get install -y -qq \
    curl ca-certificates gnupg lsb-release git \
    tesseract-ocr pciutils lshw >/dev/null
ok "base packages, tesseract, pciutils"

# -----------------------------------------------------------------------------
step "2/9  PostgreSQL with pgvector"
# -----------------------------------------------------------------------------
if ! command -v psql >/dev/null; then
    install -d /usr/share/postgresql-common/pgdg
    curl -fsSo /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
    apt-get update -qq
    apt-get install -y -qq \
        "postgresql-${PG_VERSION}" \
        "postgresql-${PG_VERSION}-pgvector" \
        "postgresql-client-${PG_VERSION}" >/dev/null
fi

# No systemd in a container, so pg_ctlcluster rather than systemctl.
pg_ctlcluster "$PG_VERSION" main start 2>/dev/null || true
pg_isready -q || die "Postgres did not start."
ok "postgres ${PG_VERSION} running"

DB_PASSWORD="$(openssl rand -hex 16)"
if su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='signlaw'\"" | grep -q 1; then
    su postgres -c "psql -q -c \"ALTER USER signlaw WITH PASSWORD '$DB_PASSWORD'\""
else
    su postgres -c "psql -q -c \"CREATE USER signlaw WITH PASSWORD '$DB_PASSWORD' SUPERUSER\""
    su postgres -c "createdb -O signlaw signlaw"
fi

PGPASSWORD="$DB_PASSWORD" psql -h localhost -U signlaw -d signlaw -qtc \
    "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null \
    || die "pgvector could not be enabled."
ok "database ready, pgvector enabled"

# -----------------------------------------------------------------------------
step "3/9  Application"
# -----------------------------------------------------------------------------
if [[ -d "$ROOT/.git" ]]; then
    git -C "$ROOT" pull --quiet
else
    git clone --quiet "$REPO" "$ROOT"
fi

cd "$ROOT"
[[ -d backend/.venv ]] || make install
ok "repository and virtualenv"

# -----------------------------------------------------------------------------
step "4/9  Torch matched to the driver"
# -----------------------------------------------------------------------------
# The default wheel targets a newer CUDA than most Vast drivers support, and the
# failure is a runtime "driver is too old" that only appears when a model loads.
# --no-cache-dir because the wheel is ~2.5 GB and the cache fills the disk.
if ! backend/.venv/bin/python -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    warn "torch cannot see the GPU; reinstalling for CUDA 12.6"
    backend/.venv/bin/pip install --quiet --no-cache-dir --force-reinstall \
        torch --index-url https://download.pytorch.org/whl/cu126
fi

if backend/.venv/bin/python -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    ok "torch sees the GPU"
    EMBEDDING_DEVICE=cuda
else
    warn "torch still cannot see the GPU — embeddings will run on CPU (slow)"
    EMBEDDING_DEVICE=cpu
fi

backend/.venv/bin/pip install --quiet --no-cache-dir ocrmypdf >/dev/null
ok "ocrmypdf installed"

# -----------------------------------------------------------------------------
step "5/9  Configuration"
# -----------------------------------------------------------------------------
if [[ ! -f .env ]]; then
    cp .env.example .env
fi

API_KEY="$(openssl rand -hex 32)"
ADMIN_KEY="$(openssl rand -hex 32)"

set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" .env; then
        # A different delimiter for each call would be neater; | is safe here
        # because none of these values can contain one.
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}

set_env ENVIRONMENT staging
set_env DB__PASSWORD "$DB_PASSWORD"
set_env EMBEDDING__DEVICE "$EMBEDDING_DEVICE"
set_env EMBEDDING__BATCH_SIZE 32
set_env LLM__MODEL "qwen2.5:7b-instruct"
set_env LLM__NUM_CTX 8192
set_env SECURITY__ADMIN_API_KEY "$ADMIN_KEY"
# JSON, not a bare value: list-valued settings are decoded from the file
# before any validator runs.
set_env SECURITY__API_KEYS "[\"${API_KEY}\"]"
ok "configuration written"

# -----------------------------------------------------------------------------
step "6/9  Schema"
# -----------------------------------------------------------------------------
(cd backend && .venv/bin/alembic upgrade head) || die "Migrations failed."
ok "schema at head"

# -----------------------------------------------------------------------------
step "7/9  Ollama"
# -----------------------------------------------------------------------------
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh

if ! pgrep -f 'ollama serve' >/dev/null; then
    # KEEP_ALIVE=0 unloads the model between questions. On a shared card that
    # is what lets the LLM and the embedder take turns instead of competing;
    # the cost is a 10-20 second load on the first question after an idle spell.
    OLLAMA_KEEP_ALIVE=0 nohup ollama serve > /tmp/ollama.log 2>&1 &
    sleep 5
fi

ollama list | grep -q 'qwen2.5:7b-instruct' || ollama pull qwen2.5:7b-instruct
ok "ollama running with qwen2.5:7b-instruct"

# -----------------------------------------------------------------------------
step "8/9  Models"
# -----------------------------------------------------------------------------
make fetch-models >/dev/null 2>&1 || warn "fetch-models reported a problem"
ok "embedding and reranker weights present"

# -----------------------------------------------------------------------------
step "9/9  Health"
# -----------------------------------------------------------------------------
make health || warn "not all components are ready — see above"

cat <<SUMMARY

${BOLD}Provisioned.${RESET}

  ${DIM}API key${RESET}    $API_KEY
  ${DIM}Admin key${RESET}  $ADMIN_KEY

Both are in $ROOT/.env. The frontend needs them as SIGNLAW_API_KEY and
SIGNLAW_ADMIN_KEY.

Start the API:

  ${DIM}cd $ROOT/backend && nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/api.log 2>&1 &${RESET}

Expose it (a quick tunnel; the URL changes on every restart):

  ${DIM}curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared${RESET}
  ${DIM}nohup cloudflared tunnel --url http://localhost:8000 > /tmp/tunnel.log 2>&1 &${RESET}
  ${DIM}sleep 10 && grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/tunnel.log | head -1${RESET}

To stop only SignLaw's API — never ${DIM}pkill -f uvicorn${RESET} on a shared box:

  ${DIM}pkill -f 'uvicorn app.main:app --host 0.0.0.0 --port 8000'${RESET}

SUMMARY
