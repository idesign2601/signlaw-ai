# SignLaw AI

A citation-first retrieval system over municipal sign bylaws from British Columbia.
Answers are grounded in indexed bylaw text only, and every claim carries a document,
page number, section number, and confidence score.

> **Informational only.** SignLaw AI is not legal advice. Always verify against the
> municipality before applying for a permit or fabricating signage.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton: config, logging, errors, DB models, migrations, Docker, CI | **Complete** |
| 2 | Ingestion: extraction, OCR, tables, metadata, section parsing, chunking | Not started |
| 3 | Index: embeddings, Chroma, versioned collections | Not started |
| 4 | Retrieval: hybrid search, rerank, filters, query router | Not started |
| 5 | Generation: synthesis, citation verifier, confidence scorer | Not started |
| 6 | API: chat, compare, search, documents, admin, SSE | Not started |
| 7 | Frontend | Not started |
| 8 | Evaluation harness + golden set | Not started |
| 9 | Hardening | Not started |

- [`docs/DEMO.md`](docs/DEMO.md) — **run the whole pipeline locally in three
  commands**, no web API
- [`docs/PHASE1.md`](docs/PHASE1.md) — architecture diagram, setup, every
  environment variable, Postgres and Ollama, running tests
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — the full design

## Try it now

```bash
make install && make migrate && make fetch-models
signlaw health
signlaw ingest documents/bylaws/burnaby_sign_bylaw.pdf
signlaw ask "What is the maximum fascia sign area?"
```

## Defaults: local-first

Out of the box nothing leaves the machine and no hosted account is needed:

- **Generation** — Ollama running Qwen 2.5 (`LLM__PROVIDER=ollama`)
- **Embeddings** — BGE-M3 loaded locally (`EMBEDDING__PROVIDER=local`)
- **Vector store** — ChromaDB in a container
- **Records** — Postgres in a container

OpenAI and Anthropic are opt-in: set the provider and its API key, and the
adapters take over. No code changes.

**No model weights are baked into any Docker image.** Embedding, reranker and
Tesseract OCR models download separately into `data/models/` via
`make fetch-models`, and mount at `/models`. For an air-gapped install, fetch
them on a connected machine and copy the directory across.

## Quick start (Docker)

```bash
cp .env.example .env          # then edit DB__PASSWORD at minimum
docker compose up -d --build  # postgres, redis, chroma, migrations, api
curl http://localhost:8000/healthz
open http://localhost:8000/docs
```

The ingestion worker is behind a compose profile because it is introduced in
Phase 2:

```bash
docker compose --profile worker up -d
```

For local generation, run Ollama on the host and pull a model:

```bash
ollama pull qwen2.5:14b-instruct
```

The API container reaches it via `host.docker.internal`.

## Quick start (local Python)

Requires Python 3.12+, and Postgres and Chroma reachable (`docker compose up -d postgres redis chroma`).

```bash
make install
cp .env.example .env
make migrate
make dev
```

## Development

```bash
make check              # lint + typecheck + tests, the same gates CI runs
make test               # unit + e2e, no external services needed
make test-integration   # requires Postgres and Chroma
make migration m="add sections table"
```

Quality bars enforced in CI: `ruff` lint and format, `mypy --strict`, and pytest
with coverage. The `app/domain/` package is pure — no I/O, no network, no
database — and carries the heaviest test coverage, because that is where a
silent regression turns into a wrong legal citation.

## Configuration

All settings live in `app/core/config.py` and are validated at process start;
a misconfiguration fails loudly at boot rather than on the first request.
Nested settings use a double-underscore delimiter — `DB__HOST` maps to
`settings.db.host`. See [`.env.example`](.env.example) for every option.

## Layout

```
backend/app/
  core/        config, logging, exceptions, security
  api/         FastAPI routers, dependencies, error handlers
  schemas/     Pydantic request/response contracts
  domain/      pure logic: section parsing, chunking, citations, confidence
  ingestion/   PDF extraction, OCR, metadata detection, worker  (Phase 2)
  rag/         retrieval, reranking, synthesis, verification    (Phases 3-5)
  services/    orchestration across domain + adapters
  adapters/    LLM, embeddings, vector store, blob storage
  db/          SQLAlchemy models, session, repositories
```

## License

Proprietary. All rights reserved.
