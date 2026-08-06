"""Coverage endpoint.

Drives the province and municipality selectors, and the "supported coverage"
list. The catalogue of what municipalities *exist* is static
(:mod:`app.domain.provinces`); whether each can actually be asked about is read
from the database on every request.

That split is deliberate. A hand-maintained "supported cities" list is wrong the
first time an ingest fails silently, and wrong in the worst direction — the
interface invites a question the corpus cannot answer, and the user reads the
resulting abstention as the bylaw being silent.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession
from app.core.logging import get_logger
from app.db.enums import DocumentStatus
from app.domain.provinces import PROVINCES
from app.schemas.coverage import (
    CoverageResponse,
    MunicipalityCoverage,
    ProvinceCoverage,
)

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(tags=["coverage"])


@router.get(
    "/municipalities",
    response_model=CoverageResponse,
    summary="Provinces and municipalities, with real coverage",
    description=(
        "Every catalogued municipality, flagged by whether in-force bylaw "
        "documents are actually indexed for it. Municipalities with no indexed "
        "documents are returned with `available: false` — show them as coming "
        "soon rather than offering them as a choice.\n\n"
        "Both Langleys and both North Vancouvers appear as separate entries "
        "under their official names. There is no bare 'Langley': the City and "
        "the Township have different sign bylaws, and picking one silently is "
        "the failure this system exists to prevent."
    ),
)
async def list_municipalities(session: DbSession) -> CoverageResponse:
    counts = await _indexed_document_counts(session)

    provinces: list[ProvinceCoverage] = []
    total_available = 0

    for province in PROVINCES:
        municipalities: list[MunicipalityCoverage] = []

        for record in province.municipalities:
            count = counts.get(record.slug, 0)
            if count:
                total_available += 1

            municipalities.append(
                MunicipalityCoverage(
                    slug=record.slug,
                    name=record.name,
                    official_name=record.official_name,
                    classification=record.classification.value,
                    region=record.region,
                    available=count > 0,
                    document_count=count,
                )
            )

        provinces.append(
            ProvinceCoverage(
                code=province.code,
                name=province.name,
                available=any(item.available for item in municipalities),
                municipalities=municipalities,
            )
        )

    return CoverageResponse(provinces=provinces, total_available=total_available)


async def _indexed_document_counts(session: AsyncSession) -> dict[str, int]:
    """In-force document count per municipality slug.

    Only ``in_force`` counts. A municipality whose every indexed bylaw has been
    repealed is not one this system can answer for, and reporting it as
    available would be a lie the interface then repeats to the user.
    """
    result = await session.execute(
        text(
            "SELECT m.slug, count(d.id) AS documents "
            "FROM municipality m "
            "JOIN document d ON d.municipality_id = m.id "
            "WHERE d.status = CAST(:in_force AS document_status) "
            "GROUP BY m.slug"
        ),
        {"in_force": DocumentStatus.IN_FORCE.value},
    )
    return {row.slug: int(row.documents) for row in result}
