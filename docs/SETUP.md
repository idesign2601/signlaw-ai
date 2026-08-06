# Clean-machine setup for Milestone 1

Everything needed to go from a fresh machine to `make validate`. Follow it in
order; the test sequence in §5 is deliberately smallest-first so a failure tells
you which layer broke.

---

## 1. Required software

| Software | Version | Required? | Notes |
|---|---|---|---|
| **Docker Desktop** | 4.30+ (Compose v2) | Yes | Must provide `docker compose`, not `docker-compose` |
| **Python** | **3.12.x** | Yes | Not 3.13 — torch and sentence-transformers wheels lag a release |
| **Ollama** | 0.5+ recommended | Yes | 0.4 works; see note below |
| **Node** | — | **No** | The frontend is Phase 7 and does not exist yet |
| **make, bash, curl, git** | any | Yes | On Windows: **use WSL2** (see §1.3) |
| **Tesseract + OCRmyPDF** | 5.x / 16+ | Only for host-run OCR | See §1.4 |

### 1.1 Ollama version

Schema-constrained decoding uses Ollama's `format` parameter with a JSON schema.
Support landed in the 0.5 series. On an older Ollama the constraint is ignored
and the synthesizer falls back to parsing the model's JSON — which usually works
but is not guaranteed. **If validation shows a high `unverified` outcome rate,
check your Ollama version first.**

```bash
ollama --version
```

### 1.2 GPU / CUDA

**Optional. Everything runs on CPU.** What changes is speed:

| Setup | Embedding (BGE-M3) | Generation (Qwen 14B) | Verdict |
|---|---|---|---|
| CPU only, 16 GB RAM | ~2–5 chunks/s | ~60–120 s/answer | Works; use the 7B model |
| CPU only, 32 GB RAM | ~5–10 chunks/s | ~30–60 s/answer | Usable |
| NVIDIA ≥ 8 GB VRAM | ~50–150 chunks/s | ~3–8 s/answer | Comfortable |
| NVIDIA ≥ 16 GB VRAM | ~150+ chunks/s | ~2–5 s/answer | Ideal |
| Apple Silicon (MPS) | ~20–60 chunks/s | ~5–15 s/answer | Good |

These are rough orders of magnitude, not measurements — nothing has been
benchmarked yet. That is what Milestone 1 is for.

If CUDA is present, install a CUDA-enabled torch **before** `make install`:

```bash
cd backend && python -m venv .venv
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu121
```

Then set `EMBEDDING__DEVICE=cuda` in `.env`. Leave it at `auto` otherwise —
CUDA and MPS are detected automatically.

**If you have under 16 GB of RAM**, use the smaller generation model:

```bash
ollama pull qwen2.5:7b-instruct
# .env:  LLM__MODEL=qwen2.5:7b-instruct
```

The `answer p95 ≤ 30 s` threshold is unlikely to hold on CPU with a 14B model.
That is a hardware finding, not a defect — record the number and note the
hardware.

### 1.3 Windows

The Makefile and scripts are POSIX (`.venv/bin/...`, `bash scripts/*.sh`).
**Run everything from WSL2.** Native Windows Python puts entry points in
`.venv/Scripts/` and the Makefile will not find them.

```powershell
wsl --install -d Ubuntu-22.04
```

Then, inside WSL2:

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv build-essential make curl git
```

Enable Docker Desktop's WSL2 integration (Settings → Resources → WSL Integration).

Keep the repo **inside the WSL2 filesystem** (`~/signlaw-ai`), not on `/mnt/d/`.
Cross-filesystem I/O is roughly 10× slower and PDF ingestion is I/O heavy.

### 1.4 Tesseract — only if running the backend on the host

The `worker` Docker image installs the OCR stack. If you run the backend on the
host (which §5 does, because it is faster to debug), OCR needs local binaries:

```bash
# Ubuntu / WSL2
sudo apt install -y tesseract-ocr ghostscript qpdf pngquant unpaper

