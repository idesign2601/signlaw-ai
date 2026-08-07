"""Dependency health checks.

Every check answers two questions: is it working, and if not, what do I type to
fix it. A health report that says "embedding model unavailable" without naming
`make fetch-models` costs someone twenty minutes.

Checks never raise. A failing dependency is a reported status, not an exception
— the point is to see the whole picture in one pass rather than stopping at the
first problem.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.logging import get_logger

__all__ = ["ComponentCheck", "ComponentStatus", "HealthReport", "HealthService"]

logger = get_logger(__name__)


class ComponentStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    """Working, but something will fail later — e.g. no documents indexed."""
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ComponentCheck:
    """One dependency's status."""

    name: str
    status: ComponentStatus
    detail: str
    remediation: str | None = None
    latency_ms: float | None = None

    @property
    def is_ok(self) -> bool:
        return self.status is ComponentStatus.OK


@dataclass
class HealthReport:
    """The whole picture."""

    components: list[ComponentCheck] = field(default_factory=list)

    @property
    def status(self) -> ComponentStatus:
        if any(c.status is ComponentStatus.UNAVAILABLE for c in self.components):
            return ComponentStatus.UNAVAILABLE
        if any(c.status is ComponentStatus.DEGRADED for c in self.components):
            return ComponentStatus.DEGRADED
        return ComponentStatus.OK

    @property
    def can_answer(self) -> bool:
        """Whether a question can be answered right now.

        Requires Postgres, an embedding model and generation. The reranker is
        an accuracy optimisation and its absence does not block answering.
        """
        required = {"postgres", "pgvector", "embedding_model", "ollama"}
        return all(check.is_ok for check in self.components if check.name in required)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "can_answer": self.can_answer,
            "components": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "detail": c.detail,
                    "remediation": c.remediation,
                    "latency_ms": c.latency_ms,
                }
                for c in self.components
            ],
        }


