"""Client API key authentication.

Guards the answering routes. Not because bylaws are secret — they are published
documents — but because each question occupies the GPU for seconds, so an
unauthenticated endpoint reachable from a network is a free inference service
for whoever finds it.
"""

from __future__ import annotations

import pytest

from app.core.config import Environment
from app.core.exceptions import AuthenticationError, ConfigurationError
from app.core.security import API_KEY_HEADER, generate_admin_key, verify_api_key

KEY_A = "a" * 64
KEY_B = "b" * 64


class TestVerifyApiKey:
    def test_correct_key_passes(self, settings_factory) -> None:
        settings = settings_factory(security={"api_keys": [KEY_A]})
        verify_api_key(KEY_A, settings)

    def test_any_configured_key_passes(self, settings_factory) -> None:
        """Several keys so clients can be revoked independently.

        Rotating the frontend's key should not take a mobile client offline.
        """
        settings = settings_factory(security={"api_keys": [KEY_A, KEY_B]})
        verify_api_key(KEY_B, settings)

    def test_wrong_key_is_rejected(self, settings_factory) -> None:
        settings = settings_factory(security={"api_keys": [KEY_A]})
        with pytest.raises(AuthenticationError, match="Invalid API key"):
            verify_api_key("z" * 64, settings)

    @pytest.mark.parametrize("provided", [None, ""])
    def test_missing_key_is_rejected(self, provided: str | None, settings_factory) -> None:
        settings = settings_factory(security={"api_keys": [KEY_A]})
        with pytest.raises(AuthenticationError, match=API_KEY_HEADER):
            verify_api_key(provided, settings)


class TestUnconfigured:
    def test_local_development_is_permitted(self, settings_factory) -> None:
        """A fresh checkout has to run without ceremony."""
        settings = settings_factory(environment=Environment.LOCAL, security={})
        verify_api_key(None, settings)

    def test_staging_without_keys_refuses(self, settings_factory) -> None:
        """Anything that is not a developer's laptop must be configured.

        Failing closed matters more here than convenience: the failure mode of
        failing open is a stranger's questions billed to your GPU, discovered
        later.
        """
        settings = settings_factory(environment=Environment.STAGING, security={})
        with pytest.raises(ConfigurationError, match="SECURITY__API_KEYS"):
            verify_api_key(None, settings)


class TestConfiguration:
    def test_short_keys_are_rejected(self, settings_factory) -> None:
        with pytest.raises(ValueError, match="at least 32"):
            settings_factory(security={"api_keys": ["too-short"]})

    def test_comma_separated_env_value_is_split(self, settings_factory) -> None:
        """A .env line is a string, not JSON.

        Pydantic would otherwise demand ["a","b"] in an environment variable,
        which is awkward to write and easy to get subtly wrong.
        """
        settings = settings_factory(security={"api_keys": f"{KEY_A}, {KEY_B}"})
        assert len(settings.security.api_keys) == 2

    def test_generated_key_satisfies_validation(self, settings_factory) -> None:
        settings = settings_factory(security={"api_keys": [generate_admin_key()]})
        assert len(settings.security.api_keys) == 1

    def test_production_refuses_to_boot_without_keys(self, settings_factory) -> None:
        with pytest.raises(ValueError, match="SECURITY__API_KEYS"):
            settings_factory(
                environment=Environment.PRODUCTION,
                debug=False,
                db={"password": "a-real-password"},
                security={
                    "admin_api_key": generate_admin_key(),
                    "cors_origins": ["https://signlaw.example"],
                },
                observability={"log_format": "json"},
            )