# macOS
brew install tesseract ghostscript qpdf pngquant unpaper
```

**Language data is not installed by this** — `make fetch-models` downloads it to
`data/models/tessdata/`. That is deliberate: no trained model is ever baked into
an image.

Without these binaries, ingestion still works; scanned pages are skipped and
flagged. `signlaw health` reports it.

### 1.5 Disk space

Roughly **25 GB**:

| Item | Size |
|---|---|
| Python deps incl. torch | ~5 GB |
| BGE-M3 embedding model | ~2.3 GB |
| BGE reranker v2-m3 | ~2.3 GB |
| Qwen 2.5 14B (via Ollama) | ~9 GB |
| Docker images | ~2 GB |
| Postgres volume + blobs | ~1 GB+ |

Check before starting — insufficient disk is what prevented any of this being
run during development:

```bash
df -h .
```

---

## 2. Commands from a clean machine

### 2.1 Clone and install

```bash
git clone <your-repo-url> signlaw-ai
cd signlaw-ai

make install          # creates backend/.venv, installs .[dev,local]
```

Confirm the CLI is on the path:

```bash
backend/.venv/bin/signlaw --help
```

### 2.2 Environment file

```bash
cp .env.example .env
```

Edit `.env`. For the host-backend + Docker-Postgres layout in §5, these are the
only lines that must change:

```ini
DB__HOST=localhost
DB__PASSWORD=<pick something>
LLM__OLLAMA_BASE_URL=http://localhost:11434
```

Leave everything else at its default. The defaults are local-first: Ollama for
generation, BGE-M3 for embeddings, no API key anywhere.

Two settings worth knowing about:

```ini
EMBEDDING__DEVICE=auto        # cuda | mps | cpu to force it
LLM__MODEL=qwen2.5:14b-instruct   # or qwen2.5:7b-instruct on <16 GB RAM
```

### 2.3 Database

```bash
docker compose up -d postgres
docker compose ps postgres            # wait for "healthy"
```

The image is `pgvector/pgvector:pg16`. **Stock `postgres:16` will not work** —
migration `0003` needs `CREATE EXTENSION vector`.

### 2.4 Migrations

```bash
make migrate                          # alembic upgrade head
```

Verify three migrations applied:

```bash
cd backend && .venv/bin/alembic current    # expect 0003 (head)
```

### 2.5 Models

```bash
make fetch-models                     # tessdata + BGE-M3 + reranker -> data/models/
ollama serve                          # separate terminal, if not already a service
ollama pull qwen2.5:14b-instruct
ollama list
```

Nothing here touches a Docker image. For an air-gapped install, run
`make fetch-models` on a connected machine and copy `data/models/` across.

### 2.6 Verify the whole stack

```bash
make health
```

Expect `ok` for postgres, pgvector, embedding_model and ollama. `corpus` will
report `warn` — correct, nothing is indexed yet.

---

## 3. PDF folder structure

**Use flat files with the municipality in the filename.**

```
documents/bylaws/
    burnaby_sign_bylaw.pdf
    vancouver_sign_bylaw.pdf
    surrey_sign_bylaw.pdf
```

### Why not per-city subfolders

Your proposed layout —

```
documents/bylaws/burnaby/sign_bylaw.pdf     # ← do not use
```

— will ingest, but the municipality will not be detected from the path.
`MetadataDetector._from_filename` reads `Path(filename).stem`, which is the
**basename only**. The parent directory is never consulted. So
`burnaby/sign_bylaw.pdf` gives the detector nothing to work with, and it falls
back to whatever the cover page says.

Additionally, `validate.sh`'s preflight uses `find -iname "*burnaby*.pdf"`, which
matches basenames. Nested files named `sign_bylaw.pdf` would fail preflight even
though the folder says `burnaby`.

Subfolders are fine **as long as the filename still carries the city**:

```
documents/bylaws/
    burnaby/burnaby_sign_bylaw.pdf        # works
    vancouver/vancouver_sign_bylaw.pdf    # works
