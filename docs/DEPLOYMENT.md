# Deployment — Vast.ai GPU hosting

Target: NVIDIA RTX 3090 (24 GB) or similar, CUDA-enabled Docker.

Architecture is unchanged: local-first, no OpenAI, Ollama for generation, local
embeddings, Postgres + pgvector. CPU-only development still works with the same
compose files.

---

## 1. The constraint that shapes everything

**Vast.ai instances are ephemeral.** Storage is instance-local. If the instance
is destroyed — by you, by preemption on an interruptible bid, or by the host
going offline — **the disk goes with it.** There is no managed persistent volume.

That matters unevenly across the four data classes:

| Data | Lives in | If lost | Recovery |
|---|---|---|---|
| **Postgres** (documents, sections, chunks, embeddings, lineage, traces) | `pgdata` volume | **Unrecoverable** | Re-ingest the entire corpus |
| PDFs | `documents/` bind mount | Recoverable | Re-download from municipalities |
| Embeddings | *inside Postgres* | Regenerable | Re-embed: minutes on GPU, hours on CPU |
| Model weights | `models` + `ollama_models` volumes | Recoverable | Re-download ~15 GB |

Only the first is genuinely dangerous, and it is the one holding the audit trail
— the retrieval traces that let a disputed answer be reconstructed months later.
For a legal tool, losing those is worse than losing the index.

**Recommendation: do not put Postgres on an ephemeral GPU instance.** See
topology B. If you do (topology A), the backup step is not optional.

---

## 2. Topologies

**Decided:** Topology B is the target. Postgres does not depend on Vast.ai
ephemeral storage; the GPU instance is replaceable compute.

### A — everything on one Vast instance *(fallback only)*

Simplest, and adequate for a short validation run. **Not the target
architecture**, because it makes the one unrecoverable data class depend on the
most replaceable machine.

```
┌─ Vast.ai GPU instance (RTX 3090) ──────────────────────┐
│                                                        │
│  ollama          GPU   generation, model volume        │
│  api             GPU   query embedding + orchestration │
│  worker          GPU   ingestion, bulk embedding       │
│  postgres        CPU   pgdata volume  ← at risk        │
│  redis           CPU                                   │
│                                                        │
└────────────────────────────────────────────────────────┘
```

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

Allocate **at least 80 GB** of instance disk: ~15 GB models, ~10 GB images,
the rest for Postgres and blobs.

**Back up on a schedule** — see §4.1. Not optional in this topology.

### B — persistent database + stateless GPU worker *(target)*

```
┌─ Vast.ai GPU instance ─────────┐   ┌─ Persistent CPU host ──────────┐
│                                │   │  (your machine, VPS, managed)  │
│  ollama    generation          │   │                                │
│            + embeddings        │◄──┤  postgres + pgvector           │
│                                │   │  api                           │
│  GPU-bound inference only.     │   │  worker                        │
│  Stateless. Destroy freely.    │   │  documents/                    │
└────────────────────────────────┘   └────────────────────────────────┘
```

The GPU box becomes stateless: it holds model weights and nothing else, so
losing it costs a re-pull rather than the corpus. Rent interruptible instances
without risk, and stop paying for the GPU when you are not ingesting.

**This topology needs code that does not exist yet.** See §3.

### C — CPU only (development, CI, Milestone 1 on your own machine)

```bash
docker compose up -d          # TORCH_VARIANT defaults to cpu
```

Unchanged. The GPU overlay is purely additive.

---

## 3. What topology B requires that we have not built

Honest gap, flagged rather than glossed.

`LocalEmbeddingProvider` loads sentence-transformers **in-process**. If the API
and worker run on a CPU host, embeddings run on that host's CPU — the GPU is
used for generation only, and ingestion throughput stays slow. That defeats
most of the point.

Two ways to close it, both **deferred until Milestone 1 passes**:

**Option 1 — Ollama serves embeddings too** *(preferred)*

Ollama exposes `/api/embed` and can serve `bge-m3`. A new
`OllamaEmbeddingProvider` behind the existing `EmbeddingProviderProtocol` would
let the GPU box handle both generation and embedding with no additional service.
Roughly 120 lines, no architectural change — the Protocol seam already exists.

One correctness note: Ollama's `bge-m3` and sentence-transformers' `bge-m3` may
normalise differently. **Vectors from the two are not interchangeable.** Mixing
them inside one collection would silently corrupt retrieval. The collection
model already prevents this — `embedding_model_revision` differs, so switching
provider forces a new collection and a re-index. Do not override that.

**Option 2 — a dedicated embedding service**

A small FastAPI wrapper around sentence-transformers on the GPU box, plus a
`RemoteEmbeddingProvider`. More moving parts, more control over batching.

