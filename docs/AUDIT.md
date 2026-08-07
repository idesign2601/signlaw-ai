# Repository audit — August 2026

Full review of architecture, backend, database, API, frontend, deployment,
security and expansion readiness, ahead of Phase 3.

Findings are ordered by consequence, not by area.

---

## P0 — The corpus is unreachable

**Nothing this system indexes can currently be retrieved.** Every question will
answer "I found bylaw text that addresses this, but only in documents that are
superseded or repealed."

Three correct components, one missing wire:

1. `MetadataDetector` deliberately never infers currency
   (`metadata.py:271`): `status=DocumentStatus.UNKNOWN`, because "in force" is a
   statement about a document's relationship to every *other* document. Right.
2. `HybridRetriever._filters` applies `d.status = 'in_force'` by default
   (`retriever.py:341`), so repealed text cannot surface by accident. Also right.
3. `LineageResolver` (`ingestion/amendments.py`) is what promotes a document
   from `unknown` to `in_force`. **It is never called.** Nothing outside its own
   module and its tests imports it.

So every document sits at `unknown` forever, the `in_force` filter excludes all
of them, retrieval returns zero chunks, and `RagService._nothing_found` re-probes
without the filter, finds the same chunks, sees they are not `in_force`, and
reports `ONLY_OUTDATED`.

**This is worse than an outright failure**, because the message is plausible. An
operator reads "only superseded text found" and goes looking for fresher PDFs
that do not exist. It would have been blamed on the corpus or the model.

It also explains why `bylaw_relation` is empty in every run so far, and why
`make validate` was never going to pass regardless of GPU.

**Fix:** run the lineage pass at the end of `IngestionService.ingest_paths`,
over every document in the corpus rather than only the batch — currency is a
property of the whole corpus, so a single-file upload must re-resolve the
municipality's other documents too. Roughly 60 lines plus tests. Nothing else in
Phase 3 is worth doing before this.

---

## P1 — Significant gaps

### The ingestion worker does not exist

`Makefile` has `worker: arq app.ingestion.worker.WorkerSettings` and
`docker-compose.yml` defines a `worker` service under a profile. There is no
`app/ingestion/worker.py`. Both commands fail.

Consequence: the admin upload added in Phase 5 runs ingestion via FastAPI
`BackgroundTasks`, **inside the API process**. A 300-page scanned bylaw then
occupies the same event loop that answers questions, and the work is lost
entirely if the process restarts mid-ingest — leaving the job row stuck at
`running` forever.

### No delete, replace or re-index

Requested and absent. Only `POST /admin/documents/upload` and
`GET /admin/documents` exist. Re-indexing after a model change is CLI-only.

Deletion in particular needs care: `ON DELETE CASCADE` removes chunks and
embeddings, but the *reason* matters. A bylaw that was repealed should be marked
repealed and kept — its text is still the correct answer to "what did the rule
used to be", and deleting it destroys the audit trail for answers already given.
Deletion should be reserved for documents ingested in error.

### Rate limiting is configured and unenforced

`SECURITY__RATE_LIMIT_PER_MINUTE` exists in settings; nothing reads it. A
`RateLimited` exception class exists and is never raised. The API key is
currently the only thing between one caller and unlimited GPU time.

### `verify_model_on_startup` is configured and unused

Settings promise the configured LLM is checked at boot. It is not. A typo in
`LLM__MODEL` surfaces as a failed answer under load rather than a refusal to
start.

### No public `/documents` endpoint

Requested. Only the admin list exists, behind `X-Admin-Key`. Users cannot see
what a citation refers to, and `Citation.source_url` is permanently `null`.

### No historical version query

The schema supports it — `bylaw_relation`, `DocumentStatus`,
`consolidation_date`, `last_amendment_date` — but there is no way to ask "what
did Burnaby's rule say in 2019". The data is there; the retrieval path has no
as-at parameter.

---

## P2 — Expansion is blocked in three specific places

The architecture is broadly province-agnostic. Three concrete things are not.

1. **`IngestionService._resolve_municipality` hardcodes British Columbia**
   (`ingestion_service.py:249`): `SELECT id FROM province WHERE code = 'BC'`.
   A Calgary document would be filed under BC.

2. **`MunicipalityRegistry` defaults to `BC_MUNICIPALITIES`.** Alberta
   municipalities are catalogued in `domain/provinces.py` for coverage display
   but do not resolve during query routing — deliberate today, and exactly the
   line that has to move when Calgary is ingested.

3. **User-visible copy hardcodes the province.** The `OUT_OF_SCOPE` message says
   "I only answer questions about municipal sign bylaws in British Columbia",
   and the FastAPI description says the same. Both become wrong the day Alberta
   lands, and neither is configuration.

Only BC exists in the `province` table (seeded in migration 0002).

---

## P3 — Debt

- **`frontend/` cannot run as committed.** No `composer.json`, no Laravel
  skeleton — the README documents a manual overlay step. Fine for now,
  a trap for anyone else who clones it.
- **Citations carry a dead `source_url`.** Deliberate, documented, but it means
  the primary verification path is manual.
- **`num_ctx` was hardcoded until this week.** Fixed; worth noting as a pattern —
  check for other constructor defaults that should be settings.
- **Two divergent copies of the repository** during development caused real
  confusion. Now resolved via git, but `frontend/` went missing once already.

---

## Security review

