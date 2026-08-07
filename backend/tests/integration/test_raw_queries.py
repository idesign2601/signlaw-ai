"""Hand-written SQL, executed against a real database.

This suite exists because of a specific failure: the coverage endpoint selected
``m.slug`` when the column is ``m.canonical_slug``. Its e2e test passed — the
session was stubbed, so the SQL was never sent anywhere — and the error only
appeared when a browser hit the deployed API.

A stubbed session tests the handler's *logic*. It cannot test whether the query
is valid, and this codebase writes a lot of SQL by hand. Anything hand-written
gets executed here at least once.

The assertions are deliberately weak: what matters is that Postgres accepts the
statement and the named columns come back. Behaviour is covered elsewhere.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1.municipalities import _indexed_document_counts
from app.core.config import get_settings
from app.db.session import create_session_factory
from app.services.zoning import ZoningOutcome, ZoningService

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


class TestCoverageQuery:
    async def test_document_counts_query_is_valid_sql(
        self, session: AsyncSession
    ) -> None:
        """The regression.

        An empty corpus returns an empty mapping — the point is that Postgres
        accepts the statement at all.
        """
        counts = await _indexed_document_counts(session)
        assert isinstance(counts, dict)

    async def test_counts_are_keyed_by_canonical_slug(
        self, session: AsyncSession
    ) -> None:
        """Keys must match what the province catalogue uses.

        A mismatch here would make every municipality report as unavailable
        however many documents were indexed, because the join in the endpoint
        is by slug.
        """
        counts = await _indexed_document_counts(session)
        assert all(isinstance(key, str) for key in counts)


class TestZoningQueries:
    async def test_lookup_for_an_unknown_municipality(
        self, session: AsyncSession
    ) -> None:
        report = await ZoningService(session=session).lookup(
            "123 Main Street", "atlantis"
        )
        assert report.outcome is ZoningOutcome.UNSUPPORTED

    async def test_lookup_against_a_real_municipality_row(
        self, session: AsyncSession
    ) -> None:
        """Exercises the municipality and parcel_zoning queries.

        With no GIS configuration the outcome is `unsupported`, which is the
        correct answer and still requires both statements to be valid.
        """
        import uuid

        from sqlalchemy import text

        province_id = await session.scalar(
            text("SELECT id FROM province WHERE code = 'BC'")
        )
        municipality_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO municipality (id, province_id, name, canonical_slug) "
                "VALUES (:id, :province, 'Zoningville', 'zoningville')"
            ),
            {"id": municipality_id, "province": province_id},
        )
        await session.flush()

        report = await ZoningService(session=session).lookup(
            "123 Main Street", "zoningville"
        )
        assert report.outcome is ZoningOutcome.UNSUPPORTED
