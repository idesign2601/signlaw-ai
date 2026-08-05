"""Migration integrity against a real Postgres instance.

The schema is the contract that keeps citations correct, so it is verified
end-to-end: migrations apply, every expected table and index exists, and the
constraints that guard currency and provenance are actually enforced by the
database rather than only by application code.

Requires Postgres. Run with `make test-integration`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.db.session import create_session_factory

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


EXPECTED_TABLES = {
    "municipality",
    "document",
    "bylaw_relation",
    "page",
    "section",
    "chunk",
    "ingestion_job",
    "chat_session",
    "chat_message",
    "answer_feedback",
}


class TestSchemaExists:
    async def test_all_tables_were_created(self, session: AsyncSession) -> None:
        result = await session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        tables = {row[0] for row in result}
        assert EXPECTED_TABLES <= tables

    async def test_enum_types_were_created(self, session: AsyncSession) -> None:
        result = await session.execute(
            text("SELECT typname FROM pg_type WHERE typtype = 'e'")
        )
        types = {row[0] for row in result}
        assert {
            "doc_type",
            "document_status",
            "relation_type",
            "metadata_source",
            "chunk_type",
            "job_status",
            "chat_role",
            "query_intent",
            "confidence_band",
            "feedback_rating",
        } <= types

    async def test_full_text_index_exists(self, session: AsyncSession) -> None:
        result = await session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunk'")
        )
        assert "ix_chunk_body_fts" in {row[0] for row in result}

    async def test_full_text_index_is_usable(self, session: AsyncSession) -> None:
        # Proves the expression is IMMUTABLE and the index is queryable.
        result = await session.execute(
            text(
                "SELECT count(*) FROM chunk "
                "WHERE to_tsvector('english'::regconfig, body) @@ plainto_tsquery('fascia sign')"
            )
        )
        assert result.scalar() is not None


class TestConstraintsAreEnforced:
    async def test_duplicate_document_hash_is_rejected(self, session: AsyncSession) -> None:
        # What makes re-running ingestion over the same folder idempotent.
        sha = uuid.uuid4().hex * 2
        for _ in range(2):
            await session.execute(
                text(
                    "INSERT INTO document (id, filename, source_path, sha256) "
                    "VALUES (:id, 'a.pdf', '/tmp/a.pdf', :sha)"
                ),
                {"id": uuid.uuid4(), "sha": sha},
            )
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_document_defaults_to_unknown_status(self, session: AsyncSession) -> None:
        # A freshly ingested document is never assumed to be in force.
        document_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO document (id, filename, source_path, sha256) "
                "VALUES (:id, 'a.pdf', '/tmp/a.pdf', :sha)"
            ),
            {"id": document_id, "sha": uuid.uuid4().hex * 2},
        )
        status = await session.scalar(
            text("SELECT status FROM document WHERE id = :id"), {"id": document_id}
        )
        assert status == "unknown"

    async def test_self_referential_bylaw_relation_is_rejected(
        self, session: AsyncSession
    ) -> None:
        document_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO document (id, filename, source_path, sha256) "
                "VALUES (:id, 'a.pdf', '/tmp/a.pdf', :sha)"
            ),
            {"id": document_id, "sha": uuid.uuid4().hex * 2},
        )
        await session.flush()

        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO bylaw_relation "
                    "(id, parent_document_id, child_document_id, relation_type, detected_by) "
                    "VALUES (:id, :doc, :doc, 'amends', 'test')"
                ),
                {"id": uuid.uuid4(), "doc": document_id},
            )
            await session.flush()

    async def test_confidence_outside_zero_to_one_is_rejected(
        self, session: AsyncSession
    ) -> None:
        session_id = uuid.uuid4()
        await session.execute(
            text("INSERT INTO chat_session (id) VALUES (:id)"), {"id": session_id}
        )
        await session.flush()

        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    "INSERT INTO chat_message (id, session_id, role, content, confidence) "
                    "VALUES (:id, :session, 'assistant', 'x', 1.5)"
                ),
                {"id": uuid.uuid4(), "session": session_id},
            )
            await session.flush()

    async def test_deleting_a_document_cascades_to_chunks(
        self, session: AsyncSession
    ) -> None:
        # Admin delete must not strand chunks that would keep being retrieved.
        document_id, chunk_id = uuid.uuid4(), uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO document (id, filename, source_path, sha256) "
                "VALUES (:id, 'a.pdf', '/tmp/a.pdf', :sha)"
            ),
            {"id": document_id, "sha": uuid.uuid4().hex * 2},
        )
        await session.execute(
            text(
                "INSERT INTO chunk (id, document_id, page_number, body, content_hash) "
                "VALUES (:cid, :did, 1, 'text', 'hash')"
            ),
            {"cid": chunk_id, "did": document_id},
        )
        await session.flush()

        await session.execute(
            text("DELETE FROM document WHERE id = :id"), {"id": document_id}
        )
        await session.flush()

        remaining = await session.scalar(
            text("SELECT count(*) FROM chunk WHERE id = :id"), {"id": chunk_id}
        )
        assert remaining == 0
