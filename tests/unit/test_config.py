"""Unit tests for configuration derivation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.config import Settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Integration fixtures export GEOID_* into os.environ; clear the ones these
    # derivation tests assert on so unit tests are order-independent.
    for key in (
        "GEOID_DID_HOST",
        "GEOID_BASE_URL",
        "GEOID_OIDC_ISSUER",
        "GEOID_OIDC_JWKS_URL",
        "GEOID_DEDUP_GRID_DEFAULT",
        "GEOID_ADMIN_TOKEN",
        "GEOID_ENVIRONMENT",
    ):
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> Settings:
    # _env_file=None so a developer's local .env never leaks into the assertions.
    return Settings(_env_file=None, **overrides)


def test_did_host_defaults_to_base_url_host():
    settings = _settings(base_url="https://data.fao.org")
    assert settings.did_host == "data.fao.org"


def test_explicit_did_host_is_respected():
    settings = _settings(base_url="http://localhost:8000", did_host="data.fao.org")
    assert settings.did_host == "data.fao.org"


def test_base_url_clean_strips_trailing_slash():
    settings = _settings(base_url="https://data.fao.org/")
    assert settings.base_url_clean == "https://data.fao.org"


def test_did_host_percent_encodes_non_default_port():
    # did:web encodes the port into the authority so the DID stays resolvable.
    settings = _settings(base_url="http://localhost:8000")
    assert settings.did_host == "localhost%3A8000"


def test_did_host_omits_default_ports():
    assert _settings(base_url="https://data.fao.org:443").did_host == "data.fao.org"
    assert _settings(base_url="http://data.fao.org:80").did_host == "data.fao.org"


def test_oidc_disabled_without_issuer():
    assert _settings().oidc_enabled is False


def test_oidc_enabled_with_issuer_and_jwks():
    settings = _settings(
        oidc_issuer="https://idp.fao.org", oidc_jwks_url="https://idp.fao.org/jwks"
    )
    assert settings.oidc_enabled is True


def test_dedup_grid_default_is_10m():
    # The Release-1 default coordinate precision: 9e-5 deg/vertex ≈ 10 m.
    assert _settings().dedup_grid_default == 9e-5


def test_dedup_grid_default_env_override(monkeypatch):
    monkeypatch.setenv("GEOID_DEDUP_GRID_DEFAULT", "0.0005")
    assert _settings().dedup_grid_default == 0.0005


def test_default_admin_token_in_production_is_rejected():
    with pytest.raises(ValidationError):
        _settings(environment="production")


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


def test_invalid_storage_backend_is_rejected():
    with pytest.raises(ValidationError):
        _settings(storage_backend="bogus")
