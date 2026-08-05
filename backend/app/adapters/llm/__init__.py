"""Generation providers.

Local-first: Ollama is the default and needs no dependency beyond httpx. Hosted
providers are opt-in and unimplemented until someone asks for them —
``build_llm_provider`` is the single place that maps configuration to an
implementation, so adding one touches nothing else.
"""

from __future__ import annotations

from app.adapters.llm.base import ChatMessage, GenerationResult, LLMProviderProtocol
from app.adapters.llm.ollama import OllamaProvider
from app.core.config import LLMProvider, LLMSettings
from app.core.exceptions import ConfigurationError

__all__ = [
    "ChatMessage",
    "GenerationResult",
    "LLMProviderProtocol",
    "OllamaProvider",
    "build_llm_provider",
]


def build_llm_provider(settings: LLMSettings) -> LLMProviderProtocol:
    """Construct the configured provider.

    Raises:
        ConfigurationError: The configured provider has no implementation.
    """
    if settings.provider is LLMProvider.OLLAMA:
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            timeout_s=settings.request_timeout_s,
            max_retries=settings.max_retries,
        )

    raise ConfigurationError(
        f"LLM__PROVIDER={settings.provider.value} has no implementation. "
        "This deployment is local-first; use LLM__PROVIDER=ollama."
    )
