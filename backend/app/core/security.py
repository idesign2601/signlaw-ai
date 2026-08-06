"""Authentication for privileged routes.

Admin routes re-index and delete documents, so they are guarded from the first
commit rather than retrofitted later. Authentication is a shared secret in the
``X-Admin-Key`` header, compared in constant time.

Local development without a configured key is permitted so a fresh checkout
runs, but every unauthenticated admin call logs a warning, and
:class:`~app.core.config.Settings` refuses to boot in production without a key.
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
    "generate_admin_key",
    "require_admin",
    "verify_admin_key",
]

logger = get_logger(__name__)

ADMIN_KEY_HEADER = "X-Admin-Key"

# auto_error=False so a missing header raises our own AuthenticationError,
# keeping the RFC 7807 error contract consistent across the API.
_admin_key_scheme = APIKeyHeader(
    name=ADMIN_KEY_HEADER,
    auto_error=False,
    description="Shared secret guarding destructive admin operations.",
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
