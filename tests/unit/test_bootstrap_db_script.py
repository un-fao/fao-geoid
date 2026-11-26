"""Unit tests for scripts/bootstrap_db.py pure helpers (no DB, no subprocess)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_db", ROOT / "scripts" / "bootstrap_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the dataclass decorator resolves the module's string
    # annotations through sys.modules[cls.__module__].
    sys.modules["bootstrap_db"] = module
    spec.loader.exec_module(module)
    return module


bootstrap_db = _load_script_module()


# --- _redact ------------------------------------------------------------------


def test_redact_masks_url_password():
    redacted = bootstrap_db._redact("postgresql://geoid:hunter2@db.internal:5432/geoid")
    assert "hunter2" not in redacted
    assert "***" in redacted


def test_redact_masks_keyword_dsn_password():
    redacted = bootstrap_db._redact("host=db.internal user=postgres password=hunter2")
    assert "hunter2" not in redacted
    assert "password=***" in redacted


def test_redact_leaves_passwordless_dsn_readable():
    dsn = "postgresql://geoid@db.internal:5432/geoid"
    assert "db.internal" in bootstrap_db._redact(dsn)


# --- _derive_app_url ----------------------------------------------------------


def test_derive_app_url_extracts_role_db_and_keeps_asyncpg():
    url, role, db = bootstrap_db._derive_app_url(
        "postgresql+asyncpg://geoid:pw@10.0.0.5:5432/geoid_prod", None
    )
    assert (role, db) == ("geoid", "geoid_prod")
    assert url.startswith("postgresql+asyncpg://")
    assert "pw@" in url  # effective URL keeps the password for migrate/seed/verify


def test_derive_app_url_normalizes_plain_postgresql_driver():
    url, _, _ = bootstrap_db._derive_app_url("postgresql://geoid:pw@host:5432/geoid", None)
    assert url.startswith("postgresql+asyncpg://")


def test_derive_app_url_password_override_wins():
    url, _, _ = bootstrap_db._derive_app_url(
        "postgresql+asyncpg://geoid:old@host:5432/geoid", "new-password"
    )
    assert "new-password@" in url
    assert "old@" not in url


def test_derive_app_url_rejects_non_postgresql():
    with pytest.raises(bootstrap_db.ConfigError):
        bootstrap_db._derive_app_url("mysql://geoid:pw@host:3306/geoid", None)


def test_derive_app_url_requires_role_and_database():
    with pytest.raises(bootstrap_db.ConfigError):
        bootstrap_db._derive_app_url("postgresql://host:5432/geoid", None)  # no role
    with pytest.raises(bootstrap_db.ConfigError):
        bootstrap_db._derive_app_url("postgresql://geoid:pw@host:5432", None)  # no db


def test_derive_app_url_rejects_garbage():
    with pytest.raises(bootstrap_db.ConfigError):
        bootstrap_db._derive_app_url("not a url at all", None)


# --- _psycopg_dsn -------------------------------------------------------------


def test_psycopg_dsn_strips_the_async_driver_and_keeps_credentials():
    dsn = bootstrap_db._psycopg_dsn("postgresql+asyncpg://geoid:pw@host:5432/geoid")
    assert dsn == "postgresql://geoid:pw@host:5432/geoid"


# --- _preflight_settings --------------------------------------------------------


def test_preflight_error_names_missing_oidc_settings_without_echoing_the_dsn(monkeypatch, tmp_path):
    # pydantic's error repr ends with the tail of the input dict, which here is the
    # app URL: a short suffix like "pw@127.0.0.1:5432/geoid" would print the password.
    monkeypatch.setattr(bootstrap_db, "REPO_ROOT", tmp_path)  # no .env
    for key in ("GEOID_OIDC_ISSUER", "GEOID_OIDC_JWKS_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEOID_ENVIRONMENT", "production")
    cfg = bootstrap_db.BootstrapConfig(
        admin_dsn="postgresql://postgres@127.0.0.1:5432/postgres",
        admin_password=None,
        app_url="postgresql+asyncpg://geoid:pw@127.0.0.1:5432/geoid",
        app_role="geoid",
        app_db="geoid",
        dry_run=True,
        skip_migrate=False,
        skip_seed=False,
        reset_app_password=False,
        assume_yes=True,
    )

    with pytest.raises(bootstrap_db.ConfigError) as excinfo:
        bootstrap_db._preflight_settings(cfg)

    message = str(excinfo.value)
    detail = message.split("Hint:")[0]
    assert "OIDC is not configured in 'production'" in detail
    assert "pw@" not in message
    assert "127.0.0.1" not in message
