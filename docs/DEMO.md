# Local demo — the whole pipeline, no web API

Run the complete system on one machine: put a PDF on disk, index it, ask a
question, get an answer with citations and a confidence score.

Everything is local. Postgres with pgvector, an embedding model from the models
volume, Ollama for generation. **No OpenAI, no external API of any kind.**

---

## One-time setup

```bash
cd signlaw-ai
cp .env.example .env                 # edit DB__PASSWORD

docker compose up -d postgres redis  # pgvector/pgvector:pg16
make install                         # backend/.venv + the `signlaw` command
make migrate                         # apply the schema
make fetch-models                    # embedding + reranker + OCR weights

ollama serve                         # if not already running as a service
ollama pull qwen2.5:14b-instruct
```

Confirm everything is up:

```bash
make health
```

```
SignLaw AI — health
============================================================
  ok    postgres           connected, server 16.4
  ok    pgvector           extension installed, chunk_embedding_1024 ready
  warn  corpus             no active embedding collection
        fix: signlaw ingest <path-to-pdf>
  ok    embedding_model    BAAI/bge-m3 ready (1024d)
  ok    ollama             qwen2.5:14b-instruct ready
============================================================
Ready to answer questions.
```

`corpus` reporting `warn` before the first ingest is expected — the system works,
it just has nothing to search yet.

---

## The demo

```bash
mkdir -p documents/bylaws
cp ~/Downloads/burnaby_sign_bylaw.pdf documents/bylaws/

signlaw ingest documents/bylaws/burnaby_sign_bylaw.pdf
signlaw ask "What is the maximum fascia sign area?"
```

Or all three steps at once:

```bash
make demo
```

### Indexing

```
Ingesting 1 document(s)
  embedding model: BAAI/bge-m3

  indexed  burnaby_sign_bylaw.pdf

284 chunks embedded into signlaw_bge_m3_v1
```

Ingestion runs extraction → OCR (only for pages that need it) → table
extraction → metadata detection → section parsing → chunking → embedding, then
writes everything in one transaction per document. Re-running is a no-op:
documents are identified by content hash.

### Asking

```
Answer
------------------------------------------------------------
A fascia sign must not exceed 20% of the area of the building face
to which it is attached [S1].

Sources
------------------------------------------------------------
  [1] Burnaby
      bylaw    Sign Bylaw No. 13743
      section  5.3(b)
      page     22
      status   in force
      A fascia sign must not exceed twenty percent (20%) of the area of
      the building face to which it is attached.

Confidence
------------------------------------------------------------
  HIGH  (0.86)
  High confidence: the answer is drawn from current bylaw text with
  verified citations to specific sections.

Informational only — not legal advice. Verify with the municipality.
```

Add `--trace` to see retrieval internals — candidate counts, fusion scores,
rerank scores, per-stage timings:

```bash
signlaw ask "What is the maximum fascia sign area?" --trace
```

Add `--city` to scope explicitly, and `--json` for machine-readable output:

```bash
signlaw ask "Are projecting signs allowed?" --city burnaby --json
```

---

## What the pipeline does

```
question
   ↓  query router          intent, municipality, sign types, zones — no LLM call
   ↓  hybrid retrieval      pgvector top 50  +  Postgres FTS top 50
   ↓  weighted RRF          fused on ranks, not incomparable scores
   ↓  cross-encoder rerank  top 50 → top 5
   ↓  context assembly      numbered excerpts with inline provenance
   ↓  Ollama generation     schema-constrained JSON
   ↓  citation verification markers resolve · quotes verbatim · numbers grounded
   ↓  confidence scoring    six signals, never the model's self-report
answer + citations + confidence
```

Every step is recorded in a `PipelineTrace` and written to `chat_message`, so a
disputed answer can be reconstructed months later.

---

## When it declines

Refusing to answer is a feature. Five distinct outcomes, each with its own
response:

| You'll see | What happened |
|---|---|
| `out_of_scope` | Not a sign-bylaw question. Declines before spending a retrieval. |
| `needs_clarification` | "Langley" matches two municipalities with separate bylaws. Asks rather than picking. |
| `no_relevant_bylaw` | Nothing found. Does not reason from a neighbouring city's rules. |
| `only_outdated` | The rule exists, but only in superseded or repealed documents. |
| `conflicting_amendments` | Two in-force documents regulate the same section differently. |
| `unverified` | The model answered but a citation or a number failed verification. |
| `generation_unavailable` | Ollama is down. Retrieved sections are still returned to read directly. |

Try them:

```bash
signlaw ask "What are the sign rules in Langley?"        # asks which Langley
signlaw ask "What is the population of Surrey?"          # declines
signlaw ask "Rules for holographic projection signs?"    # abstains
```

---

## Evaluation

```bash
signlaw eval                 # verified cases only
signlaw eval --all           # exercise unverified cases without scoring
pytest tests/eval -m eval    # same suite as a release gate
```

**Verified cases only, by default.** A case without `verified_by` has
expectations nobody has checked against the real bylaw; scoring against those
measures the guesses, not the system. Behavioural cases (ambiguity, out-of-scope,
abstention) ship verified because their correctness does not depend on the
corpus. Content cases need someone to open the real bylaw, record the section
and page in `app/eval/dataset.py`, and set `verified_by`.

Staleness is zero-tolerance: any citation to superseded or repealed text fails
the run regardless of everything else.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `signlaw: command not found` | `make install` |
| `pgvector: extension not installed` | Use `pgvector/pgvector:pg16`, then `make migrate` |
| `embedding_model: could not load` | `make fetch-models` |
| `ollama: model not pulled` | `ollama pull qwen2.5:14b-instruct` |
| `ollama: cannot reach` | `ollama serve`; from a container use `host.docker.internal` |
| `index_not_ready` | `signlaw ingest <pdf>` |
| Answers cite nothing | Section parsing found no headings — check `--trace` for `section: -` |
