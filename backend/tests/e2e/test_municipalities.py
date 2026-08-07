"""Coverage endpoint behaviour.

The property under test is that coverage reflects the database rather than a
maintained list. A municipality with no indexed in-force document must come back
unavailable, however prominently it features in the product plan.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.deps import get_db


class _Row:
    """Stands in for a SQLAlchemy result row."""

    def __init__(self, slug: str, documents: int) -> None:
        self.slug = slug
        self.documents = documents


class _FakeSession:
    """Returns fixed document counts without touching Postgres."""

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    async def execute(self, *_args: Any, **_kwargs: Any) -> list[_Row]:
        return [_Row(slug, count) for slug, count in self.counts.items()]


@pytest.fixture
def coverage_client(app: FastAPI, client: AsyncClient) -> AsyncClient:
    """Client whose database reports Vancouver, Burnaby and Surrey indexed."""
    counts = {"vancouver": 1, "burnaby": 1, "surrey": 2}
    app.dependency_overrides[get_db] = lambda: _FakeSession(counts)
    return client


class TestCoverage:
    async def test_reports_indexed_municipalities_as_available(
        self, coverage_client: AsyncClient
    ) -> None:
        response = await coverage_client.get("/api/v1/municipalities")
        assert response.status_code == 200

        bc = _province(response.json(), "BC")
        available = {
            item["slug"] for item in bc["municipalities"] if item["available"]
        }
        assert available == {"vancouver", "burnaby", "surrey"}

    async def test_uningested_municipality_is_not_available(
        self, coverage_client: AsyncClient
    ) -> None:
        """Coquitlam is catalogued but has no documents.

        Reporting it as available would invite a question the corpus cannot
        answer, and the user would read the abstention as the bylaw being
        silent rather than as the bylaw being absent.
        """
        response = await coverage_client.get("/api/v1/municipalities")
        coquitlam = _municipality(response.json(), "BC", "coquitlam")

        assert coquitlam["available"] is False
        assert coquitlam["document_count"] == 0

    async def test_both_langleys_appear_separately(
        self, coverage_client: AsyncClient
    ) -> None:
        """The whole point of the qualified slugs.

        A single "Langley" option would force the interface to pick a
        jurisdiction, which is the failure the domain model refuses to make.
        """
        payload = (await coverage_client.get("/api/v1/municipalities")).json()
        bc = _province(payload, "BC")
        langleys = {
            item["slug"]: item["official_name"]
            for item in bc["municipalities"]
            if item["name"] == "Langley"
        }

        assert langleys == {
            "langley-city": "City of Langley",
            "langley-township": "Township of Langley",
        }

    async def test_alberta_is_catalogued_but_unavailable(
        self, coverage_client: AsyncClient
    ) -> None:
        """Adding a province must not require touching the frontend."""
        payload = (await coverage_client.get("/api/v1/municipalities")).json()
        alberta = _province(payload, "AB")

        assert alberta["available"] is False
        assert [item["name"] for item in alberta["municipalities"]] == ["Calgary"]

    async def test_total_available_counts_municipalities_not_documents(
        self, coverage_client: AsyncClient
    ) -> None:
        # Surrey contributes 2 documents but 1 municipality.
        payload = (await coverage_client.get("/api/v1/municipalities")).json()
        assert payload["total_available"] == 3


def _province(payload: dict[str, Any], code: str) -> dict[str, Any]:
    return next(item for item in payload["provinces"] if item["code"] == code)


def _municipality(payload: dict[str, Any], code: str, slug: str) -> dict[str, Any]:
    province = _province(payload, code)
    return next(item for item in province["municipalities"] if item["slug"] == slug)
