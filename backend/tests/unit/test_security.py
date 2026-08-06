"""Admin authentication.

Admin routes re-index and delete documents. These tests pin the three cases that
matter: a correct key passes, a wrong or missing key fails, and an unconfigured
key is tolerated only in local development.
"""

from __future__ import annotations

import pytest

from app.core.config import Environment
from app.core.exceptions import AuthenticationError, ConfigurationError
from app.core.security import ADMIN_KEY_HEADER, generate_admin_key, verify_admin_key

VALID_KEY = "k" * 64


class TestGenerateAdminKey:
    def test_length_is_64_hex_characters(self) -> None:
        key = generate_admin_key()
        assert len(key) == 64
        assert all(char in "0123456789abcdef" for char in key)

    def test_keys_are_unique(self) -> None:
        assert generate_admin_key() != generate_admin_key()

    def test_generated_key_satisfies_settings_validation(self, settings_factory) -> None:
        settings = settings_factory(security={"admin_api_key": generate_admin_key()})
        assert settings.security.admin_api_key is not None


class TestVerifyAdminKey:
    def test_correct_key_passes(self, settings_factory) -> None:
        settings = settings_factory(security={"admin_api_key": VALID_KEY})
        verify_admin_key(VALID_KEY, settings)

    def test_wrong_key_is_rejected(self, settings_factory) -> None:
        settings = settings_factory(security={"admin_api_key": VALID_KEY})
        with pytest.raises(AuthenticationError, match="Invalid admin key"):
            verify_admin_key("x" * 64, settings)

    @pytest.mark.parametrize("provided", [None, ""])
    def test_missing_key_is_rejected(self, provided: str | None, settings_factory) -> None:
        settings = settings_factory(security={"admin_api_key": VALID_KEY})
        with pytest.raises(AuthenticationError, match="Missing"):
            verify_admin_key(provided, settings)

    def test_rejection_names_the_expected_header(self, settings_factory) -> None:
        settings = settings_factory(security={"admin_api_key": VALID_KEY})
        with pytest.raises(AuthenticationError) as exc_info:
            verify_admin_key(None, settings)
        assert exc_info.value.details["header"] == ADMIN_KEY_HEADER

    def test_near_miss_key_is_rejected(self, settings_factory) -> None:
        settings = settings_factory(security={"admin_api_key": VALID_KEY})
        with pytest.raises(AuthenticationError):
            verify_admin_key(VALID_KEY[:-1] + "j", settings)


class TestUnconfiguredKey:
    def test_local_environment_allows_access(self, settings_factory) -> None:
        # A fresh checkout must run without ceremony.
        settings = settings_factory(environment=Environment.LOCAL)
        verify_admin_key(None, settings)

    @pytest.mark.parametrize(
        "environment", [Environment.DEV, Environment.STAGING]
    )
    def test_other_environments_refuse_access(
        self, environment: Environment, settings_factory
    ) -> None:
        settings = settings_factory(environment=environment)
        with pytest.raises(ConfigurationError, match="ADMIN_API_KEY"):
            verify_admin_key(None, settings)

    def test_production_cannot_reach_this_state(self, settings_factory) -> None:
        # Settings validation refuses to build a keyless production config at
        # all, so the route guard never has to handle that case.
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="ADMIN_API_KEY"):
            settings_factory(
                environment=Environment.PRODUCTION,
                db={"password": "a-real-production-password"},
                observability={"log_format": "json"},
                security={"cors_origins": ["https://app.example.com"]},
            )
