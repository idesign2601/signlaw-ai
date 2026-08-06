"""Structured logging.

structlog renders human-readable output locally and JSON in production. A
request-scoped correlation ID is bound into a context variable so every log line
emitted while handling a request carries it, without threading a logger through
call signatures.

For a system that produces legal citations, the retrieval trace attached to each
answer is an audit record. Logs are the second half of that: they must be
machine-parseable and correlatable.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Callable, MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import Processor

from app.core.config import LogFormat, ObservabilitySettings

__all__ = [
    "bind_request_context",
    "clear_request_context",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "new_request_id",
    "request_id_var",
]

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Keys scrubbed from log output wherever they appear.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "api_key",
        "apikey",
        "authorization",
        "token",
        "secret",
        "openai_api_key",
        "anthropic_api_key",
        "admin_api_key",
        "x-admin-key",
    }
)

_REDACTED = "***"


def new_request_id() -> str:
    """Generate a fresh correlation ID."""
    return uuid.uuid4().hex


def get_request_id() -> str | None:
    """Return the correlation ID bound to the current context, if any."""
    return request_id_var.get()


def bind_request_context(request_id: str, **extra: Any) -> None:
    """Bind a correlation ID and any extra fields for the current context."""
    request_id_var.set(request_id)
    structlog.contextvars.bind_contextvars(request_id=request_id, **extra)


def clear_request_context() -> None:
    """Clear all context-local log bindings."""
    request_id_var.set(None)
    structlog.contextvars.clear_contextvars()


def _add_request_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach the correlation ID if one is bound and not already present."""
    if "request_id" not in event_dict:
        request_id = request_id_var.get()
        if request_id is not None:
            event_dict["request_id"] = request_id
    return event_dict


def _redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Redact credential-shaped values anywhere in the event."""

    def scrub(value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return value
        if isinstance(value, dict):
            return {
                k: (_REDACTED if str(k).lower() in _SENSITIVE_KEYS else scrub(v, depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [scrub(item, depth + 1) for item in value]
        if isinstance(value, tuple):
            return tuple(scrub(item, depth + 1) for item in value)
        return value

    for key in list(event_dict.keys()):
        if str(key).lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = scrub(event_dict[key])
    return event_dict


def _drop_color_message(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Remove uvicorn's duplicate pre-coloured message."""
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(settings: ObservabilitySettings) -> None:
    """Configure structlog and route the stdlib logging tree through it.

    Safe to call more than once; later calls replace the configuration.
    """
    level = getattr(logging, settings.log_level, logging.INFO)
    use_json = settings.log_format is LogFormat.JSON

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _drop_color_message,
        _redact_sensitive,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ConsoleRenderer formats tracebacks itself; JSONRenderer needs exc_info
    # flattened into a string first, so format_exc_info is added only there.
    render_chain: list[Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if use_json:
        render_chain.append(structlog.processors.format_exc_info)
    render_chain.append(renderer)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=render_chain,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party loggers: propagate through our handler, no duplicate output.
    for name, lib_level in _library_log_levels(settings).items():
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
        lib_logger.setLevel(lib_level)


def _library_log_levels(settings: ObservabilitySettings) -> dict[str, int]:
    """Per-library log levels.

    Access logs are suppressed because the request-logging middleware emits a
    richer, correlated equivalent.
    """
    sql_level = logging.INFO if settings.log_sql else logging.WARNING
    return {
        "uvicorn": logging.INFO,
        "uvicorn.error": logging.INFO,
        "uvicorn.access": logging.WARNING,
        "sqlalchemy.engine": sql_level,
        "sqlalchemy.pool": logging.WARNING,
        "alembic": logging.INFO,
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
        "chromadb": logging.WARNING,
        "urllib3": logging.WARNING,
        "asyncio": logging.WARNING,
        "arq": logging.INFO,
    }


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger.

    Callers normally pass ``__name__``.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


# Convenience alias for modules that prefer a factory-style import.
LoggerFactory: Callable[[str | None], structlog.stdlib.BoundLogger] = get_logger
