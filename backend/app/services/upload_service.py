"""Admin document uploads.

Accepts a PDF plus the metadata an operator knows, stores it in the corpus
directory, and runs it through the existing :class:`IngestionService`. No new
pipeline: extraction, OCR fallback, table detection, section parsing, chunking
and embedding are exactly what a command-line ingest does.

**Processing happens after the response.** A large scanned bylaw takes minutes,
which no HTTP client or proxy will wait for. The upload is acknowledged with the
job id, and progress is read from the dashboard.

**Admin metadata overrides detection.** The detector reads the filename and the
first three pages and is usually right; the operator uploading the file is
right by definition. Detection still runs, because it also finds the bylaw
number and consolidation date, but the fields the operator supplied win.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.embeddings.base import EmbeddingProviderProtocol
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.enums import JobStatus
from app.services.ingestion_service import IngestionService

__all__ = ["UploadRequest", "process_upload", "store_upload"]

logger = get_logger(__name__)

# PDFs begin with %PDF-. Checked against the bytes rather than trusting the
# declared content type, which the client controls and can simply be wrong.
_PDF_MAGIC = b"%PDF-"

_UNSAFE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class UploadRequest:
    """What the operator supplied."""

    municipality_slug: str
    title: str
    year: int | None
    original_filename: str
    content: bytes


def build_filename(request: UploadRequest) -> str:
    """Compose a filename carrying the municipality, title and year.

    Not cosmetic. ``MetadataDetector._from_filename`` reads only the basename,
    so a file called ``scan001.pdf`` gives detection nothing to work with and it
    falls back to whatever the cover page happens to say. Encoding the
    municipality here means detection agrees with the operator instead of
    contradicting them.
    """
    parts = [request.municipality_slug, _UNSAFE.sub("-", request.title.lower()).strip("-")]
    if request.year:
        parts.append(str(request.year))

    stem = "_".join(part for part in parts if part)[:180]
    return f"{stem or uuid.uuid4().hex}.pdf"


def store_upload(request: UploadRequest, settings: Settings) -> Path:
    """Write the PDF into the corpus directory and return its path.

    Raises:
        ValueError: The bytes are not a PDF.
    """
    if not request.content.startswith(_PDF_MAGIC):
        raise ValueError(
            "That file is not a PDF. Its contents do not begin with %PDF-, "
            "whatever its extension says."
        )

    corpus = settings.ingestion.corpus_dir
    corpus.mkdir(parents=True, exist_ok=True)

    # The filename is composed from a validated slug and a sanitised title, so
    # it cannot contain a separator — but resolve and re-check anyway, because
    # a path traversal here would let an upload overwrite arbitrary files.
    target = (corpus / build_filename(request)).resolve()
    if not target.is_relative_to(corpus.resolve()):
        raise ValueError("Refusing to write outside the corpus directory.")

    target.write_bytes(request.content)
    return target


async def create_job(session: AsyncSession, path: Path) -> uuid.UUID:
    """Record the upload so the dashboard can show it before it finishes."""
    job_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO ingestion_job (id, status, source_path, total_documents) "
            "VALUES (:id, CAST(:status AS job_status), :path, 1)"
        ),
        {"id": job_id, "status": JobStatus.PENDING.value, "path": str(path)},
    )
    return job_id


async def process_upload(
    *,
    job_id: uuid.UUID,
    path: Path,
    request: UploadRequest,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    embedder: EmbeddingProviderProtocol,
) -> None:
    """Ingest an uploaded PDF. Runs after the response has been sent.

    Opens its own session: the request-scoped one is closed by the time this
    runs. Never raises — a failure is recorded on the job row, because there is
    no caller left to receive an exception and an unrecorded failure would leave
    the dashboard showing "processing" forever.
    """
    async with session_factory() as session:
        try:
            await _mark(session, job_id, JobStatus.RUNNING, started=True)

            service = IngestionService(
                session=session, settings=settings, embedder=embedder
            )
            # force=True: an operator re-uploading a file is asking for it to be
            # re-indexed, even when the bytes are identical.
            result = await service.ingest_paths([path], force=True)

            if result.failed:
                _, reason = result.failed[0]
                await _fail(session, job_id, reason)
                logger.warning("upload_ingest_failed", filename=path.name, reason=reason)
                return

            await _apply_operator_metadata(session, path, request)
            await _complete(session, job_id, chunks=result.chunks_written)
            await session.commit()

            logger.info(
                "upload_ingested",
                filename=path.name,
                chunks=result.chunks_written,
                skipped=len(result.skipped),
            )
        except Exception as exc:  # noqa: BLE001 — nothing upstream can catch this
            logger.exception("upload_processing_failed", filename=path.name)
            await session.rollback()
            await _fail(session, job_id, str(exc))


async def _apply_operator_metadata(
    session: AsyncSession, path: Path, request: UploadRequest
) -> None:
    """Let the operator's title, year and municipality win over detection.

    ``document_status`` is deliberately untouched. Whether a bylaw is in force
    is resolved by the lineage pass across the whole corpus, and letting an
    upload form assert it would be the one field where a well-meaning mistake
    produces confident citations to superseded law.
    """
    municipality_id = await session.scalar(
        text("SELECT id FROM municipality WHERE canonical_slug = :slug"),
        {"slug": request.municipality_slug},
    )

    await session.execute(
        text(
            "UPDATE document SET "
            " title = COALESCE(NULLIF(:title, ''), title), "
            " year = COALESCE(:year, year), "
            " municipality_id = COALESCE(:municipality_id, municipality_id), "
            " metadata_source = 'manual', "
            " metadata_confidence = 1.0 "
            "WHERE source_path = :path"
        ),
        {
            "title": request.title.strip(),
            "year": request.year,
            "municipality_id": municipality_id,
            "path": str(path),
        },
    )


async def _mark(
    session: AsyncSession, job_id: uuid.UUID, status: JobStatus, *, started: bool = False
) -> None:
    # Two whole statements rather than one assembled from fragments. Building
    # SQL by concatenation is how an injection gets in eventually, even when
    # this particular branch is driven by a boolean.
    statement = (
        "UPDATE ingestion_job SET status = CAST(:status AS job_status), "
        " started_at = :now WHERE id = :id"
        if started
        else "UPDATE ingestion_job SET status = CAST(:status AS job_status) "
        "WHERE id = :id"
    )

    await session.execute(
        text(statement),
        {"id": job_id, "status": status.value, "now": datetime.now(UTC)},
    )
    await session.commit()


async def _complete(session: AsyncSession, job_id: uuid.UUID, *, chunks: int) -> None:
    await session.execute(
        text(
            "UPDATE ingestion_job SET status = CAST(:status AS job_status), "
            " processed_documents = 1, total_chunks = :chunks, finished_at = :now "
            "WHERE id = :id"
        ),
        {
            "id": job_id,
            "status": JobStatus.COMPLETED.value,
            "chunks": chunks,
            "now": datetime.now(UTC),
        },
    )


async def _fail(session: AsyncSession, job_id: uuid.UUID, reason: str) -> None:
    """Record the failure. Best effort — the session may already be broken."""
    try:
        await session.execute(
            text(
                "UPDATE ingestion_job SET status = CAST(:status AS job_status), "
                " failed_documents = 1, finished_at = :now, "
                " error_log = CAST(:error AS jsonb) "
                "WHERE id = :id"
            ),
            {
                "id": job_id,
                "status": JobStatus.FAILED.value,
                "now": datetime.now(UTC),
                "error": _json([{"error": reason[:2000]}]),
            },
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — the log is the last resort
        logger.exception("upload_failure_not_recorded", job_id=str(job_id))


def _json(value: object) -> str:
    import json

    return json.dumps(value, default=str)