Until one exists, **topology B works with CPU embeddings** — correct, just slow
during ingestion. Query-time embedding is one short string and is fine on CPU.

---

## 4. Persistent storage

### 4.1 Backup and restore

```bash
make backup                      # dump to backups/, verified
make restore                     # restore the newest dump
make restore force=1             # overwrite a populated database
make restore-file f=backups/signlaw-20260804T120000Z-rev0003.dump
```

**The dump is verified after writing.** A dump that cannot be read is worse than
no dump, because it looks like protection — so `pg_restore --list` runs before
success is reported, and an unreadable dump is deleted rather than kept.

**Dumps are stamped with their Alembic revision** (`...-rev0003.dump`). Restoring
a dump taken at one schema version into a database at another produces a corpus
that queries successfully and is subtly wrong. `restore.sh` compares the two and
refuses to proceed without `--force`.

**Restore refuses to clobber.** A populated target requires `--force`, then
pauses five seconds. Recovery happens under pressure, and "I meant the other
environment" has no undo.

Custom format (`-Fc`), not plain SQL: compressed, selectively restorable, and
parallel on the way back in.

#### Size

Embeddings live in Postgres, so they are in the dump. A 1024-dimension vector is
~4 KB raw and compresses well in the custom format. Rough shape:

| Corpus | Chunks | Dump size |
|---|---|---|
| 3 cities | ~1,000 | 10–30 MB |
| 30 cities | ~15,000 | 150–400 MB |
| All of BC | ~100,000 | 1–3 GB |

Small enough that off-instance copies are cheap and frequent backups are
reasonable.

#### Off-instance is the whole point

`backup.sh` writes to local disk and then says so. On ephemeral hosting a backup
sitting beside the database protects against nothing:

```bash
make backup
aws s3 cp backups/signlaw-*.dump s3://your-bucket/signlaw/
# or rclone, or scp to a machine that will still exist tomorrow
```

#### Restoring the vector extension

`restore.sh` runs `CREATE EXTENSION IF NOT EXISTS vector` before restoring,
because pg_dump does not reliably recreate extensions in a usable order. The
target must be a `pgvector/pgvector` image or have the extension available.

#### After any restore

```bash
signlaw health          # expect an active collection and a non-zero chunk count
signlaw ask "What is the maximum fascia sign area?"
```

A restore with no active `embedding_collection` will report `index_not_ready` at
query time — `restore.sh` warns about this explicitly.

### Volumes

| Volume | Contents | Size | Class |
|---|---|---|---|
| `pgdata` | Everything authoritative | 5–50 GB | **Must persist** |
| `ollama_models` | Generation weights | ~9 GB per model | Cache |
| `models` | BGE-M3, reranker, tessdata | ~5 GB | Cache |
| `blobs` | Normalised PDFs, OCR output | 1–10 GB | Regenerable |
| `redisdata` | Job queue | < 1 GB | Ephemeral |

`documents/` is a bind mount, not a volume — keep the PDFs in version control or
object storage, never only on the instance.

### Sizing the GPU

On a 24 GB RTX 3090:

| Load | VRAM | Notes |
|---|---|---|
| Qwen 2.5 14B instruct (Q4) | ~9 GB | Default |
| BGE-M3 embedding | ~2.5 GB | Loaded on first use |
| BGE reranker v2-m3 | ~2.5 GB | Loaded on first use |
| **Total** | **~14 GB** | Comfortable headroom |

`OLLAMA_MAX_LOADED_MODELS=1` in the overlay is deliberate: concurrent model
loads on one card cause thrashing rather than throughput.

Room for Qwen 2.5 32B (~20 GB) exists, but not alongside the embedding and
reranker models. If you want 32B, move embeddings off the card or accept eviction.

### Model pre-warming

`OLLAMA_KEEP_ALIVE=30m` keeps the model resident. Loading a 14B model costs
10–30 s, otherwise paid on the first question after any idle period — which
would land squarely in your `answer p95` measurement.

---

## 5. Vast.ai specifics

### Instance selection

| Requirement | Why |
|---|---|
| **≥ 24 GB VRAM** | 14B model + embedding + reranker resident together |
| **≥ 80 GB disk** | Models, images, Postgres, blobs |
| **≥ 32 GB RAM** | PyMuPDF and OCR are memory-hungry on large scanned PDFs |
| **CUDA 12.1+** | Matches the bundled torch wheel |
| **Docker + nvidia-container-toolkit** | Standard on Vast GPU templates |

Prefer **on-demand over interruptible** while Postgres lives on the instance
(topology A). Interruptible is fine once the GPU box is stateless (topology B).

### Docker-in-Docker

Some Vast templates give you a container, not a Docker host. `docker compose`
then needs either a template that exposes the Docker socket, or `--privileged`.
**Check this before renting** — it determines whether topology A is possible at
all on a given instance.

