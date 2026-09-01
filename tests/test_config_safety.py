from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_debug_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.debug is False


def test_settings_reject_debug_in_production() -> None:
    with pytest.raises(ValidationError, match="DEBUG must be disabled in production"):
        Settings(_env_file=None, environment="production", debug=True)


def test_settings_allow_debug_in_development() -> None:
    settings = Settings(_env_file=None, environment="development", debug=True)

    assert settings.debug is True
