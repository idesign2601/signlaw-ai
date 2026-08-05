"""Domain exception hierarchy.

Every error the application raises deliberately derives from :class:`SignLawError`,
which carries a stable machine-readable ``code``, an HTTP ``status_code``, and a
``details`` mapping. The API layer turns these into RFC 7807 problem responses
without needing to know anything about where they came from.

The ``code`` is part of the API contract: clients may branch on it, so codes are
never renamed once released.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "ConflictError",
    "DocumentProcessingError",
    "EmbeddingError",
    "ExternalServiceError",
    "IndexNotReadyError",
    "IngestionError",
    "LLMError",
    "LLMTimeoutError",
    "MetadataDetectionError",
    "NotFoundError",
    "OCRError",
    "PDFExtractionError",
    "RateLimitError",
    "RetrievalError",
    "SignLawError",
    "UnsupportedFileTypeError",
    "ValidationError",
    "VectorStoreError",
]


class SignLawError(Exception):
    """Base class for all application errors."""

    code: str = "internal_error"
    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    # Human-facing default. Subclasses override; instances may override again.
    default_message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] = details or {}
        self.cause = cause
        super().__init__(self.message)
        if cause is not None:
            self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        """Serialise for logging and API responses."""
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "status": self.status_code,
        }
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# -----------------------------------------------------------------------------
# Configuration and startup
# -----------------------------------------------------------------------------


class ConfigurationError(SignLawError):
    """Invalid or missing configuration. Raised at boot, never per-request."""

    code = "configuration_error"
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message = "The application is misconfigured."


# -----------------------------------------------------------------------------
# Client-facing request errors
# -----------------------------------------------------------------------------


class ValidationError(SignLawError):
    """The request was well-formed but semantically invalid."""

    code = "validation_error"
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    default_message = "The request was invalid."


class NotFoundError(SignLawError):
    """A requested resource does not exist."""

    code = "not_found"
    status_code = HTTPStatus.NOT_FOUND
    default_message = "The requested resource was not found."

    def __init__(
        self,
        resource: str,
        identifier: str | int | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        message = (
            f"{resource} '{identifier}' was not found."
            if identifier is not None
            else f"{resource} was not found."
        )
        merged = {"resource": resource, **(details or {})}
        if identifier is not None:
            merged["identifier"] = str(identifier)
        super().__init__(message, details=merged)


class ConflictError(SignLawError):
    """The request conflicts with current state."""

    code = "conflict"
    status_code = HTTPStatus.CONFLICT
    default_message = "The request conflicts with the current state of the resource."


class AuthenticationError(SignLawError):
    """Missing or invalid credentials."""

    code = "authentication_required"
    status_code = HTTPStatus.UNAUTHORIZED
    default_message = "Valid credentials are required."


class AuthorizationError(SignLawError):
    """Authenticated, but not permitted."""

    code = "forbidden"
    status_code = HTTPStatus.FORBIDDEN
    default_message = "You do not have permission to perform this action."


class RateLimitError(SignLawError):
    """Too many requests."""

    code = "rate_limited"
    status_code = HTTPStatus.TOO_MANY_REQUESTS
    default_message = "Rate limit exceeded. Please retry shortly."

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after_s: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        if retry_after_s is not None:
            merged["retry_after_s"] = retry_after_s
        self.retry_after_s = retry_after_s
        super().__init__(message, details=merged)


# -----------------------------------------------------------------------------
# Ingestion
# -----------------------------------------------------------------------------


class IngestionError(SignLawError):
    """Base class for ingestion pipeline failures."""

    code = "ingestion_error"
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message = "The document could not be ingested."


class DocumentProcessingError(IngestionError):
    """A specific document failed to process.

    Carries the filename so the ingestion job can record a per-document failure
    and continue with the rest of the corpus.
    """

    code = "document_processing_error"
    default_message = "The document could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        filename: str | None = None,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        merged = dict(details or {})
        if filename is not None:
            merged["filename"] = filename
        if stage is not None:
            merged["stage"] = stage
        self.filename = filename
        self.stage = stage
        super().__init__(message, details=merged, cause=cause)


class UnsupportedFileTypeError(IngestionError):
    """The file is not a PDF, or is not a readable one."""

    code = "unsupported_file_type"
    status_code = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    default_message = "Only PDF documents are supported."


class PDFExtractionError(DocumentProcessingError):
    """Text or layout extraction failed."""

    code = "pdf_extraction_error"
    default_message = "Text could not be extracted from the PDF."


class OCRError(DocumentProcessingError):
    """The OCR fallback failed."""

    code = "ocr_error"
    default_message = "OCR failed for the scanned document."


class MetadataDetectionError(DocumentProcessingError):
    """City, title, bylaw number or year could not be determined."""

    code = "metadata_detection_error"
    default_message = "Document metadata could not be determined."


# -----------------------------------------------------------------------------
# Index and retrieval
# -----------------------------------------------------------------------------


class VectorStoreError(SignLawError):
    """The vector store rejected an operation or is unreachable."""

    code = "vector_store_error"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The vector store is unavailable."


class IndexNotReadyError(SignLawError):
    """A query arrived before any documents were indexed."""

    code = "index_not_ready"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = (
        "No bylaws have been indexed yet. Run an ingestion job before asking questions."
    )


class EmbeddingError(SignLawError):
    """The embedding model failed."""

    code = "embedding_error"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "Embeddings could not be generated."


class RetrievalError(SignLawError):
    """Retrieval failed for a reason other than an empty index."""

    code = "retrieval_error"
    status_code = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message = "Retrieval failed."


# -----------------------------------------------------------------------------
# Generation
# -----------------------------------------------------------------------------


class LLMError(SignLawError):
    """The generation provider returned an error."""

    code = "llm_error"
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The language model is unavailable."


class LLMTimeoutError(LLMError):
    """The generation provider did not respond in time."""

    code = "llm_timeout"
    status_code = HTTPStatus.GATEWAY_TIMEOUT
    default_message = "The language model timed out."


class ExternalServiceError(SignLawError):
    """A dependency the application does not own failed."""

    code = "external_service_error"
    status_code = HTTPStatus.BAD_GATEWAY
    default_message = "An upstream service failed."

    def __init__(
        self,
        service: str,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.service = service
        merged = {"service": service, **(details or {})}
        super().__init__(message or f"Upstream service '{service}' failed.", details=merged, cause=cause)
