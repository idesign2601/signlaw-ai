"""Golden-set regression.

Runs the real pipeline against a built index — Postgres with pgvector, the local
embedding model and Ollama. Excluded from the default suite because it needs all
three running and a corpus ingested::

    signlaw ingest documents/bylaws/
    pytest tests/eval -m eval

The thresholds below are the release gate. Staleness is zero-tolerance: one
citation to repealed text is a defect, not a rounding error.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.eval.dataset import seed_suite
from app.eval.runner import EvalRunner

pytestmark = [pytest.mark.eval, pytest.mark.slow]

# Release gate.
MIN_PASS_RATE = 0.80
MIN_RETRIEVAL_ACCURACY = 0.85
MIN_CITATION_ACCURACY = 0.80
MIN_BEHAVIOUR_ACCURACY = 0.95


@pytest.fixture(scope="module")
def suite():
    """Verified cases only.

    A case without ``verified_by`` has expectations nobody has checked against
    the real bylaw. Scoring against those would produce a number that measures
    the guesses rather than the system.
    """
    verified = seed_suite().verified_only()
    if not len(verified):
        pytest.skip(
            "No verified golden cases. Open the real bylaws, record the section "
            "and page for each case in app/eval/dataset.py, and set verified_by."
        )
    return verified


@pytest.fixture
async def service():
    """The real pipeline, wired as production wires it."""
    from app.adapters.embeddings import build_embedding_provider
    from app.adapters.llm import build_llm_provider
    from app.adapters.reranker import build_reranker
    from app.db.session import create_engine, create_session_factory, dispose_engine
    from app.rag.retriever import HybridRetriever
    from app.rag.synthesizer import AnswerSynthesizer
    from app.services.rag_service import RagService

    settings = get_settings()
    cache = str(settings.ingestion.tessdata_dir.parent / "huggingface")

    engine = create_engine(settings)
    factory = create_session_factory(engine)

    try:
        async with factory() as session:
            yield RagService(
                retriever=HybridRetriever(
                    session=session,
                    embedder=build_embedding_provider(settings.embedding, cache_dir=cache),
                    settings=settings.retrieval,
                    vector_settings=settings.vector,
                    reranker=build_reranker(
                        settings.retrieval, settings.embedding, cache_dir=cache
                    ),
                ),
                synthesizer=AnswerSynthesizer(llm=build_llm_provider(settings.llm)),
            )
    finally:
        await dispose_engine(engine)


@pytest.fixture(scope="module")
async def report(service, suite):
    """Run the suite once and share the result across assertions."""
    return await EvalRunner(service=service).run(suite)


class TestReleaseGate:
    def test_no_citation_rests_on_outdated_text(self, report) -> None:
        # Zero tolerance. A confident citation to a repealed clause is the worst
        # output this system can produce.
        assert report.staleness_failures == 0, (
            f"{report.staleness_failures} case(s) cited superseded or repealed "
            f"bylaws:\n{report.render()}"
        )

    def test_pass_rate(self, report) -> None:
        assert report.pass_rate >= MIN_PASS_RATE, report.render()

    def test_retrieval_accuracy(self, report) -> None:
        # Bounds everything downstream: an answer cannot cite what retrieval
        # never surfaced.
        assert report.retrieval_accuracy >= MIN_RETRIEVAL_ACCURACY, report.render()

    def test_citation_accuracy(self, report) -> None:
        assert report.citation_accuracy >= MIN_CITATION_ACCURACY, report.render()

    def test_behaviour_accuracy(self, report) -> None:
        # Declining when it should, asking when it should.
        assert report.behaviour_accuracy >= MIN_BEHAVIOUR_ACCURACY, report.render()

    def test_no_errors(self, report) -> None:
        assert report.errors == 0, report.render()


class TestCalibration:
    def test_high_confidence_outperforms_low(self, report) -> None:
        """Confidence must predict correctness.

        If high-confidence answers do not pass materially more often than
        low-confidence ones, the score is decorative and is actively misleading
        the people relying on it.
        """
        calibration = report.confidence_calibration()
        high = calibration.get("high")
        low = calibration.get("low")

        if not high or not low or high["count"] < 3 or low["count"] < 3:
            pytest.skip("not enough cases in both bands to assess calibration")

        assert high["pass_rate"] > low["pass_rate"], (
            f"confidence is not predictive: high={high['pass_rate']:.0%} low={low['pass_rate']:.0%}"
        )


class TestBehaviour:
    async def test_ambiguous_municipality_asks(self, service) -> None:
        from app.services.rag_service import AnswerOutcome

        result = await service.answer("What are the sign rules in Langley?")
        assert result.outcome is AnswerOutcome.NEEDS_CLARIFICATION

    async def test_out_of_scope_declines(self, service) -> None:
        from app.services.rag_service import AnswerOutcome

        result = await service.answer("What is the population of Surrey?")
        assert result.outcome is AnswerOutcome.OUT_OF_SCOPE

    async def test_unknown_sign_type_abstains(self, service) -> None:
        # Must not reason by analogy from illuminated signs.
        result = await service.answer(
            "What are the rules for holographic projection signs in Surrey?"
        )
        assert not result.answered or not result.citations
