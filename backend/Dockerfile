# syntax=docker/dockerfile:1.7
#
# Build arguments
# ---------------
#   EXTRAS    core | local | cloud
#             core  - no inference libraries at all (smallest; API-only when
#                     embeddings and generation are served elsewhere)
#             local - sentence-transformers + torch for on-machine embeddings
#                     and reranking, plus the Ollama client  [default]
#             cloud - OpenAI and Anthropic SDKs only
#
#   WITH_OCR  false | true
#             Installs the Tesseract *binary* and OCRmyPDF's helpers. It never
#             installs language data: trained OCR models are downloaded
#             separately into the /models volume by `make fetch-models`.
#
#   TORCH_VARIANT  default | cpu | cuda
#             `cpu` installs the CPU-only torch wheel, cutting roughly 2 GB
#             from a development image. `default` and `cuda` both take the
#             PyPI wheel, which bundles the CUDA runtime.
#
# No model weights of any kind are baked into these images. Embedding,
# reranking and OCR models all live in the mounted /models volume.

# =============================================================================
# Stage 1 — builder
# =============================================================================
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app

ARG EXTRAS=local

# torch dominates image size. The default PyPI wheel bundles the CUDA runtime
# (~2.5 GB); the CPU-only index is roughly a fifth of that. Development and CI
# have no GPU, so they build `cpu` and stay small; GPU hosts build `cuda`.
ARG TORCH_VARIANT=default

RUN pip install --upgrade pip setuptools wheel \
    && if [ "$TORCH_VARIANT" = "cpu" ] && [ "$EXTRAS" = "local" ]; then \
           pip install torch --index-url https://download.pytorch.org/whl/cpu; \
       fi \
    && if [ "$EXTRAS" = "core" ]; then pip install "."; \
       else pip install ".[${EXTRAS}]"; fi

# =============================================================================
# Stage 2 — runtime
# =============================================================================
FROM python:3.12-slim-bookworm AS runtime

ARG WITH_OCR=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # Hugging Face cache — embedding and reranker weights land here, on a volume.
    HF_HOME=/models/huggingface \
    # Tesseract looks for *.traineddata here. Populated by `make fetch-models`,
    # never by the image build.
    TESSDATA_PREFIX=/models/tessdata

# libpq5 is the Postgres client library and is always required.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# --no-install-recommends is load-bearing: without it, apt pulls
# tesseract-ocr-eng, which would bake a trained OCR model into the image.
RUN if [ "$WITH_OCR" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            tesseract-ocr \
            ghostscript \
            qpdf \
            pngquant \
            unpaper \
        && rm -rf /var/lib/apt/lists/*; \
    fi

RUN groupadd --system --gid 1001 signlaw \
    && useradd --system --uid 1001 --gid signlaw --create-home signlaw \
    && mkdir -p /data/corpus /data/blobs /models/huggingface /models/tessdata \
    && chown -R signlaw:signlaw /data /models

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=signlaw:signlaw alembic.ini ./
COPY --chown=signlaw:signlaw alembic ./alembic
COPY --chown=signlaw:signlaw app ./app

USER signlaw
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
