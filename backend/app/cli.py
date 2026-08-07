"""SignLaw AI command line.

Runs the complete pipeline with no web server, so a developer can put a PDF on
disk and ask a question about it::

    signlaw health
    signlaw ingest documents/bylaws/burnaby_sign_bylaw.pdf
    signlaw ask "What is the maximum fascia sign area?"

Everything runs locally: Postgres with pgvector, an embedding model from the
mounted models volume, and Ollama for generation. No external API is contacted.

argparse rather than click or typer — the CLI is a development tool and does not
justify a dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging

if TYPE_CHECKING:
    from app.services.rag_service import AnswerResult
    from app.validation.harness import IngestionMetrics

__all__ = ["main"]

# ANSI codes, suppressed when stdout is redirected.
_COLOUR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def BOLD(text: str) -> str:  # noqa: N802 — these read as constants at call sites
    return _c("1", text)


def DIM(text: str) -> str:  # noqa: N802
    return _c("2", text)


def GREEN(text: str) -> str:  # noqa: N802
    return _c("32", text)


def YELLOW(text: str) -> str:  # noqa: N802
    return _c("33", text)


def RED(text: str) -> str:  # noqa: N802
    return _c("31", text)


def CYAN(text: str) -> str:  # noqa: N802
    return _c("36", text)


# -----------------------------------------------------------------------------
# Wiring
# -----------------------------------------------------------------------------


def _build_settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.observability)
    return settings


def _models_cache(settings: Settings) -> str:
    """Where model weights live. Never inside a Docker image."""
    return str(settings.ingestion.tessdata_dir.parent / "huggingface")


async def _with_session[T](
    settings: Settings,
    task: Callable[[AsyncSession, AsyncEngine], Awaitable[T]],
) -> T:
    """Run a task with a database session, disposing the engine after.

    ``task`` receives the request-scoped session and the engine. The engine is
    passed as well because the health checks probe the connection directly
    rather than going through a session.
    """
    from app.db.session import create_engine, create_session_factory, dispose_engine

    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            return await task(session, engine)
    finally:
        await dispose_engine(engine)


# -----------------------------------------------------------------------------
# health
# -----------------------------------------------------------------------------


async def _run_health(settings: Settings, as_json: bool) -> int:
    from app.adapters.embeddings import build_embedding_provider
    from app.adapters.llm import build_llm_provider
    from app.adapters.reranker import build_reranker
    from app.services.health_service import HealthReport, HealthService, render_report

    cache = _models_cache(settings)
    embedder = build_embedding_provider(settings.embedding, cache_dir=cache)
    llm = build_llm_provider(settings.llm)
    reranker = build_reranker(settings.retrieval, settings.embedding, cache_dir=cache)

    async def run(session: AsyncSession, engine: AsyncEngine) -> HealthReport:
        service = HealthService(
            settings=settings,
            engine=engine,
            embedder=embedder,
            llm=llm,
            reranker=reranker,
        )
        return await service.check()

    report = await _with_session(settings, run)

    if as_json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render_report(report, colour=_COLOUR))

    return 0 if report.can_answer else 1


# -----------------------------------------------------------------------------
# ingest
# -----------------------------------------------------------------------------


async def _run_ingest(settings: Settings, paths: Sequence[str], force: bool, as_json: bool) -> int:
    from app.adapters.embeddings import build_embedding_provider
    from app.ingestion.pipeline import discover_pdfs
    from app.services.ingestion_service import IngestionService, IngestResult

    resolved: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            resolved.extend(
                discover_pdfs(path, max_size_bytes=settings.ingestion.max_file_size_bytes)
            )
        elif path.is_file():
            resolved.append(path)
        else:
            print(RED(f"not found: {path}"), file=sys.stderr)
            return 2

    if not resolved:
        print(YELLOW("No PDFs found."), file=sys.stderr)
        return 2

    embedder = build_embedding_provider(settings.embedding, cache_dir=_models_cache(settings))

    print(f"{BOLD('Ingesting')} {len(resolved)} document(s)")
    print(DIM(f"  embedding model: {settings.embedding.model}"))
    print()

    async def run(session: AsyncSession, engine: AsyncEngine) -> IngestResult:
        service = IngestionService(session=session, settings=settings, embedder=embedder)
        return await service.ingest_paths(resolved, force=force)

    result = await _with_session(settings, run)

    if as_json:
        print(
            json.dumps(
                {
                    "processed": result.processed,
                    "skipped": result.skipped,
                    "failed": [{"file": name, "error": error} for name, error in result.failed],
                    "chunks": result.chunks_written,
                    "collection": result.collection_name,
                    "documents_resolved": result.documents_resolved,
                    "in_force": result.in_force,
                },
                indent=2,
            )
        )
        return 0 if result.succeeded else 1

    for name in result.processed:
        print(f"  {GREEN('indexed')}  {name}")
    for name in result.skipped:
        print(f"  {DIM('skipped')}  {name} {DIM('(unchanged)')}")
    for name, error in result.failed:
        print(f"  {RED('failed ')}  {name}: {error}")

    print()
    print(f"{result.chunks_written} chunks embedded into {CYAN(result.collection_name)}")
    # Reported because it is the number that decides whether anything can be
    # retrieved at all. Chunk counts look healthy even when nothing is in force.
    print(f"{result.in_force} of {result.documents_resolved} document(s) in force")

    if result.documents_resolved and not result.in_force:
        print()
        print(
            RED("Nothing is in force, so no question can be answered.")
            + " Every document resolved to superseded, repealed or unknown —"
        )
        print(
            DIM(
                "  usually because the municipality or bylaw number was not "
                "detected. Check the filenames carry the city."
            )
        )
    elif result.processed:
        print(DIM('Try: signlaw ask "What is the maximum fascia sign area?"'))

    return 0 if result.succeeded else 1


# -----------------------------------------------------------------------------
# ask
# -----------------------------------------------------------------------------


async def _run_ask(
    settings: Settings,
    question: str,
    city: str | None,
    show_trace: bool,
    as_json: bool,
) -> int:
    from app.adapters.embeddings import build_embedding_provider
    from app.adapters.llm import build_llm_provider
    from app.adapters.reranker import build_reranker
    from app.rag.retriever import HybridRetriever, RetrievalFilters
    from app.rag.synthesizer import AnswerSynthesizer
    from app.services.rag_service import RagService

    cache = _models_cache(settings)
    embedder = build_embedding_provider(settings.embedding, cache_dir=cache)
    llm = build_llm_provider(settings.llm)
    reranker = build_reranker(settings.retrieval, settings.embedding, cache_dir=cache)

    async def run(session: AsyncSession, engine: AsyncEngine) -> AnswerResult:
        retriever = HybridRetriever(
            session=session,
            embedder=embedder,
            settings=settings.retrieval,
            vector_settings=settings.vector,
            reranker=reranker,
        )
        service = RagService(
            retriever=retriever,
            synthesizer=AnswerSynthesizer(llm=llm),
        )
        filters = RetrievalFilters(municipality_slugs=(city,), in_force_only=True) if city else None
        return await service.answer(question, filters=filters)

    result = await _with_session(settings, run)

    if as_json:
        print(json.dumps(_answer_as_dict(result), indent=2, default=str))
        return 0 if result.answered else 1

    _print_answer(result, show_trace=show_trace)
    return 0 if result.answered else 1


def _answer_as_dict(result: AnswerResult) -> dict[str, object]:
    return {
        "outcome": result.outcome.value,
        "answer": result.answer,
        "confidence": (result.confidence.as_dict() if result.confidence else None),
        "citations": [
            {
                "municipality": c.municipality,
                "bylaw": c.bylaw_title,
                "bylaw_number": c.bylaw_number,
                "section": c.section,
                "page": c.page,
                "amendment_status": c.amendment_status,
                "quote": c.quote,
            }
            for c in result.citations
        ],
        "clarification_options": list(result.clarification_options),
        "trace": result.trace.as_dict() if result.trace else None,
    }


def _print_answer(result: AnswerResult, *, show_trace: bool) -> None:
    from app.db.enums import ConfidenceBand
    from app.services.rag_service import AnswerOutcome

    print()
    print(BOLD("Answer"))
    print("-" * 60)
    print(result.answer)
    print()

    if result.clarification_options:
        print(BOLD("Did you mean"))
        for option in result.clarification_options:
            print(f"  - {option}")
        print()

    if result.conditions:
        print(BOLD("Depends on"))
        for condition in result.conditions:
            print(f"  - {condition}")
        print()

    if result.citations:
        print(BOLD("Sources"))
        print("-" * 60)
        for index, citation in enumerate(result.citations, start=1):
            status = citation.amendment_status
            marker = GREEN("in force") if status == "in_force" else RED(status)
            print(f"  [{index}] {citation.municipality or 'Unknown municipality'}")
            print(f"      bylaw    {citation.bylaw_title or 'Untitled'}")
            print(f"      section  {citation.section or DIM('not identified')}")
            print(f"      page     {citation.page}")
            print(f"      status   {marker}")
            if citation.from_ocr:
                print(f"      {YELLOW('note')}     text recovered by OCR")
            print(f"      {DIM(_truncate(citation.quote, 200))}")
            print()

    if result.confidence:
        band = result.confidence.band
        colour = {
            ConfidenceBand.HIGH: GREEN,
            ConfidenceBand.MEDIUM: YELLOW,
            ConfidenceBand.LOW: RED,
            ConfidenceBand.INSUFFICIENT: RED,
        }[band]
        print(BOLD("Confidence"))
        print("-" * 60)
        print(f"  {colour(band.value.upper())}  ({result.confidence.score:.2f})")
        print(f"  {result.confidence.explanation}")
        for warning in result.confidence.warnings:
            print(f"  {YELLOW('!')} {warning}")
        print()

    if show_trace and result.trace:
        trace = result.trace
        print(BOLD("Retrieval"))
        print("-" * 60)
        print(f"  collection    {trace.collection or DIM('none')}")
        print(f"  intent        {trace.intent}")
        print(
            f"  candidates    dense {trace.dense_candidates}, "
            f"sparse {trace.sparse_candidates}, fused {trace.fused_candidates}"
        )
        print(f"  reranked      {trace.reranked}")
        print(f"  model         {trace.model_used or DIM('none')}")
        print(f"  prompt        {trace.prompt_version}")
        print(
            f"  timing        retrieval {trace.retrieval_ms}ms, "
            f"generation {trace.generation_ms}ms, total {trace.total_ms}ms"
        )
        print()
        for score in trace.retrieval_scores[:10]:
            print(
                f"    {DIM(str(score.get('section') or '-')):<14} "
                f"p{score.get('page')} "
                f"fused={score.get('fused_score')} "
                f"rerank={score.get('rerank_score')}"
            )
        print()

    if result.outcome is not AnswerOutcome.ANSWERED:
        print(YELLOW(f"outcome: {result.outcome.value}"))
        print()

    print(DIM("Informational only — not legal advice. Verify with the municipality."))


def _truncate(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


# -----------------------------------------------------------------------------
# eval
# -----------------------------------------------------------------------------


async def _run_eval(settings: Settings, all_cases: bool, as_json: bool, output: str | None) -> int:
    from app.adapters.embeddings import build_embedding_provider
    from app.adapters.llm import build_llm_provider
    from app.adapters.reranker import build_reranker
    from app.eval.dataset import seed_suite
    from app.eval.metrics import SuiteReport
    from app.eval.runner import EvalRunner
    from app.rag.retriever import HybridRetriever
    from app.rag.synthesizer import AnswerSynthesizer
    from app.services.rag_service import RagService

    suite = seed_suite()
    if not all_cases:
        suite = suite.verified_only()

    if not len(suite):
        print(
            YELLOW(
                "No verified cases. Golden-set cases need a human to open the "
                "real bylaw, record the section and page, and set verified_by. "
                "Run with --all to exercise unverified cases without scoring them."
            )
        )
        return 1

    cache = _models_cache(settings)
    embedder = build_embedding_provider(settings.embedding, cache_dir=cache)
    llm = build_llm_provider(settings.llm)
    reranker = build_reranker(settings.retrieval, settings.embedding, cache_dir=cache)

    async def run(session: AsyncSession, engine: AsyncEngine) -> SuiteReport:
        retriever = HybridRetriever(
            session=session,
            embedder=embedder,
            settings=settings.retrieval,
            vector_settings=settings.vector,
            reranker=reranker,
        )
        service = RagService(retriever=retriever, synthesizer=AnswerSynthesizer(llm=llm))
        return await EvalRunner(service=service).run(suite)

    report = await _with_session(settings, run)

    if as_json:
        payload = json.dumps(report.as_dict(), indent=2)
        print(payload)
    else:
        print(report.render())

    if output:
        Path(output).write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        print(DIM(f"\nWritten to {output}"))

    # Any stale citation fails the run outright.
    return 0 if report.staleness_failures == 0 and report.pass_rate >= 0.8 else 1


# -----------------------------------------------------------------------------
# validate
# -----------------------------------------------------------------------------


async def _run_validate(
    settings: Settings, output: str | None, worksheet: str | None, as_json: bool
) -> int:
    from app.adapters.embeddings import build_embedding_provider
    from app.adapters.llm import build_llm_provider
    from app.adapters.reranker import build_reranker
    from app.rag.retriever import HybridRetriever
    from app.rag.synthesizer import AnswerSynthesizer
    from app.services.rag_service import RagService
    from app.validation import ValidationHarness, milestone_passed, render_report
    from app.validation.harness import ValidationReport

    cache = _models_cache(settings)
    embedder = build_embedding_provider(settings.embedding, cache_dir=cache)
    llm = build_llm_provider(settings.llm)
    reranker = build_reranker(settings.retrieval, settings.embedding, cache_dir=cache)

    async def run(session: AsyncSession, engine: AsyncEngine) -> ValidationReport:
        corpus = await _corpus_metrics(session)
        collection = await _active_collection_name(session)

        service = RagService(
            retriever=HybridRetriever(
                session=session,
                embedder=embedder,
                settings=settings.retrieval,
                vector_settings=settings.vector,
                reranker=reranker,
            ),
            synthesizer=AnswerSynthesizer(llm=llm),
        )
        return await ValidationHarness(service=service).run(
            corpus=corpus,
            embedding_model=settings.embedding.model,
            llm_model=settings.llm.model,
            collection=collection,
        )

    report = await _with_session(settings, run)

    if as_json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(render_report(report))

    if output:
        Path(output).write_text(
            json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8"
        )
    if worksheet:
        Path(worksheet).write_text(report.spot_check_worksheet(), encoding="utf-8")

    return 0 if milestone_passed(report) else 1


async def _corpus_metrics(session: AsyncSession) -> list[IngestionMetrics]:
    """Read what ingestion actually produced, from the database.

    Measured after the fact rather than captured during ingest, so the numbers
    describe the corpus being queried rather than one particular run.
    """
    from sqlalchemy import text

    from app.validation.harness import IngestionMetrics

    rows = await session.execute(
        text(
            "SELECT d.filename, d.page_count, d.metadata_confidence, "
            "       d.bylaw_number, m.canonical_slug AS municipality, "
            "       (SELECT count(*) FROM section s WHERE s.document_id = d.id) "
            "         AS sections, "
            "       (SELECT count(*) FROM chunk c WHERE c.document_id = d.id) "
            "         AS chunks, "
            "       (SELECT count(*) FROM document_table t "
            "        WHERE t.document_id = d.id) AS tables, "
            "       (SELECT count(*) FROM page p WHERE p.document_id = d.id "
            "        AND p.was_ocred) AS ocr_pages, "
            "       d.processing_stage, d.ingestion_error "
            "FROM document d "
            "LEFT JOIN municipality m ON m.id = d.municipality_id "
            "ORDER BY d.filename"
        )
    )

    return [
        IngestionMetrics(
            filename=row.filename,
            succeeded=row.processing_stage == "indexed",
            pages=row.page_count or 0,
            chunks=row.chunks or 0,
            sections=row.sections or 0,
            tables=row.tables or 0,
            ocr_pages=row.ocr_pages or 0,
            municipality=row.municipality,
            bylaw_number=row.bylaw_number,
            metadata_confidence=float(row.metadata_confidence or 0.0),
            error=row.ingestion_error,
        )
        for row in rows
    ]


async def _active_collection_name(session: AsyncSession) -> str:
    from sqlalchemy import text

    name = await session.scalar(
        text("SELECT name FROM embedding_collection WHERE status = 'active' LIMIT 1")
    )
    return str(name or "")


# -----------------------------------------------------------------------------
# entry point
# -----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signlaw",
        description=(
            "Query British Columbia municipal sign bylaws locally. "
            "Requires Postgres with pgvector and Ollama; no external APIs."
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser(
        "health", help="check Postgres, pgvector, embedding model and Ollama"
    )
    health.set_defaults(handler="health")

    ingest = subparsers.add_parser("ingest", help="index one or more PDFs")
    ingest.add_argument("paths", nargs="+", help="PDF files or directories")
    ingest.add_argument("--force", action="store_true", help="re-index documents already indexed")
    ingest.set_defaults(handler="ingest")

    ask = subparsers.add_parser("ask", help="ask a question about indexed bylaws")
    ask.add_argument("question", help="the question, in quotes")
    ask.add_argument("--city", help="restrict to a municipality slug, e.g. burnaby")
    ask.add_argument("--trace", action="store_true", help="show retrieval scores and timings")
    ask.set_defaults(handler="ask")

    evaluate = subparsers.add_parser("eval", help="run the golden evaluation suite")
    evaluate.add_argument(
        "--all",
        action="store_true",
        dest="all_cases",
        help="include unverified cases (exercises the pipeline; does not score)",
    )
    evaluate.add_argument("--output", help="write the report to a JSON file")
    evaluate.set_defaults(handler="eval")

    validate = subparsers.add_parser(
        "validate", help="run the Milestone 1 production validation suite"
    )
    validate.add_argument("--output", help="write the report to a JSON file")
    validate.add_argument(
        "--worksheet", help="write the human spot-check worksheet to a Markdown file"
    )
    validate.set_defaults(handler="validate")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    try:
        settings = _build_settings()
    except Exception as exc:  # configuration errors must read clearly
        print(RED(f"Configuration error:\n{exc}"), file=sys.stderr)
        return 2

    handlers = {
        "health": lambda: _run_health(settings, args.json),
        "ingest": lambda: _run_ingest(settings, args.paths, args.force, args.json),
        "ask": lambda: _run_ask(settings, args.question, args.city, args.trace, args.json),
        "eval": lambda: _run_eval(settings, args.all_cases, args.json, args.output),
        "validate": lambda: _run_validate(settings, args.output, args.worksheet, args.json),
    }

    try:
        return asyncio.run(handlers[args.handler]())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # the CLI is the last line of defence
        print(RED(f"Error: {exc}"), file=sys.stderr)
        if settings.debug:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
