"""Logging configuration and redaction.

Credentials must never reach a log sink, and every line emitted while handling
a request must be correlatable back to that request.
"""

from __future__ import annotations

import logging

import structlog

from app.core.config import LogFormat, ObservabilitySettings
from app.core.logging import (
    _redact_sensitive,
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
    get_request_id,
    new_request_id,
)


class TestRequestContext:
    def test_new_request_id_is_unique_hex(self) -> None:
        first, second = new_request_id(), new_request_id()
        assert first != second
        assert len(first) == 32

    def test_bind_then_read(self) -> None:
        bind_request_context("abc123")
        try:
            assert get_request_id() == "abc123"
        finally:
            clear_request_context()

    def test_clear_removes_the_id(self) -> None:
        bind_request_context("abc123")
        clear_request_context()
        assert get_request_id() is None

    def test_unbound_context_is_empty(self) -> None:
        clear_request_context()
        assert get_request_id() is None


class TestRedaction:
    def test_top_level_secrets_are_redacted(self) -> None:
        result = _redact_sensitive(None, "info", {"password": "hunter2", "user": "tom"})
        assert result["password"] == "***"
        assert result["user"] == "tom"

    def test_nested_secrets_are_redacted(self) -> None:
        event = {"config": {"db": {"password": "hunter2"}, "host": "localhost"}}
        result = _redact_sensitive(None, "info", event)
        assert result["config"]["db"]["password"] == "***"
        assert result["config"]["host"] == "localhost"

    def test_secrets_inside_lists_are_redacted(self) -> None:
        event = {"items": [{"api_key": "sk-secret"}, {"name": "safe"}]}
        result = _redact_sensitive(None, "info", event)
        assert result["items"][0]["api_key"] == "***"
        assert result["items"][1]["name"] == "safe"

    def test_key_matching_is_case_insensitive(self) -> None:
        result = _redact_sensitive(None, "info", {"Authorization": "Bearer x"})
        assert result["Authorization"] == "***"

    def test_every_known_credential_key_is_covered(self) -> None:
        event = {
            "password": "a",
            "api_key": "b",
            "token": "c",
            "secret": "d",
            "openai_api_key": "e",
            "anthropic_api_key": "f",
            "admin_api_key": "g",
        }
        result = _redact_sensitive(None, "info", event)
        assert set(result.values()) == {"***"}

    def test_deep_recursion_is_bounded(self) -> None:
        # A cyclic-looking structure must not hang the logger.
        event: dict[str, object] = {"level": {}}
        cursor = event["level"]
        for _ in range(20):
            assert isinstance(cursor, dict)
            cursor["level"] = {}
            cursor = cursor["level"]
        _redact_sensitive(None, "info", event)


class TestConfigureLogging:
    def test_json_format_installs_a_single_handler(self) -> None:
        configure_logging(ObservabilitySettings(log_format=LogFormat.JSON))
        assert len(logging.getLogger().handlers) == 1

    def test_console_format_configures_cleanly(self) -> None:
        configure_logging(ObservabilitySettings(log_format=LogFormat.CONSOLE))
        assert structlog.is_configured()

    def test_level_is_applied(self) -> None:
        configure_logging(ObservabilitySettings(log_level="ERROR"))
        assert logging.getLogger().level == logging.ERROR

    def test_reconfiguring_does_not_duplicate_handlers(self) -> None:
        settings = ObservabilitySettings(log_format=LogFormat.JSON)
        configure_logging(settings)
        configure_logging(settings)
        assert len(logging.getLogger().handlers) == 1

    def test_sql_logging_toggle(self) -> None:
        configure_logging(ObservabilitySettings(log_sql=True))
        assert logging.getLogger("sqlalchemy.engine").level == logging.INFO

        configure_logging(ObservabilitySettings(log_sql=False))
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING

    def test_access_logs_are_suppressed(self) -> None:
        # The request middleware emits a richer, correlated equivalent.
        configure_logging(ObservabilitySettings())
        assert logging.getLogger("uvicorn.access").level == logging.WARNING


class TestGetLogger:
    def test_returns_a_bound_logger(self) -> None:
        configure_logging(ObservabilitySettings())
        assert hasattr(get_logger(__name__), "info")

    def test_accepts_no_name(self) -> None:
        configure_logging(ObservabilitySettings())
        assert get_logger() is not None
