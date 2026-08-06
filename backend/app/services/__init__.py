"""Orchestration layer.

Services compose domain logic, adapters and repositories into complete
workflows. Nothing here imports FastAPI: the same service objects are driven by
tests, the CLI, and (from Phase 5) HTTP handlers.
"""

from app.services.rag_service import (
    AnswerOutcome,
    AnswerResult,
    PipelineTrace,
    RagService,
)
from app.services.trace_store import DatabaseTraceSink, NullTraceSink

__all__ = [
    "AnswerOutcome",
    "AnswerResult",
    "DatabaseTraceSink",
    "NullTraceSink",
    "PipelineTrace",
    "RagService",
]
