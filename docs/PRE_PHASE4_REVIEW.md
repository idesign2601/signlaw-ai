# Architecture review — before Phase 4

Phase 3 is complete and the gate is green: 682 tests, migrations round-tripping
through 0006, ruff and mypy `--strict` clean.

That sentence is worth being precise about. **Green means the code does what it
was written to do. It does not mean the answers are right**, because no answer
has yet been measured against a real bylaw.

Ordered by consequence.

---

## 1. Nothing has been measured

Milestone 1 has never produced a number. The harness, thresholds and spot-check
worksheet have existed for weeks and have never run to completion.

Until this week that was impossible: the lineage pass was never called, so every
document sat at `unknown`, the `in_force` filter excluded all of them, and every
question would have answered "found only superseded or repealed text". That is
fixed. The run can now happen.

What is unmeasured, specifically:

- Whether retrieval finds the right section
- Whether citations point at text that supports the claim
- **Whether a HIGH confidence badge predicts a correct answer** — the single
  claim the product rests on
- Every latency figure

Phase 3 added four features that all consume retrieval. If retrieval is weaker
than assumed, all four inherit it, and the compliance engine inherits it into a
number someone fabricates from.

**This should be the next thing done, before any Phase 4 feature.**

---

## 2. The ingestion worker still does not exist

`Makefile` defines `worker: arq app.ingestion.worker.WorkerSettings`.
`docker-compose.yml` defines a `worker` service. `app/ingestion/worker.py` does
not exist. Both commands fail.

Consequences, all live today:

- Admin uploads run in the API process via `BackgroundTasks`. A 300-page scanned
  bylaw occupies the event loop that answers questions.
- The upload is buffered fully in memory — `file: bytes = File()`. Fifty
  megabytes multiplied by concurrent uploads is unbounded.
- A restart mid-ingest loses the work and leaves the job row at `running`
  forever, with nothing to reconcile it.

It also blocks delete, replace and re-index, which all need somewhere to run.

This is the largest structural gap in the repository.

---

## 3. Rate limiting is configured and unenforced

`SECURITY__RATE_LIMIT_PER_MINUTE` exists in settings. Nothing reads it. A
`RateLimited` exception exists and is never raised.

Phase 3 made this worse, not better. `/compare` issues one retrieval per
municipality per dimension — comparing three cities across three dimensions is
nine retrievals and nine rerank passes for one request. `/compliance/check`
issues one per dimension. The API key is currently the only thing between a
caller and the GPU.

On a single card, generation throughput is the capacity ceiling. Rate limiting
is not a hardening task here; it is capacity management.

---

## 4. The compliance parser has never seen a real bylaw

`parsing.py` was written against phrasings I expected, and its tests use
synthetic sentences I also wrote. That is circular.

The refusals are what matter — "as set out in Schedule B", ranges, wrong units.
A parser that reads a section number as an area produces a confident wrong
limit, and it is the output with the highest consequence in the product.

Once Burnaby and Surrey are ingested, run the parser over their actual sign-area
and height sections and check what it reads and what it declines. Expect to add
refusal cases, not extraction cases.

---

## 5. No zoning provider is verified

The architecture is complete; no city can actually be looked up. Every preset
ships `verified: false`, and unconfirmed ones ship with no endpoint.

That is the correct state — shipping guessed endpoints would produce confidently
wrong zoning — but it means the zoning feature is architecture without data.
Verifying one city end to end would prove the seam. Vancouver needs a geocoder
first, since its dataset is polygons with no address field; an ArcGIS city with
an address-bearing parcel layer is the easier first proof.

This is research per municipality, not code.

---

## 6. Admin cannot delete, replace or re-index

Upload and list exist. The rest is CLI-only.

Deletion needs a distinction the schema already supports but the API does not
expose: a **repealed** bylaw should be marked repealed and kept — its text is
still the right answer to "what did the rule used to be", and deleting it
destroys the audit trail for answers already given. Deletion belongs to
documents ingested in error, and should be a different operation with a
different name.

---

## 7. Smaller, but worth listing

- **Citations carry `source_url: null`.** Serving source PDFs is unbuilt, so the
  primary verification path is manual. For a citation-first product this is the
  most valuable missing feature after the ones above.
- **`verify_model_on_startup` is configured and unused.** A typo in `LLM__MODEL`
  surfaces as a failed answer under load rather than a refusal to boot.
- **No as-at query.** The schema supports historical versions —
  `bylaw_relation`, `consolidation_date`, `last_amendment_date` — and nothing
  can ask "what did Burnaby's rule say in 2019".
- **`frontend/` cannot run as committed.** No Laravel skeleton; the README
  documents a manual overlay. Fine for one operator, a trap for anyone else.
- **The out-of-scope message hardcodes "British Columbia"** in prose, in
  `rag_service.py`. It becomes wrong the day Alberta is ingested and it is not
  configuration.

---

## What Phase 4 should probably be

Not features.

1. **Run Milestone 1.** Convert the untested majority of this repository into a
   measured one.
2. **Build the worker.** Then delete, replace and re-index on top of it.
3. **Enforce rate limiting.**
4. **Verify one zoning provider end to end.**
5. **Serve source documents**, so a citation can be clicked.

Every one of those makes what already exists trustworthy. None adds surface
area. Given that the product's entire proposition is "answers you can check",
that ordering is not conservatism — it is the product.

---

## What is genuinely solid

Worth recording, so the list above is read in proportion:

- The citation path is sound end to end: retrieval carries provenance, quotes
  are verified verbatim before display, confidence is decomposed and explained.
- Currency is treated as a corpus property rather than a document property, and
  now actually resolved.
- The ambiguity discipline holds everywhere it should — two Langleys, two North
  Vancouvers, addresses, comparisons, zoning.
- Expansion is real, not aspirational: adding Edmonton is a database row, and
  there is a test asserting Edmonton appears nowhere in the code.
- The compliance engine encodes no regulation. When a bylaw changes, it fails
  visibly instead of answering wrongly.
- The verification gate on zoning configuration means an unchecked endpoint
  cannot go live by accident.
