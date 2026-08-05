#!/usr/bin/env bash
#
# Download every AI model SignLaw AI uses into the local models directory.
#
# Nothing here is baked into a Docker image. Run this once after checkout (or
# after `make clean`), and again whenever you change the embedding or reranker
# model in .env.
#
# Downloads:
#   * Tesseract traineddata  -> $MODELS_DIR/tessdata
#   * Embedding model        -> $MODELS_DIR/huggingface
#   * Reranker model         -> $MODELS_DIR/huggingface
#
# For a fully air-gapped install, run this on a connected machine and copy
# $MODELS_DIR across.

set -euo pipefail

MODELS_DIR="${MODELS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/models}"
TESSDATA_DIR="${TESSDATA_DIR:-$MODELS_DIR/tessdata}"
HF_HOME="${HF_HOME:-$MODELS_DIR/huggingface}"

# Read model names from .env when present, otherwise use the project defaults.
ENV_FILE="${ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env}"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a && source "$ENV_FILE" && set +a
fi

EMBEDDING_MODEL="${EMBEDDING__MODEL:-BAAI/bge-m3}"
RERANK_MODEL="${RETRIEVAL__RERANK_MODEL:-BAAI/bge-reranker-v2-m3}"
OCR_LANGUAGES="${INGESTION__OCR_LANGUAGES:-eng}"

# tessdata_fast is ~4x smaller than tessdata_best with negligible accuracy loss
# on the clean 300 DPI scans typical of municipal bylaw PDFs.
TESSDATA_BASE_URL="${TESSDATA_BASE_URL:-https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mxx\033[0m %s\n' "$*" >&2; exit 1; }

mkdir -p "$TESSDATA_DIR" "$HF_HOME"

# -----------------------------------------------------------------------------
# Tesseract language data
# -----------------------------------------------------------------------------
log "Tesseract traineddata -> $TESSDATA_DIR"

command -v curl >/dev/null 2>&1 || die "curl is required"

# INGESTION__OCR_LANGUAGES is a '+'-separated list, e.g. "eng+fra".
IFS='+' read -ra languages <<< "$OCR_LANGUAGES"
for language in "${languages[@]}"; do
    target="$TESSDATA_DIR/${language}.traineddata"
    if [[ -s "$target" ]]; then
        log "  ${language}.traineddata already present, skipping"
        continue
    fi
    log "  downloading ${language}.traineddata"
    if ! curl -fsSL "$TESSDATA_BASE_URL/${language}.traineddata" -o "$target.tmp"; then
        rm -f "$target.tmp"
        die "failed to download ${language}.traineddata — check the language code"
    fi
    mv "$target.tmp" "$target"
done

# -----------------------------------------------------------------------------
# Embedding and reranker weights
# -----------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 not found — skipping embedding and reranker download."
    warn "Install the backend (\`make install\`) and re-run to fetch them."
    exit 0
fi

if ! python3 -c "import huggingface_hub" >/dev/null 2>&1; then
    warn "huggingface_hub not installed — skipping embedding and reranker download."
    warn "Run \`make install\` first, then re-run this script."
    exit 0
fi

log "Embedding model '$EMBEDDING_MODEL' -> $HF_HOME"
HF_HOME="$HF_HOME" python3 - "$EMBEDDING_MODEL" <<'PYTHON'
import sys

from huggingface_hub import snapshot_download

repo_id = sys.argv[1]
# Weight formats other than safetensors are skipped to avoid downloading the
# same parameters two or three times over.
snapshot_download(
    repo_id=repo_id,
    ignore_patterns=["*.bin", "*.h5", "*.ot", "*.msgpack", "*.onnx"],
)
print(f"  cached {repo_id}")
PYTHON

log "Reranker model '$RERANK_MODEL' -> $HF_HOME"
HF_HOME="$HF_HOME" python3 - "$RERANK_MODEL" <<'PYTHON'
import sys

from huggingface_hub import snapshot_download

repo_id = sys.argv[1]
snapshot_download(
    repo_id=repo_id,
    ignore_patterns=["*.bin", "*.h5", "*.ot", "*.msgpack", "*.onnx"],
)
print(f"  cached {repo_id}")
PYTHON

log "Done. Models are in $MODELS_DIR and are never copied into a Docker image."