| Risk | Severity | State |
|---|---|---|
| Unauthenticated `/ask` | High | **Fixed** — `X-API-Key` on the router |
| Ollama exposed | High | Documented as never-expose; not enforced by code |
| Admin routes unauthenticated | High | **Fixed** — `X-Admin-Key`, refuses to boot in production without it |
| No rate limiting | Medium | **Open** — config exists, unenforced |
| Upload read fully into memory | Medium | **Open** — `bytes = File()`; 50 MB × concurrent uploads is unbounded RAM |
| Ingestion in the API process | Medium | **Open** — a large upload degrades answering |
| Path traversal on upload | — | Mitigated: slug validated, title sanitised, path re-checked |
| PDF content-type spoofing | — | Mitigated: `%PDF-` magic bytes checked, not the declared type |
| Admin session | Low | Single shared password, throttled 6/min, regenerated on sign-in |

The upload memory issue and in-process ingestion are the same fix: move
ingestion to the worker and stream the upload to disk rather than buffering it.

---

## Scalability

Sound for the corpus size in view (tens of municipalities, thousands of chunks).

- HNSW with `ef_search` tuning, one physical table per embedding dimension —
  correct, and the additive-rebuild collection model means a model change is not
  a migration.
- Hybrid retrieval fuses on **ranks**, not incomparable scores. Correct.
- The reranker degrades to a no-op rather than failing the query.

The real ceiling is not the database, it is single-GPU generation throughput.
One question occupies the card for seconds; that is the capacity limit, and it
is why rate limiting matters more than query optimisation.

---

## The Phase 3 design question that matters

Sections C and D — the compliance engine and permit checklist — are in tension
with the invariant the rest of this system is built on.

**The invariant:** every statement is grounded in retrieved bylaw text, quoted
verbatim, verified before display, and refused when unsupported.

**A compliance calculator breaks it** if built the obvious way. Encoding "maximum
fascia sign area = 0.2 × storefront width" as Python creates a second source of
truth. It will silently drift from the PDF the first time a municipality amends
its bylaw, and it produces a confident numeric verdict with no citation — for the
highest-liability output in the product. Someone fabricates a sign from it.

**Proposal: make it retrieval-backed rather than rule-coded.**

A municipality rule adapter should record *where a rule lives* — which section,
which document — not what it says. At question time:

1. Retrieve the governing section for the sign type and dimension.
2. Parse the numeric rule out of the retrieved text.
3. Do arithmetic on the parsed values.
4. Return the verdict **with the citation it was computed from**.
5. Refuse when no rule can be retrieved, rather than falling back to a default.

Slower and less tidy than a rules table, and it keeps the property that makes
this product defensible: no number is ever asserted that cannot be traced to a
line in a published bylaw. A drifted adapter then produces a visible failure —
"could not find the governing section" — instead of a confident wrong answer.

The same applies to permit checklists: derive from the bylaw's own permit
provisions with citations, rather than curating a list per city.

---

## Zoning integration — assessment

The adapter/provider architecture proposed is the right shape. Three cautions:

**Coverage cannot be assumed.** Vancouver, Surrey and Calgary publish open data
with real APIs. Not every municipality on the list does, and some publish only
an interactive map with no queryable endpoint. This needs verifying per
municipality before any coverage is promised in the interface — the same
discipline already applied to bylaw coverage, which is computed rather than
declared.

**Zoning is a fact that goes stale.** A parcel's zoning changes by rezoning
application. Cached lookups need `updated_at`, a TTL, and the answer must state
its as-at date. A confidently wrong zone produces a confidently wrong sign rule.

**Address → municipality must not guess.** The same invariant as the two
Langleys: an address that could be in the City or the Township must ask, not
pick. Geocoders will happily return one.

---

## Recommended sequence

Ordered by dependency, not by section letter.

| Stage | Work | Why here |
|---|---|---|
| **0** | Wire the lineage pass; fix the `in_force` deadlock | Everything downstream is untestable until answers exist |
| **1** | Real ingestion worker; move uploads off the API process | Unblocks safe delete/replace/re-index, fixes two security items |
| **2** | Admin: delete, replace, re-index, document versioning | Completes the backend brief |
| **3** | Province-agnostic ingestion; Alberta seed; configurable scope copy | Three small fixes, removes the expansion blockers |
| **4** | Rate limiting, model verification at boot, public `/documents` | Production readiness |
| **5** | Zoning: schema, `base.py`, `zoning_service.py`, two real providers | Foundation with evidence, not six speculative providers |
| **6** | Address lookup with provider architecture | Depends on zoning |
| **7** | Compliance engine, retrieval-backed | Depends on reliable retrieval — i.e. on stage 0 |
| **8** | Permit checklist, municipality comparison | Depends on 7 |
| **9** | Frontend pages for the above | Last, so it renders something real |
| **10** | Docs, CI, PHASE3.md | Throughout, finalised here |

Stages 0–4 are backend completion and are prerequisites. Stages 5–8 are Phase 3
proper. Attempting 5–8 before 0 means building on a retrieval path that returns
nothing.

---

## One process note

Milestone 1 has still not run to completion. The validation harness, thresholds
and spot-check worksheet all exist and have never produced a number.

Given the P0 finding, that is now explicable rather than merely overdue: the run
could not have passed. Fixing stage 0 and completing Milestone 1 would convert a
large body of untested code into a measured one — and every stage above assumes
retrieval works, which nothing has yet demonstrated end to end.
