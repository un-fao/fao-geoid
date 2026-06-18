"""Unit tests for configuration derivation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.config import DatabaseSettings, Settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Integration fixtures export GEOID_* into os.environ; clear the ones these
    # derivation tests assert on so unit tests are order-independent.
    for key in (
        "GEOID_DATABASE_URL",
        "GEOID_BASE_URL",
        "GEOID_OIDC_ISSUER",
        "GEOID_OIDC_JWKS_URL",
        "GEOID_DEDUP_GRID_DEFAULT",
        "GEOID_MAX_OFFSET",
        "GEOID_ADMIN_TOKEN",
        "GEOID_ENVIRONMENT",
        "GEOID_ROOT_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> Settings:
    # _env_file=None so a developer's local .env never leaks into the assertions.
    return Settings(_env_file=None, **overrides)


def test_base_url_clean_strips_trailing_slash():
    settings = _settings(base_url="https://data.fao.org/")
    assert settings.base_url_clean == "https://data.fao.org"


def test_root_path_defaults_empty_and_leaves_base_url_clean_unchanged():
    settings = _settings(base_url="https://data.fao.org")
    assert settings.root_path == ""
    assert settings.base_url_clean == "https://data.fao.org"


@pytest.mark.parametrize("given", ["geoid/v1", "/geoid/v1", "/geoid/v1/", "  /geoid/v1  "])
def test_root_path_is_normalised_to_leading_slash_no_trailing(given):
    assert _settings(root_path=given).root_path == "/geoid/v1"


def test_base_url_clean_includes_root_path():
    # The minted URI/OGC-link prefix must carry the proxy sub-path to stay resolvable.
    settings = _settings(base_url="https://data.review.fao.org", root_path="/geoid/v1")
    assert settings.base_url_clean == "https://data.review.fao.org/geoid/v1"


def test_oidc_disabled_without_issuer():
    assert _settings().oidc_enabled is False


def test_oidc_enabled_with_issuer_and_jwks():
    settings = _settings(
        oidc_issuer="https://idp.fao.org", oidc_jwks_url="https://idp.fao.org/jwks"
    )
    assert settings.oidc_enabled is True


def test_dedup_grid_default_is_1cm():
    # Remi's exact-match ruling: 1e-7 deg/vertex ≈ 1 cm — float-jitter immunity only.
    assert _settings().dedup_grid_default == 1e-7


def test_dedup_grid_default_env_override(monkeypatch):
    monkeypatch.setenv("GEOID_DEDUP_GRID_DEFAULT", "0.0005")
    assert _settings().dedup_grid_default == 0.0005


def test_max_offset_default_and_rejects_negative():
    assert _settings().max_offset == 100_000
    with pytest.raises(ValidationError):
        _settings(max_offset=-1)


def test_bulk_max_features_default_is_1000():
    # The synchronous bulk POST is sized for hundreds-to-a-few-thousand geometries.
    assert _settings().bulk_max_features == 1_000


@pytest.mark.parametrize("environment", ["review", "production"])
def test_default_admin_token_outside_development_is_rejected(environment):
    with pytest.raises(ValidationError):
        _settings(environment=environment)


def test_default_admin_token_in_development_is_allowed():
    settings = _settings()
    assert settings.environment == "development"
    assert settings.admin_token == "change-me-dev-only"


def test_real_admin_token_in_production_is_allowed():
    settings = _settings(environment="production", admin_token="a-real-secret")
    assert settings.environment == "production"
    assert settings.admin_token == "a-real-secret"


def test_invalid_vocab_is_rejected():
    with pytest.raises(ValidationError):
        _settings(vocab="bogus")


@pytest.mark.parametrize("environment", ["review", "production"])
def test_database_settings_needs_no_admin_token_outside_development(monkeypatch, environment):
    # The regression pin: the migrate job constructs DatabaseSettings with no
    # admin token mounted; that must never trip the dev-token guard.
    monkeypatch.setenv("GEOID_ENVIRONMENT", environment)

    settings = DatabaseSettings(_env_file=None)

    assert settings.database_url


def test_database_settings_reads_database_url_from_env(monkeypatch):
    monkeypatch.setenv("GEOID_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/geoid")

    settings = DatabaseSettings(_env_file=None)

    assert settings.database_url == "postgresql+asyncpg://u:p@db:5432/geoid"


def test_database_settings_excludes_app_level_fields():
    # Structural pin: app-layer config must not creep onto the DB-layer surface.
    for field in ("admin_token", "environment", "base_url", "vocab", "bulk_max_features"):
        assert field not in DatabaseSettings.model_fields


def test_settings_inherits_database_fields():
    assert _settings().db_pool_size == 10
