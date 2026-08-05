# SignLaw AI — Phase 1

Skeleton: configuration, logging, error handling, database schema, migrations,
Docker, CI and health checks. No retrieval or ingestion yet — those are Phases
2–5 — but everything below runs, and every gate in §7 passes before Phase 2
begins.

---

## 1. System architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  BROWSER                                                                 │
│  React + Tailwind · Chat · Compare · Search · PDF viewer · Admin         │
│  (Phase 7)                                                               │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  REST + SSE
┌───────────────────────────────▼──────────────────────────────────────────┐
│  API — FastAPI / Uvicorn                          [container: api]       │
│                                                                          │
│   RequestContextMiddleware   correlation ID, timing, structured logs     │
│   CORS · GZip                                                            │
│   Exception handlers         every failure -> RFC 7807 problem+json      │
│                                                                          │
│   /healthz  /readyz          liveness, readiness              ✅ Phase 1 │
│   /api/v1/chat  /compare  /search  /documents  /admin         ⬜ Phase 6 │
└──────┬────────────────────────────────────────────────┬──────────────────┘
       │                                                │
       │ read/write                                     │ enqueue
       │                                                │
┌──────▼──────────────────────┐              ┌──────────▼───────────────────┐
│  POSTGRES + pgvector        │              │  REDIS         [container]   │
│  [container: postgres]      │              │  ingestion job queue (arq)   │
│                             │              └──────────┬───────────────────┘
│  SINGLE STORE               │                         │
│   province ── municipality  │                         │ consume
│   document ── bylaw_relation│              ┌──────────▼───────────────────┐
│   page · document_table     │              │  INGESTION WORKER            │
│   section  (self-ref tree)  │◄─────────────┤  [container: worker]         │
│   chunk    (parent/child)   │   write      │                              │
│   ingestion_job             │              │  PyMuPDF extraction          │
│   document_stage_event      │              │  OCR fallback (Tesseract)    │
│   chat_session/chat_message │              │  table extraction            │
│   answer_feedback           │              │  metadata detection          │
│                             │              │  section-tree parsing        │
│   embedding_collection      │              │  structure-aware chunking    │
│   chunk_embedding_{384,768, │              │              ✅ Phase 2      │
│     1024,1536}  HNSW        │              └──────────┬───────────────────┘
│                             │                         │ embed
│  GIN full-text on chunk.body│◄────────────────────────┘
└─────────────────────────────┘

