"""Admin document management.

Guarded by ``X-Admin-Key`` on the router, so a route added here later is
protected by default.

Kept separate from the answering router because the two have different
audiences and different secrets: the public API key lets a client ask
questions, the admin key lets someone change what the answers are made of.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import text

from app.api.deps import DbSession, SettingsDep
from app.core.logging import get_logger
from app.domain.provinces import find_municipality, find_province
from app.schemas.admin import (
    DocumentListResponse,
    DocumentState,
    DocumentSummary,
    PendingUpload,
    UploadAccepted,
    ZoningConfigRequest,
)
from app.services.upload_service import UploadRequest, create_job, process_upload, store_upload
from app.services.zoning.presets import preset_for
from app.services.zoning.providers import PROVIDER_KINDS

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/documents/upload",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a bylaw PDF",
    description=(
        "Accepts the PDF and begins indexing it in the background: extraction, "
        "OCR where a page has no text layer, table detection, section parsing, "
        "chunking and embedding.\n\n"
        "Returns 202 immediately. A large scanned bylaw takes minutes, which no "
        "proxy will hold a connection open for — poll `GET /admin/documents` "
        "for progress.\n\n"
        "The supplied title, year and municipality override automatic detection. "
        "Whether the bylaw is in force is **not** settable here: currency is "
        "resolved across the whole corpus by the lineage pass, and asserting it "
        "from an upload form is how confident citations to repealed law happen."
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Not a PDF, or unknown municipality."},
        status.HTTP_413_CONTENT_TOO_LARGE: {"description": "File exceeds the limit."},
    },
)
async def upload_document(
    request: Request,
    background: BackgroundTasks,
    session: DbSession,
    settings: SettingsDep,
    province: Annotated[str, Form(description="Province code, e.g. BC.")],
    municipality: Annotated[str, Form(description="Municipality slug.")],
    title: Annotated[str, Form(min_length=3, max_length=500)],
    # UploadFile, not bytes. A multipart file part carries a filename and
    # content type, and declaring it as raw bytes makes coercion depend on how
    # the client happened to encode it — which is a contract that holds until
    # someone uses a different HTTP library.
    file: Annotated[UploadFile, File(description="The bylaw PDF.")],
    year: Annotated[int | None, Form(ge=1900, le=2100)] = None,
) -> UploadAccepted:
    if not find_province(province):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown province '{province}'.",
        )

    found = find_municipality(municipality)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"Unknown municipality '{municipality}'. Use a slug from GET /municipalities."),
        )

    province_record, municipality_record = found
    if province_record.code.upper() != province.strip().upper():
        # Catches a form that let the two selectors drift apart, which would
        # otherwise file a Calgary bylaw under British Columbia.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"{municipality_record.official_name} is in {province_record.name}, not {province}."
            ),
        )

    content = await file.read()

    limit_bytes = settings.security.max_request_body_mb * 1024 * 1024
    if len(content) > limit_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"File is {len(content) // 1_048_576} MB; the limit is "
                f"{settings.security.max_request_body_mb} MB."
            ),
        )

    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty.",
        )

    upload = UploadRequest(
        municipality_slug=municipality_record.slug,
        title=title,
        year=year,
        original_filename=file.filename or "upload.pdf",
        content=content,
    )

    try:
        path = store_upload(upload, settings)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    job_id = await create_job(session, path)
    await session.commit()

    background.add_task(
        process_upload,
        job_id=job_id,
        path=path,
        request=upload,
        settings=settings,
        session_factory=request.app.state.session_factory,
        embedder=request.app.state.embedder,
    )

    logger.info(
        "upload_accepted",
        filename=path.name,
        municipality=municipality_record.slug,
        bytes=len(content),
    )

    return UploadAccepted(
        job_id=str(job_id),
        filename=path.name,
        message=(
            "Indexing started. This takes a few minutes for a large or scanned "
            "bylaw; the dashboard shows progress."
        ),
    )


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="Every indexed document, plus uploads still processing",
    description=(
        "Uploads still being processed are returned separately from documents. "
        "Without that split, an upload that is still working and an upload that "
        "finished and produced nothing look identical."
    ),
)
async def list_documents(session: DbSession) -> DocumentListResponse:
    documents = await _documents(session)
    return DocumentListResponse(
        documents=documents,
        pending=await _pending(session),
        total=len(documents),
    )


async def _documents(session: DbSession) -> list[DocumentSummary]:
    result = await session.execute(
        text(
            "SELECT d.id, d.filename, d.title, d.bylaw_number, d.year, "
            "       d.page_count, d.processing_stage, d.failed_stage, d.status, "
            "       d.created_at, d.indexed_at, d.ocr_applied, d.text_quality_score, "
            "       m.name AS municipality_name, m.classification, "
            "       (SELECT count(*) FROM chunk c WHERE c.document_id = d.id) AS chunks "
            "FROM document d "
            "LEFT JOIN municipality m ON m.id = d.municipality_id "
            "ORDER BY d.created_at DESC "
            "LIMIT 500"
        )
    )

    return [
        DocumentSummary(
            id=str(row.id),
            municipality=row.municipality_name,
            filename=row.filename,
            title=row.title,
            bylaw_number=row.bylaw_number,
            year=row.year,
            pages=row.page_count or 0,
            chunks=int(row.chunks or 0),
            state=DocumentState.from_stage(row.processing_stage),
            status=row.status,
            uploaded_at=row.created_at,
            indexed_at=row.indexed_at,
            failed_stage=row.failed_stage,
            ocr_applied=bool(row.ocr_applied),
            text_quality=row.text_quality_score,
        )
        for row in result
    ]


async def _pending(session: DbSession) -> list[PendingUpload]:
    """Jobs that have not yet produced a document row.

    A job whose document exists is omitted: it would otherwise appear twice,
    once as pending and once as indexed.
    """
    result = await session.execute(
        text(
            "SELECT j.id, j.source_path, j.status, j.created_at, j.error_log "
            "FROM ingestion_job j "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM document d WHERE d.source_path = j.source_path"
            ") "
            "ORDER BY j.created_at DESC "
            "LIMIT 50"
        )
    )

    return [
        PendingUpload(
            job_id=str(row.id),
            filename=row.source_path.rsplit("/", 1)[-1],
            state=DocumentState.from_job(row.status),
            created_at=row.created_at,
            error=_first_error(row.error_log),
        )
        for row in result
    ]


@router.get(
    "/municipalities/{slug}/zoning",
    summary="A municipality's zoning provider configuration",
    description=(
        "Returns the live configuration and, where one exists, a suggested "
        "starting point. Presets are suggestions for an operator to verify, "
        "never a runtime fallback."
    ),
)
async def get_zoning_config(slug: str, session: DbSession) -> dict[str, object]:
    row = (
        await session.execute(
            text(
                "SELECT gis_provider, gis_endpoint, gis_config, gis_verified, map_url "
                "FROM municipality WHERE canonical_slug = :slug"
            ),
            {"slug": slug},
        )
    ).first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{slug}' has no municipality record yet — ingest a bylaw first.",
        )

    return {
        "municipality": slug,
        "kind": row.gis_provider,
        "endpoint": row.gis_endpoint,
        "config": row.gis_config or {},
        "verified": bool(row.gis_verified),
        "map_url": row.map_url,
        "kinds": list(PROVIDER_KINDS),
        "preset": preset_for(slug),
    }


@router.put(
    "/municipalities/{slug}/zoning",
    summary="Configure a municipality's zoning provider",
    description=(
        "Adding a city is this call plus its bylaw PDFs. No code change: the "
        "kind names a query grammar, and `config` carries the field mapping.\n\n"
        "**`verified` gates whether the provider is ever queried.** An endpoint "
        "that responds but carries a similar-looking field returns a "
        "confidently wrong zone, so configuration is inert until someone has "
        "checked it against the city's own service directory."
    ),
)
async def put_zoning_config(
    slug: str, payload: ZoningConfigRequest, session: DbSession
) -> dict[str, object]:
    if payload.kind is not None and payload.kind not in PROVIDER_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown provider kind '{payload.kind}'. Expected one of: "
                + ", ".join(PROVIDER_KINDS)
            ),
        )

    result = await session.execute(
        text(
            "UPDATE municipality SET "
            " gis_provider = :kind, gis_endpoint = :endpoint, "
            " gis_config = CAST(:config AS jsonb), gis_verified = :verified, "
            " map_url = :map_url "
            "WHERE canonical_slug = :slug "
            "RETURNING id"
        ),
        {
            "slug": slug,
            "kind": payload.kind,
            "endpoint": payload.endpoint,
            "config": _json(payload.config),
            "verified": payload.verified,
            "map_url": payload.map_url,
        },
    )

    if result.first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{slug}' has no municipality record yet — ingest a bylaw first.",
        )

    await session.commit()
    logger.info("zoning_config_updated", municipality=slug, verified=payload.verified)

    return {"municipality": slug, "verified": payload.verified}


def _json(value: object) -> str:
    import json

    return json.dumps(value, default=str)


def _first_error(error_log: object) -> str | None:
    if isinstance(error_log, list) and error_log:
        entry = error_log[0]
        if isinstance(entry, dict):
            value = entry.get("error")
            return str(value) if value is not None else None
    return None
