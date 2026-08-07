"""Lineage resolution against a real database.

Regression cover for the defect that made the entire corpus unretrievable:
``LineageResolver`` existed, was correct, and was never called. Every document
stayed ``unknown``, retrieval filtered on ``status = 'in_force'``, and every
question answered "found only superseded or repealed text".

These tests assert the wiring, not the resolver's rules — those are unit-tested
separately. The property under test is that a document which has been ingested
ends up *retrievable*.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.db.session import create_session_factory
from app.services.ingestion_service import IngestionService

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine():
    settings = get_settings()
    engine = create_async_engine(settings.db.async_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()


class _StubEmbedder:
    """The lineage pass never embeds; this only satisfies the constructor."""

    model = "stub"
    model_revision = "stub"


async def _municipality(session: AsyncSession, slug: str) -> uuid.UUID:
    province_id = await session.scalar(text("SELECT id FROM province WHERE code = 'BC'"))
    municipality_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO municipality (id, province_id, name, canonical_slug) "
            "VALUES (:id, :province, :name, :slug)"
        ),
        {"id": municipality_id, "province": province_id, "name": slug, "slug": slug},
    )
    return municipality_id


async def _document(
    session: AsyncSession,
    municipality_id: uuid.UUID,
    *,
    bylaw_number: str,
    consolidation: date | None = None,
    year: int | None = None,
    doc_type: str = "consolidated",
) -> uuid.UUID:
    document_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO document (id, municipality_id, filename, source_path, "
            " sha256, bylaw_number, year, consolidation_date, doc_type, status) "
            "VALUES (:id, :municipality_id, :filename, :path, :sha, :number, :year, "
            " :consolidation, CAST(:doc_type AS doc_type), 'unknown')"
        ),
        {
            "id": document_id,
            "municipality_id": municipality_id,
            "filename": f"{bylaw_number}.pdf",
            # Never opened — source_path only has to be unique for these tests.
            "path": f"corpus/{document_id}.pdf",
            "sha": uuid.uuid4().hex * 2,
            "number": bylaw_number,
            "year": year,
            "consolidation": consolidation,
            "doc_type": doc_type,
        },
    )
    return document_id


def _service(session: AsyncSession) -> IngestionService:
    return IngestionService(
        session=session,
        settings=get_settings(),
        embedder=_StubEmbedder(),  # type: ignore[arg-type]
    )


class TestLineageIsActuallyRun:
    async def test_a_lone_document_becomes_retrievable(self, session: AsyncSession) -> None:
        """The regression.

        One document, no competing versions: it is the law, and must end up
        `in_force` or nothing can ever be retrieved.
        """
        municipality_id = await _municipality(session, "testville")
        document_id = await _document(session, municipality_id, bylaw_number="9001", year=2020)
        await session.flush()

        await _service(session).resolve_lineage()

        status = await session.scalar(
            text("SELECT status FROM document WHERE id = :id"), {"id": document_id}
        )
        assert status == "in_force"

    async def test_older_version_is_superseded(self, session: AsyncSession) -> None:
        municipality_id = await _municipality(session, "testburg")
        older = await _document(
            session,
            municipality_id,
            bylaw_number="9002",
            consolidation=date(2015, 1, 1),
        )
        newer = await _document(
            session,
            municipality_id,
            bylaw_number="9002",
            consolidation=date(2023, 1, 1),
        )
        await session.flush()

        await _service(session).resolve_lineage()

        rows = await session.execute(
            text("SELECT id, status FROM document WHERE id = ANY(:ids)"),
            {"ids": [older, newer]},
        )
        statuses = {str(row.id): row.status for row in rows}
        assert statuses[str(newer)] == "in_force"
        assert statuses[str(older)] == "superseded"

    async def test_undetectable_municipality_stays_unknown(self, session: AsyncSession) -> None:
        """Never assumed current.

        Without a municipality the document cannot be placed against its
        siblings, and guessing would be the failure this system exists to
        prevent.
        """
        document_id = await _document(
            session,
            None,
            bylaw_number="9003",
            year=2020,  # type: ignore[arg-type]
        )
        await session.flush()

        await _service(session).resolve_lineage()

        status = await session.scalar(
            text("SELECT status FROM document WHERE id = :id"), {"id": document_id}
        )
        assert status == "unknown"

    async def test_human_verification_is_not_overwritten(self, session: AsyncSession) -> None:
        """An operator who checked the register outranks the resolver."""
        municipality_id = await _municipality(session, "testopolis")
        document_id = await _document(session, municipality_id, bylaw_number="9004", year=2020)
        await session.execute(
            text(
                "UPDATE document SET verified_by_human = true, status = 'repealed' WHERE id = :id"
            ),
            {"id": document_id},
        )
        await session.flush()

        await _service(session).resolve_lineage()

        status = await session.scalar(
            text("SELECT status FROM document WHERE id = :id"), {"id": document_id}
        )
        assert status == "repealed"


class TestLineageEdges:
    async def test_version_ordering_creates_a_relation(self, session: AsyncSession) -> None:
        """bylaw_relation was empty in every run before this wiring existed."""
        municipality_id = await _municipality(session, "edgeville")
        await _document(
            session,
            municipality_id,
            bylaw_number="9005",
            consolidation=date(2015, 1, 1),
        )
        await _document(
            session,
            municipality_id,
            bylaw_number="9005",
            consolidation=date(2023, 1, 1),
        )
        await session.flush()

        await _service(session).resolve_lineage()

        edges = await session.scalar(
            text("SELECT count(*) FROM bylaw_relation WHERE detected_by = 'version_ordering'")
        )
        assert edges >= 1
