"""Zoning, compliance, permits and comparison endpoints.

Thin translation between HTTP and the services. No rules, no arithmetic and no
retrieval logic here.

Every one of these returns HTTP 200 for "I could not establish that", because
none of them is an error. A caller must branch on the outcome field.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import DbSession, RagServiceDep
from app.core.logging import get_logger
from app.domain.provinces import find_municipality
from app.schemas.phase3 import (
    ChecklistItemOut,
    ComparisonRequest,
    ComparisonResponse,
    ComparisonRowOut,
    ComparisonSideOut,
    ComplianceCheckOut,
    ComplianceRequest,
    ComplianceResponse,
    EvidenceOut,
    PermitChecklistResponse,
    ZoningRequest,
    ZoningResponse,
)
from app.services.address import AddressOutcome, AddressParser
from app.services.comparison import MunicipalityComparisonService
from app.services.compliance import ComplianceEngine, SignSpec, SignType
from app.services.compliance.base import MeasuredValue
from app.services.permits import PermitChecklistService
from app.services.zoning import ZoningService

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(tags=["phase3"])


@router.post(
    "/zoning/lookup",
    response_model=ZoningResponse,
    summary="Resolve an address to its zoning district",
    description=(
        "Asks the municipality's own open data. Four outcomes, and they mean "
        "different things: `resolved`, `stale` (cached, provider unreachable — "
        "check the as-of date), `unsupported` (no queryable data published for "
        "that city) and `not_found` (the provider ran and matched nothing).\n\n"
        "An address whose municipality is ambiguous returns candidates rather "
        "than picking one: the City and Township of Langley have separate "
        "zoning and separate sign bylaws."
    ),
)
async def zoning_lookup(payload: ZoningRequest, session: DbSession) -> ZoningResponse:
    slug = payload.municipality
    address = payload.address

    if slug is None:
        parsed = AddressParser().parse(payload.address)

        if parsed.outcome is AddressOutcome.AMBIGUOUS_MUNICIPALITY:
            return ZoningResponse(
                outcome="ambiguous_municipality",
                address=payload.address,
                candidates=list(parsed.candidates),
                detail=parsed.detail,
            )

        if not parsed.outcome.is_usable or parsed.municipality is None:
            return ZoningResponse(
                outcome="not_found", address=payload.address, detail=parsed.detail
            )

        slug = parsed.municipality.slug
        address = parsed.street_address

    report = await ZoningService(session=session).lookup(address, slug)

    return ZoningResponse(
        outcome=report.outcome.value,
        address=report.address,
        municipality=report.municipality_slug,
        zoning_code=report.zoning_code,
        zoning_description=report.zoning_description,
        parcel_number=report.parcel_number,
        source_url=report.source_url,
        map_url=report.map_url,
        provider=report.provider,
        confidence=report.confidence,
        as_of=report.as_of,
        detail=report.detail,
    )


@router.post(
    "/compliance/check",
    response_model=ComplianceResponse,
    summary="Check a proposed sign against the bylaw",
    description=(
        "Retrieves the governing section for each dimension, reads the limit "
        "out of the retrieved text, and compares. **Every verdict carries the "
        "passage it was computed from.**\n\n"
        "No municipal regulation is stored in this system. Where a rule cannot "
        "be retrieved or its number cannot be read, the result is "
        "`insufficient_information` — never a pass and never a fail."
    ),
)
async def compliance_check(
    payload: ComplianceRequest, service: RagServiceDep
) -> ComplianceResponse:
    sign_type = _sign_type(payload.sign_type)
    _municipality(payload.municipality)

    engine = ComplianceEngine(retriever=service.retriever)
    report = await engine.check(
        SignSpec(
            sign_type=sign_type,
            municipality_slug=payload.municipality,
            area_sq_m=payload.area_sq_m,
            height_m=payload.height_m,
            width_m=payload.width_m,
            setback_m=payload.setback_m,
            illuminated=payload.illuminated,
            frontage_m=payload.frontage_m,
            zoning_code=payload.zoning_code,
        )
    )

    return ComplianceResponse(
        outcome=report.outcome.value,
        sign_type=sign_type.value,
        municipality=payload.municipality,
        checks=[
            ComplianceCheckOut(
                dimension=check.dimension.value,
                outcome=check.outcome.value,
                proposed=check.proposed,
                limit=check.limit,
                unit=check.unit,
                evidence=_evidence(check.evidence),
                detail=check.detail,
            )
            for check in report.checks
        ],
        warnings=report.warnings,
    )


@router.get(
    "/permits/checklist",
    response_model=PermitChecklistResponse,
    summary="Permit requirements drawn from the bylaw",
    description=(
        "Assembled from the bylaw's own permit provisions, each item cited. "
        "Topics the bylaw does not address are returned marked `found: false` "
        "rather than omitted — an absent requirement and an unchecked one look "
        "identical in a list.\n\n"
        "Not a substitute for the municipality's application guide, which is "
        "not indexed here."
    ),
)
async def permit_checklist(
    sign_type: str, municipality: str, service: RagServiceDep
) -> PermitChecklistResponse:
    resolved = _sign_type(sign_type)
    _municipality(municipality)

    checklist = await PermitChecklistService(retriever=service.retriever).build(
        resolved, municipality
    )

    return PermitChecklistResponse(
        municipality=municipality,
        sign_type=resolved.value,
        items=[
            ChecklistItemOut(
                topic=item.topic.value,
                label=item.label,
                found=item.found,
                quote=item.quote,
                section=item.section,
                page=item.page,
                bylaw_number=item.bylaw_number,
                detail=item.detail,
            )
            for item in checklist.items
        ],
        warnings=checklist.warnings,
    )


@router.post(
    "/compare",
    response_model=ComparisonResponse,
    summary="Compare sign rules across municipalities",
    description=(
        "Each municipality is retrieved and measured **separately**, then placed "
        "side by side. A single retrieval across several corpora mixes them, and "
        "an answer that attributes one city's limit to another is wrong in a way "
        "that reads perfectly.\n\n"
        "Rows are marked `comparable: false` where the figures are in different "
        "units or different forms — a flat limit beside a per-metre ratio is not "
        "a comparison."
    ),
)
async def compare(payload: ComparisonRequest, service: RagServiceDep) -> ComparisonResponse:
    sign_type = _sign_type(payload.sign_type)
    for slug in payload.municipalities:
        _municipality(slug)

    comparison = MunicipalityComparisonService(engine=ComplianceEngine(retriever=service.retriever))
    report = await comparison.compare(sign_type, tuple(payload.municipalities))

    return ComparisonResponse(
        sign_type=sign_type.value,
        municipalities=list(payload.municipalities),
        rows=[
            ComparisonRowOut(
                dimension=row.dimension.value,
                sides=[
                    ComparisonSideOut(
                        municipality=side.municipality_slug,
                        value=side.value,
                        unit=side.unit,
                        is_ratio_of_frontage=side.is_ratio_of_frontage,
                        evidence=_evidence(side.evidence),
                        detail=side.detail,
                    )
                    for side in row.sides
                ],
                comparable=row.is_comparable,
                summary=row.summary,
            )
            for row in report.rows
        ],
        warnings=report.warnings,
    )


# -- helpers ------------------------------------------------------------------


def _sign_type(value: str) -> SignType:
    try:
        return SignType(value.strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown sign type '{value}'. Expected one of: "
                + ", ".join(item.value for item in SignType)
            ),
        ) from exc


def _municipality(slug: str) -> None:
    if find_municipality(slug) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown municipality '{slug}'. Use a slug from /municipalities.",
        )


def _evidence(value: MeasuredValue | None) -> EvidenceOut | None:
    if value is None:
        return None
    return EvidenceOut(
        quote=value.quote,
        section=value.section,
        page=value.page,
        document_title=value.document_title,
        bylaw_number=value.bylaw_number,
        municipality=value.municipality,
    )
