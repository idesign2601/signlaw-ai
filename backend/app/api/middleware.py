"""HTTP middleware: correlation IDs and request logging."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    new_request_id,
)

__all__ = ["REQUEST_ID_HEADER", "RequestContextMiddleware"]

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# Health checks fire constantly; logging them buries real traffic.
_QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/favicon.ico"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a correlation ID to every request and log its outcome.

    An inbound ``X-Request-ID`` is honoured so a trace can span services; a new
    one is minted otherwise. The ID is echoed on the response and attached to
    every log line and error body produced while handling the request.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        bind_request_context(request_id)
        request.state.request_id = request_id

        quiet = request.url.path in _QUIET_PATHS
        started = time.perf_counter()

        try:
            try:
                response = await call_next(request)
            except Exception:
                # The registered exception handlers build the response body;
                # this only records timing for the failed request.
                logger.warning(
                    "request_errored",
                    method=request.method,
                    path=request.url.path,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                raise

            duration_ms = (time.perf_counter() - started) * 1000
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers["X-Response-Time-ms"] = f"{duration_ms:.2f}"

            if not quiet:
                log = logger.info if response.status_code < 500 else logger.error
                log(
                    "request_completed",
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 2),
                    client=request.client.host if request.client else None,
                )

            return response
        finally:
            clear_request_context()