Vectors live beside the relational data they describe, so a chunk and its
embedding are written in one transaction and a filtered search ("in-force
bylaws in Coquitlam") is a join, not a cross-system reconciliation.

┌──────────────────────────────────────────────────────────────────────────┐
│  MODELS — mounted volume, never baked into an image                      │
│    /models/tessdata      Tesseract *.traineddata                         │
│    /models/huggingface   embedding + reranker weights                    │
│  Generation runs in Ollama on the host, or a hosted API if you opt in.   │
└──────────────────────────────────────────────────────────────────────────┘
```

**Why two stores.** Postgres is authoritative; Chroma holds only vectors and a
thin slice of filter metadata and is fully rebuildable. Re-chunking or swapping
the embedding model therefore costs one embedding pass rather than re-OCRing
hundreds of scanned PDFs.

**Layering inside `app/`.** Dependencies point inward only:

```
api/  ──►  services/  ──►  domain/        (pure: no I/O, no network, no DB)
                    └──►  adapters/       (LLM, embeddings, vector store, blobs)
                    └──►  db/             (SQLAlchemy, repositories)
```

`domain/` holds section parsing, chunking, citation verification and confidence
scoring — the components where a silent regression becomes a wrong legal
citation. Keeping them free of I/O is what makes them cheap to test exhaustively.

---

## 2. Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Docker + Compose | 24+ | Postgres, Redis, Chroma, the containerised stack |
| Python | 3.12+ | running the backend outside Docker |
| Ollama | 0.4+ | local generation (optional — see §6) |
| `make`, `bash`, `curl` | any | the Makefile targets and scripts |

On Windows, run the `make` and `bash` targets from WSL2 or Git Bash. Docker
Desktop with the WSL2 backend works unchanged.

---

## 3. Local setup

### 3.1 Everything in Docker (quickest)

```bash
git clone <repo> && cd signlaw-ai
cp .env.example .env          # then edit DB__PASSWORD

docker compose up -d --build  # postgres, redis, chroma, migrations, api
curl http://localhost:8000/healthz
open http://localhost:8000/docs
```

`docker compose up` runs migrations to completion before the API starts, so the
schema is always current.

The ingestion worker sits behind a compose profile because it arrives in Phase 2:

```bash
docker compose --profile worker up -d
```

### 3.2 Backend on the host, services in Docker

Better for development — autoreload, a debugger, and fast tests.

```bash
docker compose up -d postgres redis chroma   # just the infrastructure
make install                                  # backend/.venv + dev extras
cp .env.example .env
make fetch-models                             # OCR + embedding + reranker weights
make migrate
make dev                                      # http://localhost:8000
```

### 3.3 What `make fetch-models` does

Downloads every model into `data/models/`:

| Destination | Contents |
|---|---|
| `data/models/tessdata/` | `eng.traineddata` (and any other `INGESTION__OCR_LANGUAGES`) |
| `data/models/huggingface/` | `EMBEDDING__MODEL` and `RETRIEVAL__RERANK_MODEL` weights |

Nothing is baked into a Docker image. For an air-gapped install, run this on a
connected machine and copy `data/models/` across.

---

## 4. Environment variables

Copy `.env.example` to `.env`. Nested settings use a **double underscore**:
`DB__HOST` maps to `settings.db.host`. Everything is validated at process start,
so a mistake fails at boot with an actionable message rather than on the first
request that touches it.

### Application

| Variable | Default | Notes |
|---|---|---|
| `APP_NAME` | `SignLaw AI` | |
| `ENVIRONMENT` | `local` | `local` · `dev` · `staging` · `production` |
| `DEBUG` | `false` | Must be `false` in production |
| `API_PREFIX` | `/api/v1` | Must start with `/` |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | |

### Database

| Variable | Default | Notes |
|---|---|---|
| `DB__HOST` | `localhost` | `postgres` inside compose |
| `DB__PORT` | `5432` | |
| `DB__USER` / `DB__NAME` | `signlaw` | |
| `DB__PASSWORD` | `signlaw` | **Rejected in production if left at a default** |
| `DB__POOL_SIZE` | `10` | |
| `DB__MAX_OVERFLOW` | `20` | |
| `DB__ECHO` | `false` | Logs every statement |

### Redis (ingestion queue)

| Variable | Default |
|---|---|
| `REDIS__HOST` / `REDIS__PORT` / `REDIS__DB` | `localhost` / `6379` / `0` |
| `REDIS__PASSWORD` | *(empty)* |

### LLM

| Variable | Default | Notes |
|---|---|---|
| `LLM__PROVIDER` | `ollama` | `ollama` · `openai` · `anthropic` |
| `LLM__MODEL` | `qwen2.5:14b-instruct` | Free text; validated against the provider at boot |
| `LLM__TEMPERATURE` | `0.0` | Deterministic — bylaw answers should not vary run to run |
| `LLM__MAX_TOKENS` | `2048` | |
| `LLM__REQUEST_TIMEOUT_S` | `120` | |
| `LLM__OLLAMA_BASE_URL` | `http://localhost:11434` | `http://host.docker.internal:11434` from a container |
| `LLM__OPENAI_API_KEY` | *(none)* | **Required only if** provider is `openai` |
| `LLM__ANTHROPIC_API_KEY` | *(none)* | **Required only if** provider is `anthropic` |

### Embeddings

| Variable | Default | Notes |
|---|---|---|
| `EMBEDDING__PROVIDER` | `local` | `local` · `openai` |
| `EMBEDDING__MODEL` | `BAAI/bge-m3` | Any sentence-transformers or OpenAI model |
| `EMBEDDING__DIMENSIONS` | `1024` | Cross-checked against known models at boot |
| `EMBEDDING__BATCH_SIZE` | `32` | |
| `EMBEDDING__DEVICE` | `auto` | `auto` · `cpu` · `cuda` · `cuda:N` · `mps` |

Changing the model or dimensions invalidates the vector index — re-index into a
new `VECTOR__INDEX_VERSION`.

### Vector store (pgvector, inside Postgres)

| Variable | Default | Notes |
|---|---|---|
| `VECTOR__COLLECTION_PREFIX` | `signlaw` | Collection name is `<prefix>_<model>_v<n>` |
| `VECTOR__INDEX_VERSION` | `1` | Live collection is `signlaw_bge_m3_v1` |
| `VECTOR__CHUNKING_VERSION` | `1` | Bump when chunking rules change |
| `VECTOR__DISTANCE_METRIC` | `cosine` | `cosine` · `l2` · `ip` |
| `VECTOR__HNSW_M` | `16` | Index graph connectivity |
| `VECTOR__HNSW_EF_CONSTRUCTION` | `64` | Build-time candidate list |
| `VECTOR__HNSW_EF_SEARCH` | `100` | Query candidate list; must exceed top-k |

Changing `EMBEDDING__MODEL` requires a new collection but **not** re-extraction,
re-OCR, table detection or section parsing — only `chunks → embeddings → index`.
Changing `VECTOR__CHUNKING_VERSION` is the expensive case and re-chunks.

### Ingestion

| Variable | Default | Notes |
|---|---|---|
| `INGESTION__CORPUS_DIR` | `./data/corpus` | Where your bylaw PDFs live |
| `INGESTION__BLOB_DIR` | `./data/blobs` | Normalised copies and OCR output |
| `INGESTION__MAX_FILE_SIZE_MB` | `200` | |
| `INGESTION__CONCURRENCY` | `4` | Documents processed in parallel |
| `INGESTION__SCAN_DETECTION_MIN_CHARS` | `120` | Below this per page, treat as a scan and OCR |
| `INGESTION__OCR_ENABLED` | `true` | |
| `INGESTION__OCR_LANGUAGES` | `eng` | `+`-separated, e.g. `eng+fra` |
| `INGESTION__OCR_DPI` | `300` | |
| `INGESTION__TESSDATA_DIR` | `./data/models/tessdata` | Populated by `make fetch-models` |
| `INGESTION__CHUNK_TARGET_TOKENS` | `700` | |
| `INGESTION__CHUNK_OVERLAP_TOKENS` | `80` | Must be below the target |
| `INGESTION__CHUNK_MAX_TOKENS` | `1200` | Must be at or above the target |

### Retrieval

Pipeline: pgvector top 50 + Postgres full-text top 50 → weighted RRF → top 50 →
local reranker → top 5 → LLM.

| Variable | Default | Notes |
|---|---|---|
| `RETRIEVAL__DENSE_TOP_K` | `50` | pgvector HNSW search |
| `RETRIEVAL__SPARSE_TOP_K` | `50` | Postgres `websearch_to_tsquery` + `ts_rank_cd` |
| `RETRIEVAL__CANDIDATE_POOL_SIZE` | `50` | What the reranker sees |
| `RETRIEVAL__DENSE_WEIGHT` | `0.5` | Fusion weight |
| `RETRIEVAL__SPARSE_WEIGHT` | `0.5` | Equal: bylaw queries are full of exact terms |
| `RETRIEVAL__RRF_K` | `60` | Higher flattens top-rank advantage |
| `RETRIEVAL__RERANK_ENABLED` | `true` | |
| `RETRIEVAL__RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | |
| `RETRIEVAL__RERANK_TOP_N` | `5` | What reaches the model |
| `RETRIEVAL__ABSTAIN_BELOW_CONFIDENCE` | `0.35` | Below this the system declines to answer |
| `RETRIEVAL__IN_FORCE_ONLY` | `true` | **Excludes superseded and repealed text** |

### Security

| Variable | Default | Notes |
|---|---|---|
| `SECURITY__ADMIN_API_KEY` | *(none)* | ≥32 chars. **Required in production.** `openssl rand -hex 32` |
| `SECURITY__CORS_ORIGINS` | localhost 5173/3000 | JSON array. `*` rejected in production |
| `SECURITY__RATE_LIMIT_PER_MINUTE` | `60` | |

### Observability

| Variable | Default | Notes |
|---|---|---|
| `OBSERVABILITY__LOG_LEVEL` | `INFO` | |
| `OBSERVABILITY__LOG_FORMAT` | `console` | `json` required in production |
| `OBSERVABILITY__LOG_SQL` | `false` | |
| `OBSERVABILITY__PERSIST_RETRIEVAL_TRACE` | `true` | Audit record for every answer |

### Production refuses to boot if…

`ENVIRONMENT=production` and any of: `DEBUG=true`, no `SECURITY__ADMIN_API_KEY`,
a default `DB__PASSWORD`, `*` in CORS origins, or `LOG_FORMAT` other than `json`.
All problems are reported together, so one restart tells you everything to fix.

---

## 5. Starting Postgres

### Compose (recommended)

```bash
docker compose up -d postgres
docker compose ps postgres            # wait for "healthy"
make migrate                          # apply the schema
make psql                             # open a shell
```

Data persists in the `pgdata` volume. `make clean` destroys it.

### An existing Postgres

Point `.env` at it and create the database:

```sql
CREATE DATABASE signlaw;
CREATE USER signlaw WITH PASSWORD 'your-password';
GRANT ALL PRIVILEGES ON DATABASE signlaw TO signlaw;
```

```bash
make migrate
```

Postgres 14+ is required — the schema uses `JSONB`, arrays, native enums and a
GIN full-text index.

### Migration commands

```bash
make migrate                          # upgrade to head
make migration m="add sections table" # autogenerate a revision
make downgrade                        # roll back one
cd backend && .venv/bin/alembic check # detect model/migration drift
```

Alembic runs through asyncpg, so the project needs only one Postgres driver.

---

## 6. Starting Ollama

Ollama is **optional**. The API boots, serves health checks and applies
migrations without it; only generation needs it, and only when
`LLM__PROVIDER=ollama` (the default).

### Install and run

```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

ollama serve                          # usually already running as a service
ollama pull qwen2.5:14b-instruct
ollama list
```

Windows: install the official Ollama package; it runs as a background service on
port 11434.

### Reaching it from a container

The compose file sets `LLM__OLLAMA_BASE_URL=http://host.docker.internal:11434`
and adds `host.docker.internal:host-gateway`, so containers reach an Ollama
running on the host. To run Ollama in Docker instead, add a service and point
the URL at it.

### Model sizing

| Model | RAM/VRAM | Notes |
|---|---|---|
| `qwen2.5:7b-instruct` | ~6 GB | Fastest; acceptable for single-city questions |
| `qwen2.5:14b-instruct` | ~10 GB | **Default.** Best accuracy-to-size for citation work |
| `qwen2.5:32b-instruct` | ~20 GB | Best comparison-table quality |

Change `LLM__MODEL` and restart — no code change.

### Not using Ollama

Set `LLM__PROVIDER=openai` (or `anthropic`) and supply the matching key. The
adapters are written and tested either way.

---

## 7. Running tests

```bash
make test               # unit + e2e — no Postgres, Redis, Chroma or models needed
make test-integration   # requires Postgres
make test-cov           # coverage report
make lint               # ruff check + format check
make typecheck          # mypy --strict
make check              # lint + typecheck + test (what CI runs)
```

### Full verification

```bash
make verify             # docker build, migrations up/down/up, ruff, mypy, pytest
make verify-fast        # same without Docker builds or Postgres
```

`verify.sh` runs every step even when one fails, so a single run reports
everything that is broken. Summary at the end:

```
Verification summary
--------------------------------------------------------
  pass   docker compose build
  pass   database migrations (up -> down -> up -> check)
  pass   ruff check
  pass   ruff format --check
  pass   mypy --strict
  pass   pytest (unit + e2e)
  pass   pytest (integration)
--------------------------------------------------------
All checks passed.
```

### Suite layout

| Path | Needs infrastructure | Covers |
|---|---|---|
| `tests/unit/` | no | config validation, exceptions, logging redaction, admin auth, ORM metadata |
| `tests/e2e/` | no | health, readiness, RFC 7807 error contract, CORS, docs gating |
| `tests/integration/` | Postgres | migrations apply, constraints actually enforced, cascades |
| `tests/eval/` | built index | golden Q&A regression (Phase 8) |

Unit and e2e tests replace the database engine with a stub, so the fast suite
never touches a network socket.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Unsafe production configuration` at boot | Production invariants violated | Read the bullet list in the error; it names every problem |
| `EMBEDDING__DIMENSIONS ... does not match` | Dimension/model mismatch | Correct the dimension, or use an unlisted model |
| `/readyz` returns 503 | Postgres unreachable | `docker compose ps postgres`; check `DB__HOST` |
| `alembic check` reports drift | Models changed without a migration | `make migration m="..."` |
| Ollama connection refused from a container | Wrong base URL | Use `http://host.docker.internal:11434` |
| `TesseractError: language 'eng' not found` | Traineddata not downloaded | `make fetch-models` |
| Tests read the wrong settings | A local `.env` leaking in | The suite pins `env_file=None`; check for shell exports |
