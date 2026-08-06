"""Exception handlers producing RFC 7807 ``application/problem+json`` responses.

One error shape across the whole API, whatever raised it. Every response carries
the correlation ID so a user-reported failure maps to a log line.

Unexpected exceptions are logged with a traceback but never leak internals to
the client outside debug mode.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.exceptions import RateLimitError, SignLawError
from app.core.logging import get_logger, get_request_id

__all__ = ["problem_response", "register_exception_handlers"]

logger = get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"


def problem_response(
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    instance: str | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    """Build an RFC 7807 problem response."""
    body: dict[str, Any] = {
        "type": f"https://signlaw.ai/errors/{code}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "code": code,
    }
    if instance:
        body["instance"] = instance
    resolved_request_id = request_id or get_request_id()
    if resolved_request_id:
        body["request_id"] = resolved_request_id
    if extra:
        body.update(extra)

    response_headers = dict(headers or {})
    if resolved_request_id:
        response_headers.setdefault("X-Request-ID", resolved_request_id)

    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=response_headers,
    )


def _request_id_for(request: Request) -> str | None:
    """Resolve the correlation ID.

    Unhandled exceptions are turned into responses by Starlette's outermost
    error middleware, which runs *after* the request-context middleware has
    cleared its context variables. ``request.state`` survives that teardown, so
    it is consulted first and the context variable is the fallback.
    """
    state_id: str | None = getattr(request.state, "request_id", None)
    return state_id or get_request_id()


def _title_for(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


async def _handle_signlaw_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, SignLawError)

    # 5xx means we broke; 4xx means the caller did. Log accordingly.
    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.error(
            "request_failed",
            error_code=exc.code,
            error_message=exc.message,
            details=exc.details,
            path=request.url.path,
            exc_info=exc,
        )
    else:
        logger.info(
            "request_rejected",
            error_code=exc.code,
            error_message=exc.message,
            details=exc.details,
            path=request.url.path,
        )

    headers: dict[str, str] | None = None
    if isinstance(exc, RateLimitError) and exc.retry_after_s is not None:
        headers = {"Retry-After": str(exc.retry_after_s)}

    return problem_response(
        status_code=exc.status_code,
        code=exc.code,
        title=_title_for(exc.status_code),
        detail=exc.message,
        instance=request.url.path,
        extra={"details": exc.details} if exc.details else None,
        headers=headers,
        request_id=_request_id_for(request),
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)

    errors = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "message": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]
    logger.info("request_validation_failed", path=request.url.path, errors=errors)

    return problem_response(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        title="Unprocessable Entity",
        detail="The request payload failed validation.",
        instance=request.url.path,
        extra={"errors": errors},
        request_id=_request_id_for(request),
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)

    detail = exc.detail if isinstance(exc.detail, str) else _title_for(exc.status_code)
    headers = dict(exc.headers) if exc.headers else None

    return problem_response(
        status_code=exc.status_code,
        code=f"http_{exc.status_code}",
        title=_title_for(exc.status_code),
        detail=detail,
        instance=request.url.path,
        headers=headers,
        request_id=_request_id_for(request),
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Never leaks internals unless debug is on."""
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
    )

    settings = get_settings()
    detail = (
        f"{type(exc).__name__}: {exc}"
        if settings.debug
        else "An unexpected error occurred. The incident has been logged."
    )

    return problem_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        title="Internal Server Error",
        detail=detail,
        instance=request.url.path,
        request_id=_request_id_for(request),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler to the application."""
    app.add_exception_handler(SignLawError, _handle_signlaw_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_error)
