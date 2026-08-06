# Vast.ai runbook — Milestone 1 on a rented GPU

Step-by-step for moving SignLaw AI off a CPU workstation onto an RTX 3090.
Companion to `DEPLOYMENT.md`, which explains *why*; this one is *what to type*.

**Topology A** (everything on one instance). Topology B is the eventual target
but needs `OllamaEmbeddingProvider`, which does not exist yet — see
`DEPLOYMENT.md` §3.

---

## 0. Before you destroy anything local

The corpus PDFs are gitignored and exist nowhere else. Vancouver's cannot be
re-downloaded by script — `bylaws.vancouver.ca` returns 403 to non-browser
clients. **Copy them off before cleaning up the local machine.**

Everything else is safe to lose:

| Local artefact | Recovery |
|---|---|
| Repository | `git clone` |
| `data/models/` (~4.5 GB) | `make fetch-models` |
| Postgres volume | Re-ingest |
| `.env` | Six lines, retyped |
| **`documents/bylaws/*.pdf`** | **Vancouver: manual browser download only** |

---

## 1. Choose the instance

| Requirement | Why |
|---|---|
| ≥ 24 GB VRAM | 14B model + embedding + reranker resident together |
| ≥ 80 GB disk | ~15 GB models, ~10 GB images, Postgres, blobs |
| ≥ 32 GB RAM | PyMuPDF and OCR are memory-hungry on scanned PDFs |
| CUDA 12.1+ | Matches the bundled torch wheel |
| **Working Docker** | Not all Vast templates give you a Docker host |
| On-demand, not interruptible | Postgres lives on this instance in topology A |

**Verify Docker before you trust the instance.** Some templates hand you a
container rather than a Docker host, and `docker compose` then fails in ways
that look like configuration errors:

```bash
docker run --rm hello-world
docker compose version
nvidia-smi
```

If `docker run` fails, stop. Either pick a template that exposes the Docker
socket, or accept running Postgres and Ollama natively instead of in compose.

---

