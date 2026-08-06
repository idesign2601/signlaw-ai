# Milestone 1 — Production Validation

**Status: PARTIALLY RUN — stages 1–4 pass, stages 5–7 not run.**

`make verify` passes on a real machine: Docker images build, Postgres with
pgvector comes up, migrations round-trip `up → down → up → check` with no drift,
and ruff, mypy `--strict` and the unit, e2e and integration suites are green.
That covers stages 1–4.

Stages 5–7 have never run. The Burnaby, Vancouver and Surrey bylaws are not in
the repo, so nothing has been ingested, no question has been answered, and every
number below is still blank.

**Do not treat any metric in this document as evidence until an actual run fills
it in.**

---

## How to run it

```bash
# 1. Put the real bylaws in place. Names help metadata detection.
mkdir -p documents/bylaws
#   documents/bylaws/burnaby_sign_bylaw.pdf
#   documents/bylaws/vancouver_sign_bylaw.pdf
#   documents/bylaws/surrey_sign_bylaw.pdf
#   (download each from its municipality's bylaw page)

# 2. Run everything.
make validate
```

`scripts/validate.sh` runs seven stages and stops at the first failure, because
a report built on a broken stage measures nothing:

| Stage | Checks |
|---|---|
| 1 | Docker images build |
| 2 | Postgres + pgvector up, migrations at head |
| 3 | Health: Postgres, pgvector, embedding model, Ollama |
| 4 | ruff, mypy, unit + e2e tests |
| 5 | Ingest the three bylaws |
| 6 | Run the validation suite |
| 7 | Write report + spot-check worksheet to `reports/` |

---

## What the harness can and cannot measure

This distinction is the reason the milestone needs a person, not just a script.

**Measured automatically** — no ground truth needed:

| Metric | How |
|---|---|
| Ingestion success | Documents reaching `indexed` |
| Section detection | Sections parsed per document — zero means uncitable chunks |
| Embedding time | Chunks per second |
| Retrieval latency | p50 / p95 from the trace |
| Answer latency | p50 / p95 end to end |
| Citation verifiability | Quote found verbatim in the chunk it came from |
| Retrieval precision | Fraction of retrieved chunks from the asked-about city |
| Abstention rate | Split into correct and false abstentions |
| Confidence distribution | Counts per band |

**Not measurable without a human:**

| Metric | Why |
|---|---|
| Factual correctness | Needs someone with the bylaw open |
| Citation *accuracy* | Verifying the quote exists ≠ it being the right clause |
| Confidence calibration | Needs correctness labels, which need the above |

The harness writes `reports/spot-check-<stamp>.md` with every answer, its
citations and its confidence band laid out for review. **Calibration is reported
as `NOT MEASURED` rather than estimated.**

---

## Acceptance thresholds

Defined in `app/validation/thresholds.py` so "did it pass?" has one answer.

### Correctness — do not relax after seeing results

| Criterion | Threshold | Result |
|---|---|---|
| Ingestion success | 100% | — |
| Sections detected in every document | all | — |
| Stale citations | **0** | — |
| Citation verification rate | ≥ 95% | — |
| Answers carrying citations | ≥ 90% | — |
| Municipality precision | ≥ 90% | — |
| Uncovered-city question abstains | **must** | — |
| Correct abstention rate | ≥ 90% | — |
| False abstention rate | ≤ 20% | — |
| Behaviour accuracy | ≥ 90% | — |
| Errors | 0 | — |

### Performance — hardware dependent, adjust with justification

| Criterion | Threshold | Result |
|---|---|---|
| Retrieval p95 | ≤ 2,000 ms | — |
| Answer p95 | ≤ 30,000 ms | — |
| Embedding throughput | ≥ 5 chunks/s | — |

Two are absolute and cannot be traded against anything else:

- **Zero stale citations.** One answer resting on repealed text is a defect.
- **The uncovered-city probe must abstain.** "What is the maximum fascia sign
  area in Kelowna?" answered from Burnaby's bylaw is precisely the failure this
  product exists to prevent. Kelowna is deliberately excluded from the corpus.

---

## Results

*(Blank until `make validate` runs.)*

### Ingestion

| Document | Municipality | Bylaw | Pages | Sections | Chunks | Tables | OCR pages | Time |
|---|---|---|---|---|---|---|---|---|
| burnaby_sign_bylaw.pdf | — | — | — | — | — | — | — | — |
| vancouver_sign_bylaw.pdf | — | — | — | — | — | — | — | — |
| surrey_sign_bylaw.pdf | — | — | — | — | — | — | — | — |

### Latency

| Stage | p50 | p95 |
|---|---|---|
| Retrieval | — | — |
| Generation | — | — |
| End to end | — | — |

### Citations, behaviour, retrieval, confidence

*(Filled by the harness.)*

---

## Human spot check

Required. Open each bylaw and mark every answer `correct` / `wrong` /
`unsupported` in `reports/spot-check-<stamp>.md`.

**A wrong answer carrying HIGH confidence is a calibration defect and blocks the
milestone**, whatever the automatic thresholds say. That combination is the
specific failure this system's confidence scoring exists to prevent, so it
cannot be waived.

Feed each verdict back into `app/eval/dataset.py` with a real section number and
`verified_by` set. That is how the golden set gets built — from verified reality,
not from guesses.

---

## First-run risks

Stages 1–4 have now run, so the risks below are narrowed to what ingestion and
answering will exercise for the first time. They are predictions, not known bugs.

**Already hit and fixed** during stages 1–4, recorded so they are not
re-litigated:

| Found | Cause |
|---|---|
| `filterwarnings = ["error"]` failures | Starlette's deprecated `HTTP_422_UNPROCESSABLE_ENTITY`. Fixed. |
| `alembic check` reported ~15 phantom constraint changes | Migrations passed already-prefixed names to `sa.CheckConstraint` while the metadata convention `ck_%(table_name)s_%(constraint_name)s` prefixed them again. Fixed by using bare names. |
| Every integration test failed authentication | The autouse `isolated_env` fixture pinned a fake `DB__PASSWORD` for tests that need real Postgres. Integration tests are now exempt. |

Still untested:

| Risk | Where | Symptom |
|---|---|---|
| Section regexes over- or under-match | `domain/section_parser.py` | Zero sections detected, or a section per line. Caught by the "sections detected" threshold. |
| PyMuPDF `find_tables` API drift | `ingestion/tables.py` | Table detection silently returns nothing. |
| Ollama `format` schema support | `adapters/llm/ollama.py` | Older Ollama ignores JSON-schema constraint; the parser fallback should cover it. |
| `SET LOCAL hnsw.ef_search` outside a transaction | `rag/retriever.py` | No-op or error depending on session state. |
| Model dimension mismatch | `adapters/embeddings/local.py` | Caught at load and reported clearly — should fail fast, not corrupt the index. |
| Context window overflow on 3-way comparison | `rag/prompts.py` | Five long sections plus instructions may exceed `num_ctx=16384`. |

Expect stage 5 to fail on the first attempt. That is what it is for.

---

## Sign-off

Milestone 1 passes when **all three** hold:

1. Every blocking threshold passes in an actual run
2. The spot-check worksheet is complete
3. No wrong answer carries HIGH confidence

Phase 5 does not begin before that.

| | Name | Date |
|---|---|---|
| Ran validation | | |
| Completed spot check | | |
| Approved | | |
