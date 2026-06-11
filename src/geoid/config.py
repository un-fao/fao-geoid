"""Environment-driven configuration (pydantic-settings, ``GEOID_*`` prefix).

The core service imports no cloud SDK; every deployment difference (FAO GCP vs
on-prem docker-compose vs a standalone country instance) is expressed here as
configuration, so the *same image* serves all of them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shipped dev/demo default; rejected outside development (see _forbid_dev_token_in_prod).
_DEV_ADMIN_TOKEN = "change-me-dev-only"


class Settings(BaseSettings):
    """All runtime configuration. Read once and cached via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="GEOID_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Deployment ---------------------------------------------------------
    environment: Literal["development", "test", "production"] = Field(
        default="development",
        description=(
            "Deployment mode (env: GEOID_ENVIRONMENT). Outside development the "
            "default admin token is rejected at startup — see the validator."
        ),
    )

    # --- Database -----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://geoid:geoid@localhost:5432/geoid",
        description="Async SQLAlchemy URL (asyncpg driver).",
    )

    # --- Identity / resolvability ------------------------------------------
    base_url: str = Field(
        default="http://localhost:8000",
        description="Public base URL used to derive URIs and OGC links on read.",
    )
    did_host: str | None = Field(
        default=None,
        description="did:web host authority. Defaults to the BASE_URL host if unset.",
    )

    # --- Auth (temporary static-token stopgap; unified auth service later) -
    admin_token: str = Field(
        default=_DEV_ADMIN_TOKEN,
        description=(
            "Static bearer token gating create/manage/list endpoints "
            "(temporary stopgap until the FAO unified auth service)."
        ),
    )

    # --- Data-layer anonymous / federation knobs ---------------------------
    public_collection: str = Field(
        default="public",
        description="Reserved collection slug that accepts anonymous writes.",
    )
    instance_id: str = Field(
        default="fao-central",
        description="This instance's id, stamped into provenance + originating_instance.",
    )

    # --- API vocabulary (the reviewer's ruling — workspace/collection/item — is the
    #     "fao" preset and the shipped default) ------------------------------
    vocab: Literal["fao", "stac", "neutral"] = Field(
        default="fao", description='Surface vocabulary: "fao" (the ruling), "stac", or "neutral".'
    )

    # --- Object storage backend --------------------------------------------
    storage_backend: Literal["local", "gcs"] = Field(
        default="local", description='"local" or "gcs".'
    )
    storage_local_root: str = Field(default="./_blobstore")
    storage_gcs_bucket: str | None = Field(default=None)

    # --- Geometry dedup precision ------------------------------------------
    dedup_grid_default: float = Field(
        default=1e-7,
        gt=0,
        description=(
            "The ONE global coordinate-precision grid (ST_ReducePrecision gridsize, "
            "decimal degrees) for geometry dedup. 1e-7 ≈ 1cm/vertex — exact-match "
            "semantics (float-jitter immunity only), per the reviewer's security ruling. "
            "Effectively migration-pinned: it must equal the BEFORE-INSERT trigger's "
            "literal (migration 0001) or the incumbent lookup misses on conflicts — "
            "retunes are migration events, never a config-only change."
        ),
    )

    # --- Read paging guard rails -------------------------------------------
    default_limit: int = Field(default=100, ge=1)
    max_limit: int = Field(default=10_000, ge=1)
    max_offset: int = Field(
        default=100_000,
        ge=0,
        description=(
            "Upper bound on the offset paging parameter; a deep OFFSET forces the DB "
            "to scan and discard that many rows per request. rel=next links stop at "
            "the cap, so 0 cleanly disables deep paging."
        ),
    )

    # --- Connection pool + server-side timeouts (bound the blast radius of a
    #     slow client on the public streaming bulk-export endpoint) ----------
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout: int = Field(default=30, ge=1, description="seconds to wait for a pooled conn")
    db_statement_timeout_ms: int = Field(default=30_000, ge=0, description="0 disables")
    db_idle_in_tx_timeout_ms: int = Field(default=60_000, ge=0, description="0 disables")

    # --- Unified auth service seam (expected to be OIDC). Inert until enabled;
    #     the oidc_* names are kept deliberately to avoid a second rename. ----
    oidc_issuer: str | None = Field(default=None)
    oidc_jwks_url: str | None = Field(default=None)
    oidc_audience: str | None = Field(default=None)

    @model_validator(mode="after")
    def _default_did_host(self) -> Settings:
        if not self.did_host:
            parts = urlsplit(self.base_url)
            host = parts.hostname or "localhost"
            # did:web percent-encodes a non-default port into the authority so the
            # DID stays resolvable (e.g. localhost:8000 -> localhost%3A8000).
            if parts.port and parts.port not in (80, 443):
                host = f"{host}%3A{parts.port}"
            object.__setattr__(self, "did_host", host)
        return self

    @model_validator(mode="after")
    def _forbid_dev_token_in_prod(self) -> Settings:
        # Fail fast so a missing secret mount can't leave the dev token live in prod.
        if self.environment != "development" and self.admin_token == _DEV_ADMIN_TOKEN:
            raise ValueError(
                f"GEOID_ADMIN_TOKEN is the insecure default in {self.environment!r}; "
                "set a real GEOID_ADMIN_TOKEN (the dev default is only allowed when "
                "GEOID_ENVIRONMENT=development)."
            )
        return self

    @property
    def base_url_clean(self) -> str:
        """BASE_URL without a trailing slash (link derivation expects this)."""
        return self.base_url.rstrip("/")

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_jwks_url)


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings. Override in tests via ``get_settings.cache_clear()``."""
    return Settings()
