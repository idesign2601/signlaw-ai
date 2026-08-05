"""Domain exception contract.

Error ``code`` values are part of the public API — clients branch on them — so
they are asserted explicitly here. A rename should break this suite loudly.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest

from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ConflictError,
    DocumentProcessingError,
    EmbeddingError,
    ExternalServiceError,
    IndexNotReadyError,
    IngestionError,
    LLMError,
    LLMTimeoutError,
    MetadataDetectionError,
    NotFoundError,
    OCRError,
    PDFExtractionError,
    RateLimitError,
    RetrievalError,
    SignLawError,
    UnsupportedFileTypeError,
    ValidationError,
    VectorStoreError,
)


class TestBaseBehaviour:
    def test_default_message_is_used(self) -> None:
        assert SignLawError().message == SignLawError.default_message

    def test_explicit_message_wins(self) -> None:
        assert SignLawError("boom").message == "boom"

    def test_to_dict_shape(self) -> None:
        error = SignLawError("boom", details={"key": "value"})
        assert error.to_dict() == {
            "code": "internal_error",
            "message": "boom",
            "status": HTTPStatus.INTERNAL_SERVER_ERROR,
            "details": {"key": "value"},
        }

    def test_to_dict_omits_empty_details(self) -> None:
        assert "details" not in SignLawError("boom").to_dict()

    def test_cause_is_chained(self) -> None:
        cause = ValueError("root cause")
        error = SignLawError("wrapper", cause=cause)
        assert error.__cause__ is cause
        assert error.cause is cause

    def test_is_an_exception(self) -> None:
        with pytest.raises(SignLawError):
            raise SignLawError("boom")

    def test_repr_is_informative(self) -> None:
        assert "internal_error" in repr(SignLawError("boom"))


class TestErrorCodesAndStatuses:
    @pytest.mark.parametrize(
        ("error", "expected_code", "expected_status"),
        [
            (ConfigurationError(), "configuration_error", 500),
            (ValidationError(), "validation_error", 422),
            (ConflictError(), "conflict", 409),
            (AuthenticationError(), "authentication_required", 401),
            (AuthorizationError(), "forbidden", 403),
            (RateLimitError(), "rate_limited", 429),
            (IngestionError(), "ingestion_error", 500),
            (UnsupportedFileTypeError(), "unsupported_file_type", 415),
            (VectorStoreError(), "vector_store_error", 503),
            (IndexNotReadyError(), "index_not_ready", 503),
            (EmbeddingError(), "embedding_error", 503),
            (RetrievalError(), "retrieval_error", 500),
            (LLMError(), "llm_error", 503),
            (LLMTimeoutError(), "llm_timeout", 504),
        ],
    )
    def test_code_and_status(
        self, error: SignLawError, expected_code: str, expected_status: int
    ) -> None:
        assert error.code == expected_code
        assert error.status_code == expected_status

    def test_every_error_derives_from_base(self) -> None:
        for error_cls in (
            ConfigurationError,
            ValidationError,
            NotFoundError,
            ConflictError,
            AuthenticationError,
            AuthorizationError,
            RateLimitError,
            IngestionError,
            DocumentProcessingError,
            VectorStoreError,
            LLMError,
            ExternalServiceError,
        ):
            assert issubclass(error_cls, SignLawError)


class TestNotFoundError:
    def test_message_includes_resource_and_identifier(self) -> None:
        error = NotFoundError("Document", "abc-123")
        assert error.message == "Document 'abc-123' was not found."
        assert error.details == {"resource": "Document", "identifier": "abc-123"}

    def test_identifier_is_optional(self) -> None:
        error = NotFoundError("Municipality")
        assert error.message == "Municipality was not found."
        assert "identifier" not in error.details

    def test_status_is_404(self) -> None:
        assert NotFoundError("Document").status_code == 404


class TestRateLimitError:
    def test_retry_after_is_exposed_and_recorded(self) -> None:
        error = RateLimitError(retry_after_s=30)
        assert error.retry_after_s == 30
        assert error.details["retry_after_s"] == 30

    def test_retry_after_is_optional(self) -> None:
        assert RateLimitError().retry_after_s is None


class TestDocumentProcessingError:
    """Per-document failures must carry enough context for the job to record
    which file failed at which stage, then continue with the rest of the corpus."""

    def test_filename_and_stage_are_recorded(self) -> None:
        error = DocumentProcessingError(
            "extraction failed", filename="coquitlam_sign_bylaw.pdf", stage="pdf_extract"
        )
        assert error.filename == "coquitlam_sign_bylaw.pdf"
        assert error.stage == "pdf_extract"
        assert error.details == {
            "filename": "coquitlam_sign_bylaw.pdf",
            "stage": "pdf_extract",
        }

    def test_context_is_optional(self) -> None:
        error = DocumentProcessingError("failed")
        assert error.filename is None
        assert error.details == {}

    @pytest.mark.parametrize(
        ("error_cls", "expected_code"),
        [
            (PDFExtractionError, "pdf_extraction_error"),
            (OCRError, "ocr_error"),
            (MetadataDetectionError, "metadata_detection_error"),
        ],
    )
    def test_subclasses_keep_the_context_constructor(
        self, error_cls: type[DocumentProcessingError], expected_code: str
    ) -> None:
        error = error_cls("failed", filename="a.pdf", stage="s")
        assert error.code == expected_code
        assert error.filename == "a.pdf"
        assert isinstance(error, IngestionError)


class TestExternalServiceError:
    def test_service_is_recorded(self) -> None:
        error = ExternalServiceError("ollama")
        assert error.service == "ollama"
        assert error.details["service"] == "ollama"
        assert "ollama" in error.message

    def test_custom_message_is_kept(self) -> None:
        error = ExternalServiceError("chroma", "connection refused")
        assert error.message == "connection refused"
        assert error.details["service"] == "chroma"


class TestIndexNotReadyError:
    def test_message_tells_the_operator_what_to_do(self) -> None:
        # The most likely first-run failure; the message must be actionable.
        assert "ingestion" in IndexNotReadyError().message.lower()
