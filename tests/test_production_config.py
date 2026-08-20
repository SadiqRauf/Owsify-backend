"""The configuration guard that stops the app serving production traffic unsafely."""

import pytest

from app.core.config import DEV_SECRET_KEY, Settings

REAL_SECRET = "z" * 64


def production(**overrides) -> Settings:
    base = {
        "ENVIRONMENT": "production",
        "SECRET_KEY": REAL_SECRET,
        "DEBUG": False,
        "CORS_ORIGINS": "https://app.example.com",
    }
    return Settings(**(base | overrides))


class TestProductionGuard:
    def test_a_sound_production_config_is_accepted(self) -> None:
        settings = production()
        assert settings.is_production is True

    def test_the_development_secret_is_refused(self) -> None:
        with pytest.raises(ValueError, match="SECRET_KEY"):
            production(SECRET_KEY=DEV_SECRET_KEY)

    def test_a_short_secret_is_refused(self) -> None:
        with pytest.raises(ValueError, match="SECRET_KEY"):
            production(SECRET_KEY="tooshort")

    def test_debug_is_refused(self) -> None:
        with pytest.raises(ValueError, match="DEBUG"):
            production(DEBUG=True)

    def test_wildcard_cors_is_refused(self) -> None:
        with pytest.raises(ValueError, match="CORS_ORIGINS"):
            production(CORS_ORIGINS="*")

    def test_development_is_left_alone(self) -> None:
        """The same values that fail in production must still work locally."""
        settings = Settings(ENVIRONMENT="development", SECRET_KEY=DEV_SECRET_KEY, DEBUG=True)
        assert settings.is_production is False
        assert settings.DEBUG is True


class TestDocsExposure:
    def test_docs_are_disabled_in_production(self) -> None:
        assert production().is_production is True

    def test_docs_are_available_in_development(self) -> None:
        assert Settings(ENVIRONMENT="development").is_production is False