@dataclass
class HealthService:
    """Checks every dependency the pipeline needs."""

    settings: Settings
    engine: AsyncEngine | None = None
    embedder: object | None = None
    llm: object | None = None
    reranker: object | None = None

    async def check(self) -> HealthReport:
        """Run every check. Never raises."""
        report = HealthReport()

        db_check = await self._check_postgres()
        report.components.append(db_check)

        if db_check.is_ok:
            report.components.append(await self._check_pgvector())
            report.components.append(await self._check_corpus())
        else:
            # Downstream DB checks would only repeat the same failure.
            report.components.append(
                ComponentCheck(
                    "pgvector",
                    ComponentStatus.UNAVAILABLE,
                    "not checked: Postgres is unreachable",
                )
            )

        report.components.append(await self._check_embedding_model())
        report.components.append(await self._check_ollama())

        if self.reranker is not None:
            report.components.append(await self._check_reranker())

        logger.info("health_checked", status=report.status.value)
        return report

    # -- Postgres ------------------------------------------------------------

    async def _check_postgres(self) -> ComponentCheck:
        if self.engine is None:
            return ComponentCheck(
                "postgres",
                ComponentStatus.UNAVAILABLE,
                "no database engine configured",
            )

        started = time.perf_counter()
        try:
            async with self.engine.connect() as connection:
                version = await connection.scalar(text("SHOW server_version"))
        except Exception as exc:  # health checks report, never raise
            return ComponentCheck(
                "postgres",
                ComponentStatus.UNAVAILABLE,
                f"cannot connect to {self.settings.db.safe_url}: {exc}",
                remediation="docker compose up -d postgres",
            )

        return ComponentCheck(
            "postgres",
            ComponentStatus.OK,
            f"connected, server {version}",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def _check_pgvector(self) -> ComponentCheck:
        """Whether the extension is installed and the storage table exists."""
        assert self.engine is not None
        dimensions = self.settings.embedding.dimensions
        table = f"chunk_embedding_{dimensions}"

        try:
            async with self.engine.connect() as connection:
                installed = await connection.scalar(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                )
                if not installed:
                    return ComponentCheck(
                        "pgvector",
                        ComponentStatus.UNAVAILABLE,
                        "the 'vector' extension is not installed",
                        remediation=(
                            "use the pgvector/pgvector:pg16 image, then run `make migrate`"
                        ),
                    )

                exists = await connection.scalar(
                    text("SELECT to_regclass(:table)"), {"table": table}
                )
        except Exception as exc:
            return ComponentCheck("pgvector", ComponentStatus.UNAVAILABLE, str(exc))

        if not exists:
            return ComponentCheck(
                "pgvector",
                ComponentStatus.UNAVAILABLE,
                f"no storage table for {dimensions}-dimensional vectors",
                remediation=(
                    f"EMBEDDING__DIMENSIONS={dimensions} has no table; use a "
                    f"supported dimension or add a migration creating {table}"
                ),
            )

        return ComponentCheck("pgvector", ComponentStatus.OK, f"extension installed, {table} ready")

    async def _check_corpus(self) -> ComponentCheck:
        """Whether anything has been indexed.

        Degraded rather than unavailable: the system is working correctly, it
        just has nothing to search, which is the expected state before the first
        ingest.
        """
        assert self.engine is not None
        try:
            async with self.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT name, chunk_count FROM embedding_collection "
                            "WHERE status = 'active' LIMIT 1"
                        )
                    )
                ).first()
                documents = await connection.scalar(text("SELECT count(*) FROM document"))
                in_force = await connection.scalar(
                    text("SELECT count(*) FROM document WHERE status = 'in_force'")
                )
        except Exception as exc:
            return ComponentCheck("corpus", ComponentStatus.UNAVAILABLE, str(exc))

        if row is None:
            return ComponentCheck(
                "corpus",
                ComponentStatus.DEGRADED,
                "no active embedding collection",
                remediation="signlaw ingest <path-to-pdf>",
            )

        if not row.chunk_count:
            return ComponentCheck(
                "corpus",
                ComponentStatus.DEGRADED,
                f"collection '{row.name}' exists but holds no embeddings",
                remediation="signlaw ingest <path-to-pdf>",
            )

        if documents and not in_force:
            # The silent failure this check exists to catch. Chunks are indexed
            # and embedded, and retrieval filters on status = 'in_force', so
            # every question returns nothing — reported downstream as "found
            # only superseded or repealed text", which reads like a corpus
            # problem rather than a broken pipeline.
            return ComponentCheck(
                "corpus",
                ComponentStatus.DEGRADED,
                (
                    f"{documents} document(s) indexed but none are in force; "
                    "retrieval will return nothing"
                ),
                remediation=(
                    "the lineage pass could not establish currency — check that "
                    "municipality and bylaw number were detected, then re-run "
                    "`signlaw ingest <path> --force`"
                ),
            )

        return ComponentCheck(
            "corpus",
            ComponentStatus.OK,
            (
                f"{documents} document(s), {in_force} in force, "
                f"{row.chunk_count} chunks in '{row.name}'"
            ),
        )

    # -- models --------------------------------------------------------------

    async def _check_embedding_model(self) -> ComponentCheck:
        """Whether the embedding model loads and produces the expected width."""
        if self.embedder is None:
            return ComponentCheck(
                "embedding_model",
                ComponentStatus.UNAVAILABLE,
                "no embedding provider configured",
            )

        started = time.perf_counter()
        try:
            ok, detail = await self.embedder.health()  # type: ignore[attr-defined]
        except Exception as exc:
            ok, detail = False, str(exc)

        latency = round((time.perf_counter() - started) * 1000, 2)

        if not ok:
            return ComponentCheck(
                "embedding_model",
                ComponentStatus.UNAVAILABLE,
                detail,
                remediation="make fetch-models",
                latency_ms=latency,
            )

        return ComponentCheck("embedding_model", ComponentStatus.OK, detail, latency_ms=latency)

    async def _check_ollama(self) -> ComponentCheck:
        """Whether Ollama is reachable and the configured model is pulled."""
        if self.llm is None:
            return ComponentCheck(
                "ollama", ComponentStatus.UNAVAILABLE, "no LLM provider configured"
            )

        started = time.perf_counter()
        try:
            ok, detail = await self.llm.health()  # type: ignore[attr-defined]
        except Exception as exc:
            ok, detail = False, str(exc)

        latency = round((time.perf_counter() - started) * 1000, 2)

        if not ok:
            remediation = (
                f"ollama pull {self.settings.llm.model}"
                if "not pulled" in detail
                else "ollama serve"
            )
            return ComponentCheck(
                "ollama",
                ComponentStatus.UNAVAILABLE,
                detail,
                remediation=remediation,
                latency_ms=latency,
            )

        return ComponentCheck("ollama", ComponentStatus.OK, detail, latency_ms=latency)

    async def _check_reranker(self) -> ComponentCheck:
        """Reranking is an optimisation, so its absence is degraded, not down."""
        try:
            ok, detail = await self.reranker.health()  # type: ignore[attr-defined]
        except Exception as exc:
            ok, detail = False, str(exc)

        if not ok:
            return ComponentCheck(
                "reranker",
                ComponentStatus.DEGRADED,
                f"{detail} — answers will use fused ranking only",
                remediation="make fetch-models",
            )
        return ComponentCheck("reranker", ComponentStatus.OK, detail)


def render_report(report: HealthReport, *, colour: bool = True) -> str:
    """Console rendering of a health report."""
    symbols = {
        ComponentStatus.OK: ("ok  ", "\033[32m"),
        ComponentStatus.DEGRADED: ("warn", "\033[33m"),
        ComponentStatus.UNAVAILABLE: ("down", "\033[31m"),
    }
    reset = "\033[0m" if colour else ""

    lines = ["SignLaw AI — health", "=" * 60]
    for check in report.components:
        label, code = symbols[check.status]
        prefix = code if colour else ""
        lines.append(f"  {prefix}{label}{reset}  {check.name:<18} {check.detail}")
        if check.remediation:
            lines.append(f"        {'':<18} fix: {check.remediation}")

    lines.append("=" * 60)
    lines.append(
        "Ready to answer questions."
        if report.can_answer
        else "NOT ready: fix the components above."
    )
    return "\n".join(lines)


def required_components() -> Sequence[str]:
    return ("postgres", "pgvector", "embedding_model", "ollama")
