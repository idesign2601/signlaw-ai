"""Coverage response contract.

What the frontend renders in its province and municipality selectors, and in
the "supported coverage" list. Availability is computed from indexed documents
rather than declared, so the list cannot claim a municipality the corpus cannot
actually answer for.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CoverageResponse", "MunicipalityCoverage", "ProvinceCoverage"]


class MunicipalityCoverage(BaseModel):
    """One municipality, and whether it can actually be asked about."""

    model_config = ConfigDict(frozen=True)

    slug: str = Field(description="Stable identifier; what /ask expects.")
    name: str = Field(description="Bare name, e.g. 'Langley'.")
    official_name: str = Field(
        description=(
            "Name as it appears on the bylaw, e.g. 'Township of Langley'. "
            "Always use this for display: 'Langley' alone is ambiguous, and the "
            "City and the Township have separate sign bylaws."
        )
    )
    classification: str = Field(description="city, district, township, village, …")
    region: str | None = None
    available: bool = Field(
        description=(
            "True when at least one in-force bylaw document is indexed. False "
            "means catalogued but not yet ingested — show it as coming soon "
            "rather than offering it as a choice."
        )
    )
    document_count: int = Field(
        default=0, description="In-force bylaw documents indexed for this municipality."
    )


class ProvinceCoverage(BaseModel):
    """One province and its catalogued municipalities."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(description="Two-letter code, e.g. 'BC'.")
    name: str
    available: bool = Field(
        description="True when any municipality in this province is available."
    )
    municipalities: list[MunicipalityCoverage]


class CoverageResponse(BaseModel):
    """Everything the frontend needs to build its selectors.

    The frontend contains no province logic: it renders this list. Adding a
    province means adding records and ingesting PDFs, not editing templates.
    """

    model_config = ConfigDict(frozen=True)

    provinces: list[ProvinceCoverage]
    total_available: int = Field(
        description="Municipalities with at least one in-force document indexed."
    )
