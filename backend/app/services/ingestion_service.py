"""Ingestion persistence and embedding.

Bridges the Phase 2 pipeline — which turns a PDF into sections, tables and
chunks in memory — to Postgres and pgvector.

Two properties this layer is responsible for:

* **Idempotence.** A document is identified by the SHA-256 of its bytes. Running
  ingest over the same folder twice does nothing the second time, so a resumed
  run costs only the documents that actually changed.
* **Atomicity per document.** Everything a document produces is written in one
  transaction. A crash mid-write leaves no half-indexed document whose chunks
  are retrievable but whose sections are missing — which would produce
  citations pointing at nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.embeddings.base import EmbeddingProviderProtocol
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.enums import CollectionStatus, DocType, DocumentStatus, ProcessingStage
from app.domain.models import TextChunk
from app.domain.municipalities import MunicipalityRegistry
from app.domain.provinces import find_municipality
from app.ingestion.amendments import (
    DocumentFacts,
    LineageResolver,
    RelationEdge,
    ResolvedDocument,
)
from app.ingestion.pipeline import DocumentOutcome, DocumentPipeline, PipelineConfig
from app.rag.collections import CollectionSpec

__all__ = ["IngestResult", "IngestionService"]

logger = get_logger(__name__)


@dataclass
class IngestResult:
    """What one ingest run produced."""

    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    chunks_written: int = 0
    chunks_embedded: int = 0
    collection_name: str = ""
    # Documents whose in-force status was resolved. Zero here means nothing is
    # retrievable, whatever the chunk counts say.
    documents_resolved: int = 0
    in_force: int = 0

    @property
    def total(self) -> int:
        return len(self.processed) + len(self.skipped) + len(self.failed)

    @property
    def succeeded(self) -> bool:
        return not self.failed


@dataclass
class IngestionService:
    """Runs documents through the pipeline and persists the result."""

    session: AsyncSession
    settings: Settings
    embedder: EmbeddingProviderProtocol
    registry: MunicipalityRegistry = field(default_factory=MunicipalityRegistry)

    def __post_init__(self) -> None:
        ingestion = self.settings.ingestion
        self.pipeline = DocumentPipeline(
            PipelineConfig(
                scan_detection_min_chars=ingestion.scan_detection_min_chars,
                ocr_enabled=ingestion.ocr_enabled,
                ocr_languages=ingestion.ocr_languages,
                ocr_dpi=ingestion.ocr_dpi,
                ocr_timeout_s=ingestion.ocr_timeout_s,
                tessdata_dir=ingestion.tessdata_dir,
            ),
            registry=self.registry,
        )

    # -- public API ----------------------------------------------------------

    async def ingest_paths(self, paths: Sequence[Path], *, force: bool = False) -> IngestResult:
        """Ingest one or more PDFs and make them retrievable."""
        collection_id, spec = await self._ensure_collection()
        result = IngestResult(collection_name=spec.name)

        for path in paths:
            try:
                written = await self._ingest_one(path, collection_id, spec, force=force)
            except Exception as exc:  # one bad PDF must not stop the run
                await self.session.rollback()
                logger.exception("ingest_failed", filename=path.name)
                result.failed.append((path.name, str(exc)))
                continue

            if written is None:
                result.skipped.append(path.name)
                continue

            result.processed.append(path.name)
            result.chunks_written += written
            result.chunks_embedded += written

        await self._refresh_collection_count(collection_id)

        # Currency is a property of the corpus, so this runs over every
        # document, not only the ones just ingested: adding one consolidation
        # can supersede documents that were already indexed.
        #
        # Without this step every document stays UNKNOWN, and retrieval — which
        # filters on `status = 'in_force'` — returns nothing at all. The system
        # then reports "found only superseded or repealed text" for every
        # question, which is plausible enough to be mistaken for a corpus
        # problem rather than a missing pass.
        resolved = await self.resolve_lineage()
        result.documents_resolved = len(resolved)
        result.in_force = sum(
            1 for document in resolved if document.status is DocumentStatus.IN_FORCE
        )

        if resolved and not result.in_force:
            # Chunks exist, embeddings exist, and not one of them can be
            # retrieved. Worth shouting about: the failure is otherwise silent
            # and reads downstream as "the bylaw does not address this".
            logger.error(
                "no_documents_in_force",
                documents=len(resolved),
                detail=(
                    "Every document resolved to superseded, repealed or unknown, "
                    "so retrieval will return nothing. Check that bylaw numbers "
                    "and municipalities were detected."
                ),
            )

        return result

    # -- lineage -------------------------------------------------------------

    async def resolve_lineage(self) -> list[ResolvedDocument]:
        """Decide which documents are currently the law, corpus-wide.

        Answering this per document is impossible: "in force" is a claim about
        one document's relationship to every other. So the whole corpus is
        loaded, resolved together, and written back.
        """
        facts = await self._load_facts()
        if not facts:
            return []

        resolver = LineageResolver(facts=facts)
        resolved = resolver.resolve()

        await self._write_statuses(resolved)
        await self._write_edges(resolver.build_edges())
        await self.session.commit()

        return resolved

    async def _load_facts(self) -> list[DocumentFacts]:
        result = await self.session.execute(
            text(
                "SELECT d.id, d.bylaw_number, d.doc_type, d.consolidation_date, "
                "       d.effective_date, d.year, d.amends_bylaw_numbers, "
                "       m.canonical_slug "
                "FROM document d "
                "LEFT JOIN municipality m ON m.id = d.municipality_id"
            )
        )

        return [
            DocumentFacts(
                document_id=str(row.id),
                municipality_slug=row.canonical_slug,
                bylaw_number=row.bylaw_number,
                doc_type=DocType(row.doc_type),
                consolidation_date=row.consolidation_date,
                effective_date=row.effective_date,
                year=row.year,
                amends_bylaw_numbers=tuple(row.amends_bylaw_numbers or ()),
            )
            for row in result
        ]

    async def _write_statuses(self, resolved: Sequence[ResolvedDocument]) -> None:
        """Write each verdict back, leaving human corrections alone.

        ``verified_by_human`` exists precisely so an operator who has checked a
        document against the municipality's register is not overruled by a
        later automated pass working from thinner evidence.
        """
        for document in resolved:
            await self.session.execute(
                text(
                    "UPDATE document SET "
                    " status = CAST(:status AS document_status), "
                    " last_amendment_date = COALESCE(:amended, last_amendment_date), "
                    " notes = :reason "
                    "WHERE id = CAST(:id AS uuid) AND verified_by_human = false"
                ),
                {
                    "id": document.document_id,
                    "status": document.status.value,
                    "amended": document.last_amendment_date,
                    "reason": document.reason,
                },
            )

            if document.needs_review:
                logger.warning(
                    "lineage_needs_review",
                    document_id=document.document_id,
                    reason=document.reason,
                )

    async def _write_edges(self, edges: Sequence[RelationEdge]) -> None:
        """Replace the detected lineage graph.

        Deleted and rebuilt rather than upserted: an edge that a previous run
        inferred may be *wrong* once more documents exist, and leaving stale
        edges behind would make the graph grow monotonically regardless of
        evidence.
        """
        await self.session.execute(text("DELETE FROM bylaw_relation"))

        for edge in edges:
            await self.session.execute(
                text(
                    "INSERT INTO bylaw_relation (id, parent_document_id, "
                    " child_document_id, relation_type, detected_by, confidence, "
                    " evidence) "
                    "VALUES (:id, CAST(:parent AS uuid), CAST(:child AS uuid), "
                    " CAST(:relation AS relation_type), :detected_by, :confidence, "
                    " :evidence) "
                    "ON CONFLICT ON CONSTRAINT uq_bylaw_relation_edge DO NOTHING"
                ),
                {
                    "id": uuid.uuid4(),
                    "parent": edge.parent_document_id,
                    "child": edge.child_document_id,
                    "relation": edge.relation_type.value,
                    "detected_by": edge.detected_by,
                    "confidence": edge.confidence,
                    "evidence": edge.evidence,
                },
            )

    # -- one document --------------------------------------------------------

    async def _ingest_one(
        self,
        path: Path,
        collection_id: uuid.UUID,
        spec: CollectionSpec,
        *,
        force: bool,
    ) -> int | None:
        """Ingest a single PDF. Returns chunks written, or ``None`` if skipped."""
        outcome = self.pipeline.process(path)

        if not outcome.succeeded:
            reason = outcome.error.message if outcome.error else "unknown"
            raise RuntimeError(f"pipeline failed at {outcome.failed_stage}: {reason}")

        existing = await self.session.scalar(
            text("SELECT id FROM document WHERE sha256 = :sha"),
            {"sha": outcome.sha256},
        )
        if existing is not None and not force:
            logger.info("document_unchanged", filename=path.name)
            return None

        if existing is not None:
            # Re-ingest: cascades remove pages, sections, chunks and embeddings.
            await self.session.execute(
                text("DELETE FROM document WHERE id = :id"), {"id": existing}
            )

        document_id = await self._write_document(path, outcome)
        section_ids = await self._write_sections(document_id, outcome)
        await self._write_pages(document_id, outcome)
        await self._write_tables(document_id, outcome, section_ids)
        chunk_ids = await self._write_chunks(document_id, outcome, section_ids)

        embedded = await self._embed_chunks(collection_id, spec, outcome.chunks, chunk_ids)

        await self.session.execute(
            text(
                "UPDATE document SET processing_stage = 'indexed', "
                "stage_updated_at = :now, indexed_at = :now, index_version = :version "
                "WHERE id = :id"
            ),
            {
                "id": document_id,
                "now": datetime.now(UTC),
                "version": spec.index_version,
            },
        )
        await self.session.commit()

        logger.info(
            "document_ingested",
            filename=path.name,
            chunks=len(outcome.chunks),
            embedded=embedded,
            sections=len(outcome.sections),
        )
        return embedded

    # -- writes --------------------------------------------------------------

    async def _write_document(self, path: Path, outcome: DocumentOutcome) -> uuid.UUID:
        metadata = outcome.metadata
        municipality_id = await self._resolve_municipality(metadata.municipality_slug)
        document_id = uuid.uuid4()

        await self.session.execute(
            text(
                "INSERT INTO document (id, municipality_id, filename, source_path, "
                " sha256, size_bytes, title, bylaw_number, year, consolidation_date, "
                " doc_type, status, page_count, is_scanned, ocr_applied, "
                " text_quality_score, metadata_source, metadata_confidence, "
                " amends_bylaw_numbers, processing_stage, stage_updated_at) "
                "VALUES (:id, :municipality_id, :filename, :source_path, :sha256, "
                " :size_bytes, :title, :bylaw_number, :year, :consolidation_date, "
                " CAST(:doc_type AS doc_type), CAST(:status AS document_status), "
                " :page_count, :is_scanned, :ocr_applied, :quality, "
                " CAST(NULLIF(:metadata_source, '') AS metadata_source), "
                " :metadata_confidence, CAST(:amends AS text[]), "
                " CAST(:stage AS processing_stage), :now)"
            ),
            {
                "id": document_id,
                "municipality_id": municipality_id,
                "filename": path.name,
                "source_path": str(path),
                "sha256": outcome.sha256,
                "size_bytes": path.stat().st_size,
                "title": metadata.title,
                "bylaw_number": metadata.bylaw_number,
                "year": metadata.year,
                "consolidation_date": metadata.consolidation_date,
                "doc_type": metadata.doc_type.value,
                # Currency is a property of the corpus, resolved by the lineage
                # pass once every document is known. A single-file ingest cannot
                # establish it, so it stays UNKNOWN rather than assuming.
                "status": metadata.status.value,
                "page_count": len(outcome.pages),
                "is_scanned": outcome.was_ocred,
                "ocr_applied": outcome.was_ocred,
                "quality": round(outcome.mean_extraction_confidence, 3),
                "metadata_source": (metadata.source.value if metadata.source else ""),
                "metadata_confidence": metadata.confidence,
                # Kept as printed. The lineage pass turns these into edges once
                # the referenced documents exist, which may be a later run.
                "amends": list(metadata.amends_bylaw_numbers),
                "stage": ProcessingStage.CHUNKED.value,
                "now": datetime.now(UTC),
            },
        )
        return document_id

    async def _resolve_municipality(self, slug: str | None) -> uuid.UUID | None:
        """Find or create the municipality row.

        Returns ``None`` when detection could not identify one. The document is
        still indexed, but it cannot be filtered by city and its chunks will not
        be citable at full precision — which the confidence scorer accounts for.
        """
        if not slug:
            return None

        existing = await self.session.scalar(
            text("SELECT id FROM municipality WHERE canonical_slug = :slug"),
            {"slug": slug},
        )
        if existing is not None:
            return uuid.UUID(str(existing))

        record = self.registry.get(slug)
        if record is None:
            return None

        # The province comes from the catalogue, not a constant. Hardcoding 'BC'
        # here filed every Alberta document under British Columbia — silently,
        # because nothing downstream reads the province except coverage.
        province_id = await self._province_for(slug)
        if province_id is None:
            logger.warning(
                "municipality_province_missing",
                municipality=slug,
                detail="no province row; the document will not be filterable by city",
            )
            return None

        municipality_id = uuid.uuid4()

        await self.session.execute(
            text(
                "INSERT INTO municipality (id, province_id, name, canonical_slug, "
                " region, classification, aliases) "
                "VALUES (:id, :province_id, :name, :slug, :region, :classification, "
                " CAST(:aliases AS varchar[]))"
            ),
            {
                "id": municipality_id,
                "province_id": province_id,
                "name": record.name,
                "slug": record.slug,
                "region": record.region,
                "classification": record.classification.value,
                "aliases": list(record.aliases),
            },
        )
        return municipality_id

    async def _province_for(self, municipality_slug: str) -> uuid.UUID | None:
        """Find the province a municipality belongs to, creating the row if new.

        A province absent from the database is a first ingest for that province,
        not an error — the catalogue is the source of truth for which provinces
        exist, and the table merely records the ones seen so far.
        """
        found = find_municipality(municipality_slug)
        if found is None:
            return None

        province, _ = found

        existing = await self.session.scalar(
            text("SELECT id FROM province WHERE code = :code"), {"code": province.code}
        )
        if existing is not None:
            return uuid.UUID(str(existing))

        province_id = uuid.uuid4()
        await self.session.execute(
            text(
                "INSERT INTO province (id, name, code, country_code) "
                "VALUES (:id, :name, :code, 'CA') "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"id": province_id, "name": province.name, "code": province.code},
        )
        logger.info("province_created", code=province.code, name=province.name)

        # Re-read: a concurrent ingest may have won the insert.
        resolved = await self.session.scalar(
            text("SELECT id FROM province WHERE code = :code"), {"code": province.code}
        )
        return uuid.UUID(str(resolved)) if resolved else None

    async def _write_pages(self, document_id: uuid.UUID, outcome: DocumentOutcome) -> None:
        for page in outcome.pages:
            await self.session.execute(
                text(
                    "INSERT INTO page (id, document_id, page_number, raw_text, "
                    " char_count, has_tables, was_ocred, ocr_confidence, "
                    " extraction_method, extraction_confidence, width, height, rotation) "
                    "VALUES (:id, :document_id, :page_number, :raw_text, :char_count, "
                    " :has_tables, :was_ocred, :ocr_confidence, "
                    " CAST(:method AS extraction_method), :confidence, :width, "
                    " :height, :rotation)"
                ),
                {
                    "id": uuid.uuid4(),
                    "document_id": document_id,
                    "page_number": page.page_number,
                    "raw_text": page.text,
                    "char_count": page.char_count,
                    "has_tables": page.has_tables,
                    "was_ocred": page.was_ocred,
                    "ocr_confidence": page.ocr_confidence,
                    "method": page.extraction_method.value,
                    "confidence": page.extraction_confidence,
                    "width": page.width,
                    "height": page.height,
                    "rotation": page.rotation,
                },
            )

    async def _write_sections(
        self, document_id: uuid.UUID, outcome: DocumentOutcome
    ) -> dict[str, uuid.UUID]:
        """Write the section tree. Returns ``full_path -> section_id``.

        Parents are inserted before children because the foreign key is
        self-referential; the parser already returns document order, which is
        parent-before-child by construction.
        """
        ids: dict[int, uuid.UUID] = {}
        by_path: dict[str, uuid.UUID] = {}

        for section in outcome.sections:
            section_id = uuid.uuid4()
            ids[section.index] = section_id
            by_path.setdefault(section.full_path, section_id)

            await self.session.execute(
                text(
                    "INSERT INTO section (id, document_id, parent_section_id, "
                    " section_number, full_path, heading, level, ordinal, "
                    " page_start, page_end, char_start, char_end) "
                    "VALUES (:id, :document_id, :parent_id, :number, :full_path, "
                    " :heading, :level, :ordinal, :page_start, :page_end, "
                    " :char_start, :char_end)"
                ),
                {
                    "id": section_id,
                    "document_id": document_id,
                    "parent_id": (
                        ids.get(section.parent_index) if section.parent_index is not None else None
                    ),
                    "number": section.section_number,
                    "full_path": section.full_path,
                    "heading": section.heading,
                    "level": section.level,
                    "ordinal": section.ordinal,
                    "page_start": section.page_start,
                    "page_end": section.page_end,
                    "char_start": section.char_start,
                    "char_end": section.char_end,
                },
            )

        return by_path

    async def _write_tables(
        self,
        document_id: uuid.UUID,
        outcome: DocumentOutcome,
        section_ids: dict[str, uuid.UUID],
    ) -> None:
        for ordinal, table in enumerate(outcome.tables):
            section_id = self._section_for_page(outcome, section_ids, table.page_number)
            await self.session.execute(
                text(
                    "INSERT INTO document_table (id, document_id, section_id, "
                    " page_number, ordinal, caption, headers, rows, row_count, "
                    " column_count, markdown, bbox) "
                    "VALUES (:id, :document_id, :section_id, :page_number, :ordinal, "
                    " :caption, CAST(:headers AS text[]), CAST(:rows AS jsonb), "
                    " :row_count, :column_count, :markdown, CAST(:bbox AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "document_id": document_id,
                    "section_id": section_id,
                    "page_number": table.page_number,
                    "ordinal": ordinal,
                    "caption": table.caption,
                    "headers": list(table.headers),
                    "rows": _json([list(row) for row in table.rows]),
                    "row_count": table.row_count,
                    "column_count": table.column_count,
                    "markdown": table.markdown,
                    "bbox": _json(list(table.bbox)) if table.bbox else None,
                },
            )

    @staticmethod
    def _section_for_page(
        outcome: DocumentOutcome, section_ids: dict[str, uuid.UUID], page: int
    ) -> uuid.UUID | None:
        candidates = [
            section
            for section in outcome.sections
            if section.page_start <= page <= section.page_end
        ]
        if not candidates:
            return None
        deepest = max(candidates, key=lambda section: section.level)
        return section_ids.get(deepest.full_path)

    async def _write_chunks(
        self,
        document_id: uuid.UUID,
        outcome: DocumentOutcome,
        section_ids: dict[str, uuid.UUID],
    ) -> list[uuid.UUID]:
        """Write chunks. Returns ids positionally aligned with ``outcome.chunks``."""
        chunk_ids = [uuid.uuid4() for _ in outcome.chunks]
        by_ordinal = {chunk.ordinal: chunk_ids[index] for index, chunk in enumerate(outcome.chunks)}

        for index, chunk in enumerate(outcome.chunks):
            section_id = section_ids.get(chunk.section.full_path) if chunk.section else None
            await self.session.execute(
                text(
                    "INSERT INTO chunk (id, document_id, section_id, parent_chunk_id, "
                    " page_number, page_end, ordinal, body, token_count, chunk_type, "
                    " content_hash, embedding_model, embedded_at, index_version) "
                    "VALUES (:id, :document_id, :section_id, :parent_id, :page_number, "
                    " :page_end, :ordinal, :body, :token_count, "
                    " CAST(:chunk_type AS chunk_type), :content_hash, :model, "
                    " :embedded_at, :index_version)"
                ),
                {
                    "id": chunk_ids[index],
                    "document_id": document_id,
                    "section_id": section_id,
                    "parent_id": (
                        by_ordinal.get(chunk.parent_ordinal)
                        if chunk.parent_ordinal is not None
                        else None
                    ),
                    "page_number": chunk.page_start,
                    "page_end": chunk.page_end,
                    "ordinal": chunk.ordinal,
                    "body": chunk.body,
                    "token_count": chunk.token_count,
                    "chunk_type": chunk.chunk_type.value,
                    "content_hash": _content_hash(chunk),
                    "model": self.embedder.model,
                    "embedded_at": datetime.now(UTC),
                    "index_version": self.settings.vector.index_version,
                },
            )

        return chunk_ids

    # -- embedding -----------------------------------------------------------

    async def _embed_chunks(
        self,
        collection_id: uuid.UUID,
        spec: CollectionSpec,
        chunks: Sequence[TextChunk],
        chunk_ids: Sequence[uuid.UUID],
    ) -> int:
        """Embed chunks and write vectors into the dimension-routed table."""
        if not chunks:
            return 0

        batch_size = self.settings.embedding.batch_size
        table = spec.table_name
        written = 0
        total = len(chunks)

        # Embedding is the longest step by far — minutes on CPU for a large
        # bylaw — and it committed nothing until the end, so an operator
        # watching the dashboard saw "processing" with no way to tell progress
        # from a hang. Logged per batch so the two are distinguishable.
        logger.info("embedding_started", chunks=total, batch_size=batch_size)

        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            ids = chunk_ids[start : start + batch_size]

            result = await self.embedder.embed_documents([chunk.body for chunk in batch])

            for chunk, chunk_id, vector in zip(batch, ids, result.vectors, strict=True):
                await self.session.execute(
                    text(
                        f"INSERT INTO {table} "  # noqa: S608 — table name is validated
                        "(collection_id, chunk_id, embedding, content_hash) "
                        "VALUES (:collection_id, :chunk_id, CAST(:embedding AS vector), "
                        " :content_hash) "
                        "ON CONFLICT (collection_id, chunk_id) DO UPDATE SET "
                        " embedding = EXCLUDED.embedding, "
                        " content_hash = EXCLUDED.content_hash"
                    ),
                    {
                        "collection_id": collection_id,
                        "chunk_id": chunk_id,
                        "embedding": _vector_literal(vector),
                        "content_hash": _content_hash(chunk),
                    },
                )
                written += 1

            logger.info(
                "embedding_progress",
                embedded=written,
                total=total,
                percent=round(100 * written / total),
            )

        return written

    # -- collection ----------------------------------------------------------

    async def _ensure_collection(self) -> tuple[uuid.UUID, CollectionSpec]:
        """Find or create the active collection for the current configuration."""
        spec = CollectionSpec(
            prefix=self.settings.vector.collection_prefix,
            embedding_model=self.settings.embedding.model,
            dimensions=self.settings.embedding.dimensions,
            index_version=self.settings.vector.index_version,
            chunking_version=self.settings.vector.chunking_version,
            distance_metric=self.settings.vector.distance_metric,
        )

        existing = await self.session.scalar(
            text("SELECT id FROM embedding_collection WHERE name = :name"),
            {"name": spec.name},
        )
        if existing is not None:
            collection_id = uuid.UUID(str(existing))
            await self._activate(collection_id)
            return collection_id, spec

        collection_id = uuid.uuid4()
        await self.session.execute(
            text(
                "INSERT INTO embedding_collection (id, name, embedding_model, "
                " embedding_model_revision, dimensions, chunking_version, "
                " index_version, distance_metric, status) "
                "VALUES (:id, :name, :model, :revision, :dimensions, "
                " :chunking_version, :index_version, :metric, "
                " CAST(:status AS collection_status))"
            ),
            {
                "id": collection_id,
                "name": spec.name,
                "model": spec.embedding_model,
                "revision": getattr(self.embedder, "model_revision", None),
                "dimensions": spec.dimensions,
                "chunking_version": spec.chunking_version,
                "index_version": spec.index_version,
                "metric": spec.distance_metric,
                "status": CollectionStatus.BUILDING.value,
            },
        )
        await self._activate(collection_id)
        await self.session.commit()

        logger.info("collection_created", name=spec.name, dimensions=spec.dimensions)
        return collection_id, spec

    async def _activate(self, collection_id: uuid.UUID) -> None:
        """Make one collection active.

        A partial unique index enforces at most one active collection, so the
        previous one is retired first — retained rather than deleted, so a bad
        rebuild can be rolled back with one UPDATE.
        """
        await self.session.execute(
            text(
                "UPDATE embedding_collection SET status = 'retired', "
                " retired_at = now() "
                "WHERE status = 'active' AND id <> :id"
            ),
            {"id": collection_id},
        )
        await self.session.execute(
            text(
                "UPDATE embedding_collection SET status = 'active', "
                " activated_at = COALESCE(activated_at, now()) WHERE id = :id"
            ),
            {"id": collection_id},
        )

    async def _refresh_collection_count(self, collection_id: uuid.UUID) -> None:
        # The interpolated fragment is the dimension, an int validated at boot
        # against SUPPORTED_EMBEDDING_DIMENSIONS. It can never be user input.
        await self.session.execute(
            text(
                "UPDATE embedding_collection c SET chunk_count = ("  # noqa: S608
                "  SELECT count(*) FROM chunk_embedding_"
                f"{int(self.settings.embedding.dimensions)} e "
                "  WHERE e.collection_id = c.id) "
                "WHERE c.id = :id"
            ),
            {"id": collection_id},
        )
        await self.session.commit()


def _content_hash(chunk: TextChunk) -> str:
    """Hash the chunk body so an unchanged chunk can skip re-embedding."""
    import hashlib

    return hashlib.sha256(chunk.body.encode("utf-8")).hexdigest()


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


def _json(value: object) -> str:
    import json

    return json.dumps(value, default=str)
