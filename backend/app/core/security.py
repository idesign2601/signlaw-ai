"""Authentication.

Two independent secrets, because they protect against different things:

``X-Admin-Key``
    Destructive operations — re-index, delete. One key.

``X-API-Key``
    The answering routes. Not about secrecy of the bylaws, which are public
    documents, but about who gets to spend GPU time: one question occupies the
    card for seconds, so an unauthenticated ``/ask`` reachable from the internet
    is a free inference service for whoever finds it. Several keys are accepted
    so each client can be revoked without disrupting the others.

Both compare in constant time. Both are optional in local development so a
fresh checkout runs, and both are mandatory in production —
:class:`~app.core.config.Settings` refuses to boot without them.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader

from app.core.config import Environment, Settings, get_settings
from app.core.exceptions import AuthenticationError, ConfigurationError
from app.core.logging import get_logger

__all__ = [
    "ADMIN_KEY_HEADER",
    "API_KEY_HEADER",
    "generate_admin_key",
    "require_admin",
    "require_api_key",
    "verify_admin_key",
    "verify_api_key",
]

logger = get_logger(__name__)

ADMIN_KEY_HEADER = "X-Admin-Key"
API_KEY_HEADER = "X-API-Key"

# auto_error=False so a missing header raises our own AuthenticationError,
# keeping the RFC 7807 error contract consistent across the API.
_admin_key_scheme = APIKeyHeader(
    name=ADMIN_KEY_HEADER,
    auto_error=False,
    description="Shared secret guarding destructive admin operations.",
)

_api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="Shared secret required to ask questions.",
)


def generate_admin_key() -> str:
    """Generate a cryptographically secure admin key (64 hex characters)."""
    return secrets.token_hex(32)


def verify_admin_key(provided: str | None, settings: Settings) -> None:
    """Validate a presented admin key.

    Raises:
        AuthenticationError: The key is missing or wrong.
        ConfigurationError: No key is configured outside local development.
    """
    configured = settings.security.admin_api_key

    if configured is None:
        if settings.environment is Environment.LOCAL:
            logger.warning(
                "admin_route_unauthenticated",
                reason="SECURITY__ADMIN_API_KEY is not set; allowing in local environment only",
                environment=settings.environment.value,
            )
            return
        raise ConfigurationError(
            "Admin routes are disabled because SECURITY__ADMIN_API_KEY is not configured."
        )

    if not provided:
        raise AuthenticationError(
            f"Missing {ADMIN_KEY_HEADER} header.",
            details={"header": ADMIN_KEY_HEADER},
        )

    if not hmac.compare_digest(provided, configured.get_secret_value()):
        logger.warning("admin_auth_failed", reason="key_mismatch")
        raise AuthenticationError(
            "Invalid admin key.",
            details={"header": ADMIN_KEY_HEADER},
        )


async def require_admin(
    provided: str | None = Security(_admin_key_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """FastAPI dependency guarding privileged routes.

    Tests override the configuration with
    ``app.dependency_overrides[get_settings] = ...``.
    """
    verify_admin_key(provided, settings)


def verify_api_key(provided: str | None, settings: Settings) -> None:
    """Validate a presented client key against every configured key.

    Raises:
        AuthenticationError: The key is missing or matches none of the
            configured keys.
        ConfigurationError: No keys are configured outside local development.
    """
    configured = settings.security.api_keys

    if not configured:
        if settings.environment is Environment.LOCAL:
            logger.warning(
                "answering_route_unauthenticated",
                reason=(
                    "SECURITY__API_KEYS is not set; allowing in local environment "
                    "only. Set it before exposing this process to a network."
                ),
                environment=settings.environment.value,
            )
            return
        raise ConfigurationError(
            "Answering routes are disabled because SECURITY__API_KEYS is not "
            "configured. Generate one with: openssl rand -hex 32"
        )

    if not provided:
        raise AuthenticationError(
            f"Missing {API_KEY_HEADER} header.",
            details={"header": API_KEY_HEADER},
        )

    # Every key is compared even after a match, so the time taken does not
    # reveal which key matched or how many are configured.
    matched = False
    for key in configured:
        if hmac.compare_digest(provided, key.get_secret_value()):
            matched = True

    if not matched:
        logger.warning("api_auth_failed", reason="key_mismatch")
        raise AuthenticationError(
            "Invalid API key.",
            details={"header": API_KEY_HEADER},
        )


async def require_api_key(
    provided: str | None = Security(_api_key_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """FastAPI dependency guarding the answering routes."""
    verify_api_key(provided, settings)