If Docker-in-Docker is unavailable, run Ollama directly on the instance
(topology B with a remote Postgres), which needs no nested containers.

### Ports

Vast maps container ports to external ports. Expose only what you need:

| Port | Service | Expose? |
|---|---|---|
| 8000 | API | Yes, once Phase 5 exists |
| 11434 | Ollama | **No** — no authentication whatsoever |
| 5432 | Postgres | Only for topology B, and then over a tunnel |

Ollama has no auth. An exposed 11434 is an open inference endpoint on your card.

### Cost shape

The GPU is idle most of the time in normal use: ingestion is bursty, questions
are seconds of work. Topology B lets you stop the GPU instance between sessions
and keep the corpus alive on cheap CPU hosting. That is the main practical
argument for the split, beyond durability.

---

## 6. Configuration

`.env` differences from the CPU defaults:

```ini
# GPU
EMBEDDING__DEVICE=cuda
TORCH_VARIANT=cuda

# Ollama runs as a container in the overlay, not on the host
LLM__OLLAMA_BASE_URL=http://ollama:11434
LLM__MODEL=qwen2.5:14b-instruct

# Batch sizes: raise on a 24 GB card, ingestion throughput scales with them
EMBEDDING__BATCH_SIZE=128          # 32 on CPU
RETRIEVAL__RERANK_BATCH_SIZE=64    # 16 on CPU
INGESTION__CONCURRENCY=8           # 4 on CPU

# Keep the model resident between questions
OLLAMA_KEEP_ALIVE=30m
```

Nothing else changes. Provider selection, model names and device are all
configuration — no code differs between CPU and GPU deployments.

### Verifying the GPU is actually in use

```bash
docker compose exec api python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
signlaw health
nvidia-smi        # should show ollama and the api/worker processes
```

If `signlaw health` reports the embedding model ready but ingestion runs at CPU
speed, `EMBEDDING__DEVICE` did not take effect — check it reached the container.

---

## 7. Migration path

1. **Milestone 1 on CPU, locally.** Establishes correctness on hardware you
   control. Correctness thresholds must pass before GPU numbers mean anything.
2. **Topology A on Vast.** Same corpus, same validation suite. Compare only the
   latency and throughput figures; correctness metrics should be identical, and
   any difference is a bug worth chasing.
3. **Split to topology B** once an Ollama or remote embedding provider exists.
4. **Phase 5** — the API — only after all of the above.

Running Milestone 1 on both CPU and GPU is worth the time: identical
correctness numbers across very different hardware is strong evidence the
pipeline is deterministic. A divergence points at something real.

---

## 8. Decisions and deferred work

### Decided

| # | Decision |
|---|---|
| 1 | Postgres must not depend on Vast.ai ephemeral storage. Backup/restore workflow in §4.1. |
| 2 | The Vast GPU instance is replaceable compute: Ollama, LLM inference, optionally GPU embedding and reranking. Nothing authoritative lives there. |
| 3 | Milestone 1 is unchanged. Validate locally on CPU first; no deployment complexity is added before it passes. |
| 4 | `OllamaEmbeddingProvider` after Milestone 1, behind the existing interface. |
| 5 | Topology B is the target; A is a documented fallback. |

### Deferred until Milestone 1 passes

**`OllamaEmbeddingProvider`** — a second implementation of
`EmbeddingProviderProtocol` calling Ollama's `/api/embed`, so the GPU box serves
both generation and embeddings and the CPU host stays stateless.

Two constraints carry over into that work, both already enforced by the schema:

1. **No mixing providers within a collection.** `embedding_collection` is unique
   on `(embedding_model, chunking_version, index_version)`, and
   `embedding_model_revision` records which build produced the vectors. Ollama's
   `bge-m3` and sentence-transformers' `bge-m3` may normalise differently, so
   their vectors are not interchangeable — switching provider must create a new
   collection and re-index. Do not relax this to "reuse the existing collection
   if the model name matches."

2. **Keep `embedding_model_revision` validation.** It is what makes a silently
   republished model distinguishable from the version that built the index.
   `HybridRetriever` already fails loudly when a query embedding's width does not
   match the active collection; revision mismatch should be treated the same way.

The re-index itself is cheap by design: same `chunking_version` means extraction,
OCR, table detection and section parsing are all reused, and only
`chunks → embeddings → index` re-runs.

---

## 9. What has not changed

- No OpenAI, no external API, on any topology
- Postgres + pgvector remains the only vector store
- Embeddings and reranking stay local
- CPU-only development is fully supported; the GPU overlay is additive
- No model weights are baked into any image — `make fetch-models` and
  `ollama pull` populate volumes at deploy time
