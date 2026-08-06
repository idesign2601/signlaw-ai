"""Generation provider interface.

A Protocol, so a provider only has to satisfy the shape. Nothing above this
layer imports a vendor SDK or knows which model produced an answer.

Two capabilities matter for this system beyond plain completion:

* **Schema-constrained output.** The answer contract is structured — prose plus
  an array of citations. Parsing that out of free text is unreliable, and a
  malformed citation block is indistinguishable from a fabricated one. Providers
  that can constrain decoding to a JSON schema should.
* **Reported token usage.** Cost and latency per answer are operational
  necessities, and usage is also a signal when an answer is suspiciously long.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["ChatMessage", "GenerationResult", "LLMProviderProtocol"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn in a prompt."""

    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """A completion plus the metadata needed to audit and cost it."""

    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens

    @property
    def was_truncated(self) -> bool:
        """Whether generation stopped at the token limit.

        A truncated answer may have lost its citation block entirely, so the
        caller must treat it as unverifiable rather than partially valid.
        """
        return self.finish_reason == "length"


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """Generates text from a prompt."""

    @property
    def model(self) -> str:
        """Model identifier, recorded against every answer."""
        ...

    @property
    def supports_schema(self) -> bool:
        """Whether decoding can be constrained to a JSON schema."""
        ...

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> GenerationResult:
        """Produce a completion, optionally constrained to ``schema``."""
        ...

    # Deliberately `def`, not `async def`. Implementations are async generators,
    # which return an AsyncIterator directly; declaring this `async def` would
    # demand a Coroutine wrapping an AsyncIterator and no generator could
    # satisfy the Protocol.
    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield the completion incrementally."""
        ...

    async def health(self) -> tuple[bool, str]:
        """Whether the provider can serve requests, and why not if it cannot."""
        ...
