"""Unit tests for scripts/bootstrap_db.py pure helpers (no DB, no subprocess)."""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from geoid.config import Settings

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


def _config(**overrides):
    base = bootstrap_db.BootstrapConfig(
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
    return dataclasses.replace(base, **overrides)


def _delenv_any_case(monkeypatch, *keys):
    # pydantic matches env keys case-insensitively, so a stray lowercase variant
    # would still reach Settings.
    for key in list(os.environ):
        if key.upper() in keys:
            monkeypatch.delenv(key)


def _without_oidc_env(monkeypatch):
    _delenv_any_case(monkeypatch, "GEOID_OIDC_ISSUER", "GEOID_OIDC_JWKS_URL")


def test_preflight_error_names_missing_oidc_settings_without_echoing_the_dsn(monkeypatch, tmp_path):
    # pydantic's error repr ends with the tail of the input dict, which here is the
    # app URL: a short suffix like "pw@127.0.0.1:5432/geoid" would print the password.
    monkeypatch.setattr(bootstrap_db, "REPO_ROOT", tmp_path)  # no .env
    _without_oidc_env(monkeypatch)
    monkeypatch.setenv("GEOID_ENVIRONMENT", "production")

    with pytest.raises(bootstrap_db.ConfigError) as excinfo:
        bootstrap_db._preflight_settings(_config())

    message = str(excinfo.value)
    detail = message.split("Hint:")[0]
    assert "OIDC is not configured in 'production'" in detail
    assert "pw@" not in message
    assert "127.0.0.1" not in message


@pytest.mark.parametrize("env_file", [None, "GEOID_ENVIRONMENT=production\n"])
def test_preflight_with_skip_seed_does_not_require_oidc_outside_development(
    monkeypatch, tmp_path, env_file
):
    monkeypatch.setattr(bootstrap_db, "REPO_ROOT", tmp_path)
    if env_file is not None:
        (tmp_path / ".env").write_text(env_file)
    _without_oidc_env(monkeypatch)
    _delenv_any_case(monkeypatch, "GEOID_PUBLIC_COLLECTION")
    monkeypatch.setenv("GEOID_ENVIRONMENT", "production")

    settings = bootstrap_db._preflight_settings(_config(skip_seed=True))

    assert settings.environment == "production"


@pytest.mark.parametrize(
    ("process_env", "env_file", "field"),
    [
        ({"GEOID_PUBLIC_COLLECTION": "Bad Slug"}, None, "public_collection"),
        ({"GEOID_ENVIRONMENT": "prodution"}, None, "environment"),
        ({}, "GEOID_ENVIRONMENT=prodution\n", "environment"),
    ],
    ids=["bad-slug", "bad-environment", "bad-environment-in-dotenv"],
)
def test_preflight_with_skip_seed_still_rejects_invalid_settings(
    monkeypatch, tmp_path, process_env, env_file, field
):
    # Migration 0012 writes GEOID_PUBLIC_COLLECTION straight from os.environ, so
    # --skip-seed must keep every non-OIDC check, on both the .env and env-only builds.
    monkeypatch.setattr(bootstrap_db, "REPO_ROOT", tmp_path)
    if env_file is not None:
        (tmp_path / ".env").write_text(env_file)
    _without_oidc_env(monkeypatch)
    for key in ("GEOID_ENVIRONMENT", "GEOID_PUBLIC_COLLECTION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in process_env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(bootstrap_db.ConfigError) as excinfo:
        bootstrap_db._preflight_settings(_config(skip_seed=True))

    message = str(excinfo.value)
    assert field in message
    assert "OIDC" not in message


def _clean_slug_env(monkeypatch, tmp_path, env_file=None):
    monkeypatch.setattr(bootstrap_db, "REPO_ROOT", tmp_path)
    if env_file is not None:
        (tmp_path / ".env").write_text(env_file)
    _without_oidc_env(monkeypatch)
    _delenv_any_case(monkeypatch, "GEOID_PUBLIC_COLLECTION", "GEOID_ENVIRONMENT")


@pytest.mark.parametrize(
    ("skip_migrate", "skip_seed"),
    [(False, False), (True, False), (False, True)],
    ids=["migrate-and-seed", "seed-only", "migrate-only"],
)
def test_preflight_rejects_public_collection_set_only_in_dotenv(
    monkeypatch, tmp_path, skip_migrate, skip_seed
):
    # Migration 0012 and the seed read only the process env, so a .env-only slug
    # would bootstrap 'public' while the service later creates 'community'.
    _clean_slug_env(monkeypatch, tmp_path, "GEOID_PUBLIC_COLLECTION=community\n")

    with pytest.raises(bootstrap_db.ConfigError) as excinfo:
        bootstrap_db._preflight_settings(_config(skip_migrate=skip_migrate, skip_seed=skip_seed))

    message = str(excinfo.value)
    assert "'community'" in message
    assert "'public'" in message
    assert "export GEOID_PUBLIC_COLLECTION=community" in message
    assert "already ran migration 0012" in message
    assert "public_write = false" in message
    assert "pw@" not in message
    assert "127.0.0.1" not in message


def test_preflight_accepts_public_collection_set_in_dotenv_and_environment(monkeypatch, tmp_path):
    _clean_slug_env(monkeypatch, tmp_path, "GEOID_PUBLIC_COLLECTION=community\n")
    monkeypatch.setenv("GEOID_PUBLIC_COLLECTION", "community")

    settings = bootstrap_db._preflight_settings(_config())

    assert settings.public_collection == "community"


def test_preflight_accepts_public_collection_without_dotenv(monkeypatch, tmp_path):
    _clean_slug_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEOID_PUBLIC_COLLECTION", "community")

    settings = bootstrap_db._preflight_settings(_config())

    assert settings.public_collection == "community"


def test_preflight_rejects_lowercase_process_key_that_migration_cannot_see(monkeypatch, tmp_path):
    # pydantic (and so the seed) sees the lowercase key; 0012's exact lookup does not.
    _clean_slug_env(monkeypatch, tmp_path, "GEOID_PUBLIC_COLLECTION=community\n")
    monkeypatch.setenv("geoid_public_collection", "community")

    with pytest.raises(bootstrap_db.ConfigError) as excinfo:
        bootstrap_db._preflight_settings(_config())

    message = str(excinfo.value)
    assert "migration 0012" in message
    assert "'public'" in message
    assert "the seed step" not in message

    settings = bootstrap_db._preflight_settings(_config(skip_migrate=True))
    assert settings.public_collection == "community"


@pytest.mark.parametrize(
    ("exact_value", "slug"),
    [("postgresql://u:SECRETMARK@h/db", "community"), ("", "public")],
    ids=["credential", "empty"],
)
def test_preflight_rejects_a_shadowed_exact_key_without_echoing_it(
    monkeypatch, tmp_path, exact_value, slug
):
    # Migration 0012 uses the exact key verbatim (an empty value matches no slug).
    _clean_slug_env(monkeypatch, tmp_path, f"GEOID_PUBLIC_COLLECTION={slug}\n")
    monkeypatch.setenv("GEOID_PUBLIC_COLLECTION", exact_value)
    monkeypatch.setenv("geoid_public_collection", slug)
    # Precondition: pydantic keeps the later (lowercase) variant, so the exact key is
    # shadowed and never slug-validated.
    shadowed = Settings(database_url=_config().app_url, _env_file=None)
    assert shadowed.public_collection == slug

    with pytest.raises(bootstrap_db.ConfigError) as excinfo:
        bootstrap_db._preflight_settings(_config())

    message = str(excinfo.value)
    assert "migration 0012" in message
    assert "hidden; not validated" in message
    assert "SECRETMARK" not in message


def test_preflight_skips_public_collection_check_when_neither_migrating_nor_seeding(
    monkeypatch, tmp_path
):
    _clean_slug_env(monkeypatch, tmp_path, "GEOID_PUBLIC_COLLECTION=community\n")

    settings = bootstrap_db._preflight_settings(_config(skip_migrate=True, skip_seed=True))

    assert settings.public_collection == "public"


def test_preflight_requires_oidc_in_environment_even_when_dotenv_supplies_it(monkeypatch, tmp_path):
    # The seed step uses the env-only build, so OIDC from .env alone is not enough.
    _clean_slug_env(
        monkeypatch,
        tmp_path,
        "GEOID_OIDC_ISSUER=https://idp.test/realms/x\nGEOID_OIDC_JWKS_URL=https://idp.test/jwks\n",
    )
    monkeypatch.setenv("GEOID_ENVIRONMENT", "production")

    with pytest.raises(bootstrap_db.ConfigError) as excinfo:
        bootstrap_db._preflight_settings(_config())

    assert "OIDC is not configured in 'production'" in str(excinfo.value)


# --- run_migrations -------------------------------------------------------------


def test_run_migrations_hands_the_child_only_the_confirmed_database_url(monkeypatch):
    # pydantic in the child would let a later case variant win over the exact key,
    # migrating a database the operator never confirmed.
    monkeypatch.setenv("GEOID_DATABASE_URL", "postgresql+asyncpg://u@a/a")
    monkeypatch.setenv("geoid_database_url", "postgresql+asyncpg://u@b/b")
    captured = {}

    def fake_run(cmd, *, env, cwd):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bootstrap_db.subprocess, "run", fake_run)

    bootstrap_db.run_migrations(_config())

    url_keys = {k: v for k, v in captured["env"].items() if k.upper() == "GEOID_DATABASE_URL"}
    assert url_keys == {"GEOID_DATABASE_URL": _config().app_url}