```

But the flat layout is simpler and is what the scripts assume.

### Naming that helps detection

The detector reads filename → regex over the first three pages → municipality
registry. A filename like `burnaby_sign_bylaw_13743_2020.pdf` gives it the
municipality, bylaw number and year before it opens the file.

Avoid four-digit numbers that could be years when they are bylaw numbers — the
detector treats a bare `2020` as a year, not a bylaw number.

### Getting the documents

Download each from its municipality's own bylaw or records page. Search for
"sign bylaw" on each city's site. **Prefer the consolidated version** where one
exists — it will say "Consolidated for convenience to <date>" on the cover, and
the detector uses that as the version date.

If a municipality only publishes a base bylaw plus separate amendments, put them
all in the folder. Lineage resolution needs the full set to work out what is in
force; a lone amendment with no base is what produces `UNKNOWN` status.

---

## 4. First test sequence

**Run these in order.** Each isolates a different layer, so a failure tells you
where the problem is. Do not skip ahead — nothing in this project has ever been
compiled, and step 1 exists to catch that.

### Step 1 — does the code compile at all

```bash
make verify-fast          # lint, types, unit + e2e tests. No Docker, no Postgres.
```

Roughly 30 seconds. **Expect this to fail on the first attempt.** The most
likely cause is `filterwarnings = ["error"]` in `backend/pyproject.toml`, which
turns any third-party `DeprecationWarning` into a test failure.

Send me the output. Do not proceed until it passes.

### Step 2 — database and schema

```bash
docker compose up -d postgres
make migrate
cd backend && .venv/bin/alembic current       # expect 0003 (head)
```

Then check migrations reverse cleanly — a migration that cannot roll back cannot
be safely deployed:

```bash
cd backend
.venv/bin/alembic downgrade base
.venv/bin/alembic upgrade head
```

### Step 3 — dependencies

```bash
make health
```

Every component `ok` except `corpus`, which should be `warn`.

### Step 4 — one document end to end

Start with **one** PDF, not three. If ingestion has a problem you want it on the
smallest possible input.

```bash
signlaw ingest documents/bylaws/burnaby_sign_bylaw.pdf
```

Check the numbers it reports. The one that matters most:

```bash
signlaw validate --json | python -m json.tool | grep -A3 sections
```

**If `sections` is 0 for a document, stop.** Every chunk from it is uncitable at
clause level, and no amount of downstream quality will fix that. It means the
section parser found no headings in that bylaw's formatting, and I need to see a
sample of the text to fix the patterns.

Then ask something:

```bash
signlaw ask "What is the maximum fascia sign area?" --trace
```

`--trace` shows retrieval scores and per-stage timings. Worth reading even when
it works.

### Step 5 — full static verification

```bash
make verify               # adds Docker builds and integration tests
```

### Step 6 — the rest of the corpus

```bash
signlaw ingest documents/bylaws/ --force
make demo
```

### Step 7 — Milestone 1

```bash
make validate
```

Writes to `reports/`:

- `validation-<stamp>.json` — every metric
- `spot-check-<stamp>.md` — the worksheet for the human review
- `ingest-<stamp>.json` — per-document ingestion detail

---

## 5. What to send me

For each failing step:

1. The **full command** you ran
2. **Complete output** including the traceback — not just the last line
3. `signlaw health --json` output
4. Your hardware: OS, RAM, GPU/VRAM
5. `ollama --version` and `python --version`

If step 4 produces answers but they look wrong, include the `--trace` output —
retrieval scores usually reveal whether the problem is retrieval or generation.

If validation completes, send `reports/validation-<stamp>.json` whole. I would
rather read the raw numbers than a summary.

---

## Quick reference

```bash
# from clean
make install && cp .env.example .env      # edit DB__PASSWORD
docker compose up -d postgres
make migrate
make fetch-models
ollama pull qwen2.5:14b-instruct

# verify in order
make verify-fast                          # ← start here
make health
signlaw ingest documents/bylaws/burnaby_sign_bylaw.pdf
signlaw ask "What is the maximum fascia sign area?" --trace
make verify
make validate
```
