"""Ollama generation provider.

Speaks Ollama's REST API over httpx rather than through the ``ollama`` Python
package. httpx is already a core dependency, so local generation adds no new
one — which is the point of a local-first deployment. The API surface used here
(``/api/chat``, ``/api/tags``, ``/api/show``) has been stable across releases.

Defaults to Qwen 2.5 Instruct. Llama, Mistral and Gemma models work unchanged:
the chat endpoint is model-agnostic and everything model-specific is
configuration.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.adapters.llm.base import ChatMessage, GenerationResult
from app.core.exceptions import ExternalServiceError, LLMError, LLMTimeoutError
from app.core.logging import get_logger

__all__ = ["OllamaProvider"]

logger = get_logger(__name__)


class OllamaProvider:
    """Local generation through an Ollama server.

    Parameters:
        base_url: Ollama's address. ``http://host.docker.internal:11434`` from
            inside a container talking to an Ollama on the host.
        model: Any model pulled into Ollama, e.g. ``qwen2.5:14b-instruct``.
        num_ctx: Context window. Bylaw answers pack five retrieved sections plus
            instructions into the prompt, which overflows the 2048-token default
            most Ollama models ship with — so it is set explicitly rather than
            left to silently truncate the evidence.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:14b-instruct",
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        num_ctx: int = 16384,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._num_ctx = num_ctx

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_schema(self) -> bool:
        """Ollama constrains decoding to a JSON schema via ``format``."""
        return True

    # -- generation ----------------------------------------------------------

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> GenerationResult:
        """Produce a completion, optionally constrained to a JSON schema."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message.as_dict() for message in messages],
            "stream": False,
            "options": {
                "temperature": (
                    self._temperature if temperature is None else temperature
                ),
                "num_predict": self._max_tokens if max_tokens is None else max_tokens,
                "num_ctx": self._num_ctx,
            },
        }
        if schema is not None:
            # Grammar-constrained decoding: the model physically cannot emit
            # text that violates the schema, which removes the whole class of
            # "answer arrived but the citation block was malformed" failures.
            payload["format"] = schema

        started = time.perf_counter()
        data = await self._post("/api/chat", payload)
        latency_ms = int((time.perf_counter() - started) * 1000)

        message = data.get("message") or {}
        text = str(message.get("content") or "")

        if not text.strip():
            raise LLMError(
                f"Model '{self._model}' returned an empty completion.",
                details={"model": self._model},
            )

        result = GenerationResult(
            text=text,
            model=self._model,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            latency_ms=latency_ms,
            finish_reason=self._finish_reason(data),
            raw={key: data.get(key) for key in ("done_reason", "total_duration")},
        )

        logger.info(
            "llm_generated",
            model=self._model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=latency_ms,
            schema_constrained=schema is not None,
        )
        return result

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield the completion incrementally.

        Used for the chat UI, where a 3-8 second answer feels far longer without
        visible progress. Never used for the structured answer path, which must
        be validated whole before any of it is shown.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message.as_dict() for message in messages],
            "stream": True,
            "options": {
                "temperature": (
                    self._temperature if temperature is None else temperature
                ),
                "num_predict": self._max_tokens if max_tokens is None else max_tokens,
                "num_ctx": self._num_ctx,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                async with client.stream(
                    "POST", f"{self._base_url}/api/chat", json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = (chunk.get("message") or {}).get("content")
                        if content:
                            yield content
                        if chunk.get("done"):
                            return
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"Ollama did not respond within {self._timeout_s:.0f}s.", cause=exc
            ) from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceError("ollama", str(exc), cause=exc) from exc

    # -- transport -----------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with bounded retries.

        Retries connection-level failures and 5xx only. A 4xx means the request
        is wrong — usually a model that was never pulled — and repeating it just
        delays a clear error.
        """
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.post(url, json=payload)

                if response.status_code == 404:
                    raise LLMError(
                        f"Model '{self._model}' is not available in Ollama. "
                        f"Pull it first: ollama pull {self._model}",
                        details={"model": self._model, "base_url": self._base_url},
                    )
                if 400 <= response.status_code < 500:
                    raise LLMError(
                        f"Ollama rejected the request ({response.status_code}): "
                        f"{response.text[:300]}",
                        details={"status": response.status_code},
                    )
                response.raise_for_status()

                parsed: dict[str, Any] = response.json()
                return parsed

            except (LLMError, LLMTimeoutError):
                raise
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise LLMTimeoutError(
                        f"Ollama did not respond within {self._timeout_s:.0f}s "
                        f"after {attempt + 1} attempts.",
                        cause=exc,
                    ) from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise ExternalServiceError(
                        "ollama",
                        f"Could not reach Ollama at {self._base_url}: {exc}. "
                        "Is `ollama serve` running?",
                        cause=exc,
                    ) from exc

            logger.warning(
                "ollama_retry",
                attempt=attempt + 1,
                max_retries=self._max_retries,
                error=str(last_error),
            )

        raise ExternalServiceError("ollama", str(last_error))

    @staticmethod
    def _finish_reason(data: dict[str, Any]) -> str | None:
        """Normalise Ollama's stop reason.

        ``length`` matters: a truncated answer may have lost its citation block,
        which makes it unverifiable rather than merely incomplete.
        """
        reason = data.get("done_reason")
        if reason == "stop":
            return "stop"
        if reason == "length":
            return "length"
        return reason

    # -- health --------------------------------------------------------------

    async def health(self) -> tuple[bool, str]:
        """Whether Ollama is reachable and the configured model is present."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            return False, f"cannot reach Ollama at {self._base_url}: {exc}"

        available = {
            str(model.get("name", "")) for model in data.get("models", [])
        }
        if not available:
            return False, "Ollama is running but no models are pulled"

        # Ollama tags default to ":latest" when a bare name is pulled.
        wanted = {self._model, f"{self._model}:latest", self._model.split(":")[0]}
        if not (wanted & available) and not any(
            name.startswith(f"{self._model.split(':')[0]}:") for name in available
        ):
            return False, (
                f"model '{self._model}' not pulled. Available: "
                f"{', '.join(sorted(available)[:5])}. Run: ollama pull {self._model}"
            )

        return True, f"{self._model} ready"
