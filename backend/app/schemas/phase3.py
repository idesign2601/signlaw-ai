"""Contracts for zoning, compliance, permits and comparison.

One convention throughout: **a computed number never appears without the text it
was computed from.** Every response type below either carries citations or
carries an explicit statement that the rule could not be established.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ChecklistItemOut",
    "ComparisonRequest",
    "ComparisonResponse",
    "ComparisonRowOut",
    "ComplianceCheckOut",
    "ComplianceRequest",
    "ComplianceResponse",
    "EvidenceOut",
    "PermitChecklistResponse",
    "ZoningRequest",
    "ZoningResponse",
]


class EvidenceOut(BaseModel):
    """The bylaw passage a number was read from."""

    model_config = ConfigDict(frozen=True)

    quote: str = Field(description="Verbatim. Never paraphrased.")
    section: str | None = None
    page: int | None = None
    document_title: str | None = None
    bylaw_number: str | None = None
    municipality: str | None = None


# --- zoning ------------------------------------------------------------------


class ZoningRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    address: str = Field(min_length=5, max_length=300)
    municipality: str | None = Field(
        default=None,
        description=(
            "Municipality slug. When omitted it is detected from the address — "
            "and an ambiguous name returns candidates rather than a guess."
        ),
    )


class ZoningResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: str = Field(description="resolved, stale, unsupported or not_found.")
    address: str
    municipality: str | None = None
    zoning_code: str | None = None
    zoning_description: str | None = None
    parcel_number: str | None = None
    source_url: str | None = None
    map_url: str | None = Field(
        default=None, description="The city's own map, for a person to verify against."
    )
    provider: str | None = None
    confidence: float = 0.0
    as_of: datetime | None = Field(
        default=None,
        description=(
            "When the underlying data was fetched. Zoning changes by rezoning "
            "application, so the answer is only as good as this date."
        ),
    )
    candidates: list[str] = Field(
        default_factory=list,
        description="Populated when the municipality in the address was ambiguous.",
    )
    detail: str = ""


# --- compliance ---------------------------------------------------------------


class ComplianceRequest(BaseModel):
    """A proposed sign. Metric throughout."""

    model_config = ConfigDict(frozen=True)

    sign_type: str = Field(description="fascia, channel_letter, pylon, window, …")
    municipality: str
    area_sq_m: float | None = Field(default=None, gt=0, le=10000)
    height_m: float | None = Field(default=None, gt=0, le=200)
    width_m: float | None = Field(default=None, gt=0, le=500)
    setback_m: float | None = Field(default=None, ge=0, le=500)
    frontage_m: float | None = Field(
        default=None,
        gt=0,
        le=1000,
        description=(
            "Building or unit frontage. Most fascia area limits are a ratio of "
            "it, and without it those rules cannot be evaluated."
        ),
    )
    illuminated: bool | None = None
    zoning_code: str | None = None


class ComplianceCheckOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    dimension: str
    outcome: str = Field(description="complies, exceeds or insufficient_information.")
    proposed: float | None = None
    limit: float | None = None
    unit: str = ""
    evidence: EvidenceOut | None = None
    detail: str = ""


class ComplianceResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: str = Field(
        description=(
            "The overall verdict. `insufficient_information` when any check "
            "could not be established — a sign is not compliant because the "
            "checks that ran happened to pass."
        )
    )
    sign_type: str
    municipality: str
    checks: list[ComplianceCheckOut]
    warnings: list[str] = Field(default_factory=list)


# --- permits ------------------------------------------------------------------


class ChecklistItemOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    topic: str
    label: str
    found: bool = Field(
        description=(
            "False when the bylaw is silent on this topic. Shown rather than "
            "omitted: an absent requirement and an unchecked one look identical "
            "in a list."
        )
    )
    quote: str = ""
    section: str | None = None
    page: int | None = None
    bylaw_number: str | None = None
    detail: str = ""


class PermitChecklistResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    municipality: str
    sign_type: str
    items: list[ChecklistItemOut]
    warnings: list[str] = Field(default_factory=list)


# --- comparison ---------------------------------------------------------------


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    sign_type: str
    municipalities: list[str] = Field(min_length=2, max_length=5)


class ComparisonSideOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    municipality: str
    value: float | None = None
    unit: str = ""
    is_ratio_of_frontage: bool = False
    evidence: EvidenceOut | None = None
    detail: str = ""


class ComparisonRowOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    dimension: str
    sides: list[ComparisonSideOut]
    comparable: bool = Field(
        description=(
            "False when the values are in different units or different forms — "
            "a flat limit beside a per-metre ratio is not a comparison."
        )
    )
    summary: str


class ComparisonResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    sign_type: str
    municipalities: list[str]
    rows: list[ComparisonRowOut]
    warnings: list[str] = Field(default_factory=list)
