"""Trace persistence.

A completed :class:`~app.services.rag_service.PipelineTrace` is written to
``chat_message`` so a disputed answer can be reconstructed months later: which
chunks were retrieved, how they scored, which prompt version and model produced
the text, and what verification concluded.

Separated from the service so the pipeline runs without a database — the CLI
and unit tests pass no sink at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import ChatRole
from app.services.rag_service import PipelineTrace

__all__ = ["DatabaseTraceSink", "NullTraceSink"]

logger = get_logger(__name__)


class NullTraceSink:
    """Discards traces. Used by the CLI and by tests."""

    async def record(self, trace: PipelineTrace) -> None:
        return None


@dataclass
class DatabaseTraceSink:
    """Writes traces to ``chat_message``.

    ``session_id`` groups an ongoing conversation. When absent, a session row is
    created per answer so a one-off question is still auditable.
    """

    session: AsyncSession
    session_id: uuid.UUID | None = None

    async def record(self, trace: PipelineTrace) -> None:
        session_id = self.session_id or await self._ensure_session(trace)

        await self.session.execute(
            text(
                "INSERT INTO chat_message "
                "(id, session_id, role, content, intent, citations, confidence, "
                " confidence_band, abstained, retrieval_trace, model_used, "
                " prompt_version, latency_ms, prompt_tokens, completion_tokens) "
                "VALUES "
                "(:id, :session_id, :role, :content, "
                " CAST(NULLIF(:intent, '') AS query_intent), "
                " CAST(:citations AS jsonb), :confidence, "
                " CAST(NULLIF(:band, '') AS confidence_band), :abstained, "
                " CAST(:trace AS jsonb), :model, :prompt_version, :latency_ms, "
                " :prompt_tokens, :completion_tokens)"
            ),
            {
                "id": uuid.uuid4(),
                "session_id": session_id,
                "role": ChatRole.ASSISTANT.value,
                "content": trace.answer,
                "intent": trace.intent,
                "citations": _json(list(trace.citations)),
                "confidence": trace.confidence.get("score"),
                "band": str(trace.confidence.get("band") or ""),
                "abstained": not trace.outcome.is_answer,
                "trace": _json(trace.as_dict()),
                "model": trace.model_used or None,
                "prompt_version": trace.prompt_version,
                "latency_ms": trace.total_ms,
                "prompt_tokens": trace.prompt_tokens,
                "completion_tokens": trace.completion_tokens,
            },
        )

    async def _ensure_session(self, trace: PipelineTrace) -> uuid.UUID:
        session_id = uuid.uuid4()
        # Title from the question so the admin view is browsable.
        await self.session.execute(
            text("INSERT INTO chat_session (id, title) VALUES (:id, :title)"),
            {"id": session_id, "title": trace.question[:300]},
        )
        self.session_id = session_id
        return session_id


def _json(value: object) -> str:
    import json

    return json.dumps(value, default=str)