## 2. Host packages

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr postgresql-client-16 git curl rsync
```

`tesseract-ocr` is not optional. Without it, pages lacking a text layer are
skipped silently — on the Surrey bylaw that is 12 of 52 pages, and every
citation that should have come from them is simply absent. The failure looks
like "the answer wasn't in the corpus" rather than like a broken install.

---

## 3. Code and Python environment

```bash
git clone https://github.com/idesign2601/signlaw-ai.git ~/signlaw-ai
cd ~/signlaw-ai
make install
```

`make install` builds `backend/.venv`. Keep it: `scripts/validate.sh` invokes
`$BACKEND_DIR/.venv/bin/...` throughout, so a fully containerised deployment
would need the script reworked. Running the Python from a host venv with
Postgres in Docker mirrors the CPU setup exactly, which means any difference in
results is hardware, not configuration.

---

## 4. Corpus

From the **local machine**, using the SSH details Vast gives you:

```bash
scp -P <port> ~/projects/signlaw-ai/documents/bylaws/*.pdf root@<host>:~/signlaw-ai/documents/bylaws/
```

Create the directory on the instance first if scp complains:

```bash
mkdir -p ~/signlaw-ai/documents/bylaws
```

Verify on the instance — a saved error page has a `.pdf` extension and fools
`ls` but not `file`:

```bash
ls -lh ~/signlaw-ai/documents/bylaws/ && file ~/signlaw-ai/documents/bylaws/*.pdf
```

Three lines, each reading "PDF document".

---

## 5. Configuration

```bash
cd ~/signlaw-ai && cp .env.example .env
```

Append the GPU settings:

```ini
EMBEDDING__DEVICE=cuda
EMBEDDING__BATCH_SIZE=128
RETRIEVAL__RERANK_BATCH_SIZE=64
INGESTION__CONCURRENCY=8
LLM__MODEL=qwen2.5:14b-instruct
OLLAMA_KEEP_ALIVE=30m
```

Do **not** carry over `EMBEDDING__DEVICE=cpu` or `EMBEDDING__BATCH_SIZE=8` from
the CPU machine. Those were workarounds for a 4 GB card and would quietly cost
you most of the GPU's throughput.

---

## 6. Services

```bash
docker compose up -d postgres
cd backend && .venv/bin/alembic upgrade head && cd ..
```

```bash
curl -fsSL https://ollama.com/install.sh | sh
nohup ollama serve > /tmp/ollama.log 2>&1 &
ollama pull qwen2.5:14b-instruct
```

```bash
make fetch-models
```

---

## 7. Confirm the GPU is genuinely in use

**Do this before measuring anything.** CPU-speed numbers from a rented GPU are
worse than no numbers, because they look authoritative.

```bash
cd backend && .venv/bin/python -c \
  "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
cd ..
make health
```

Expect `True NVIDIA GeForce RTX 3090` and six green lines from `health`
(the corpus warning stays until you ingest).

During ingestion, `nvidia-smi` should show the python process holding VRAM. If
it does not, `EMBEDDING__DEVICE` did not take effect.

---

## 8. Run the validation

```bash
make validate
```

Seven stages, stopping at the first failure. Output lands in `reports/`:

- `ingest-<stamp>.json` — per-document extraction results
- `validation-<stamp>.json` — every automatic metric
- `spot-check-<stamp>.md` — the worksheet a human must complete

### Reference figures from the CPU run

Correctness numbers should match; only latency and throughput should move. A
divergence in a *correctness* metric is a bug worth chasing, not noise.

| | Burnaby | Surrey |
|---|---|---|
| Pages | 27 | 52 |
| Sections | 69 | 63 |
| Chunks | 57 | 76 |
| Tables | 2 | 1 |
| Metadata confidence | 0.88 | 0.88 |
| Mean extraction confidence | 1.00 | 0.77 *(no OCR available)* |

Surrey's chunk count and confidence **should both rise** on the instance, since
tesseract is installed there and its 12 scanned pages will actually be read.
If they do not, OCR is not running — check `signlaw health` and the ingest log.

Embedding throughput on CPU was ~0.15 chunks/s against a ≥5 threshold. Expect
two to three orders of magnitude better on the 3090.

---

## 9. Back up before you celebrate

```bash
make backup
```

The dump is verified on write and stamped with its Alembic revision. Then get
it off the instance — a backup sitting beside the database on ephemeral
hosting protects against nothing:

```bash
scp -P <port> ~/signlaw-ai/backups/signlaw-*.dump you@somewhere-permanent:/backups/
# or: aws s3 cp ... / rclone copy ...
```

Postgres holds the only unrecoverable data: the corpus, the embeddings, the
amendment lineage, and the retrieval traces that let a disputed answer be
reconstructed months later. For a legal tool the traces matter most.

---

## 10. The part no script can do

`make validate` reports confidence calibration as `NOT MEASURED`, deliberately.
Measuring it needs correctness labels, and correctness labels need a person with
the bylaws open.

Open `reports/spot-check-<stamp>.md` and mark every answer `correct`, `wrong` or
`unsupported`.

**A wrong answer carrying HIGH confidence blocks the milestone**, whatever the
automatic thresholds say. That exact combination is what the confidence scoring
exists to prevent, so it cannot be waived.

Feed each verdict back into `app/eval/dataset.py` with a real section number and
`verified_by` set. That is how the golden set gets built — from verified
reality, not from guesses.

Milestone 1 passes when all three hold:

1. Every blocking threshold passes in an actual run
2. The spot-check worksheet is complete
3. No answer marked wrong carries HIGH confidence

---

## 11. Only now, clean up the local machine

After §9's dump is stored somewhere permanent and §8 has produced a report:

```bash
docker compose down -v            # drops the local pgdata volume
rm -rf ~/projects/signlaw-ai/data/models
```

Keep the local clone — it costs nothing and gives you somewhere to review the
reports offline. If you want it gone entirely, confirm first that the PDFs are
on the instance and the dump is off it.

**Stop the Vast instance when idle.** The GPU sits unused between ingestion
runs; questions are seconds of work. This is the main practical argument for
eventually splitting to topology B.
