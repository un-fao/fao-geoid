"""Environment-driven configuration (pydantic-settings, ``GEOID_*`` prefix).

The core service imports no cloud SDK; every deployment difference (FAO GCP vs
on-prem docker-compose vs a standalone country instance) is expressed here as
configuration, so the *same image* serves all of them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shipped dev/demo default; rejected outside development (see _forbid_dev_token_in_prod).
_DEV_ADMIN_TOKEN = "change-me-dev-only"


class DatabaseSettings(BaseSettings):
    """DB-layer configuration — the full surface the migrate job needs."""

    # extra="ignore" is load-bearing: a full .env with app-level GEOID_* keys
    # must not trip extra-field validation here.
    model_config = SettingsConfigDict(
        env_prefix="GEOID_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+asyncpg://geoid:geoid@localhost:5432/geoid",
        description="Async SQLAlchemy URL (asyncpg driver).",
    )

    # --- Connection pool + server-side timeouts (bound the blast radius of a
    #     slow client holding a pooled connection) ------------------------------
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout: int = Field(default=30, ge=1, description="seconds to wait for a pooled conn")
    db_statement_timeout_ms: int = Field(default=30_000, ge=0, description="0 disables")
    db_idle_in_tx_timeout_ms: int = Field(default=60_000, ge=0, description="0 disables")


class Settings(DatabaseSettings):
    """All runtime configuration. Read once and cached via :func:`get_settings`."""

    # --- Deployment ---------------------------------------------------------
    environment: Literal["development", "review", "production"] = Field(
        default="development",
        description=(
            "Deployment mode (env: GEOID_ENVIRONMENT). Outside development the "
            "default admin token is rejected at startup — see the validator. "
            "'review' is a deployed pre-production env: it enforces the real-token "
            "guard exactly like 'production'."
        ),
    )

    # --- Identity / resolvability ------------------------------------------
    base_url: str = Field(
        default="http://localhost:8000",
        description="Public base URL used to derive URIs and OGC links on read.",
    )
    root_path: str = Field(
        default="",
        description=(
            "Sub-path the API is mounted under behind a reverse proxy (env: "
            "GEOID_ROOT_PATH), e.g. '/geoid'. Passed to FastAPI(root_path=...) so "
            "Swagger/OpenAPI resolve behind the proxy, and prepended to BASE_URL when "
            "deriving public URIs/OGC links. The proxy is expected to strip this prefix "
            "before forwarding."
        ),
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

    # --- Geometry dedup precision ------------------------------------------
    dedup_grid_default: float = Field(
        default=1e-7,
        gt=0,
        description=(
            "The ONE global coordinate-precision grid (ST_ReducePrecision gridsize, "
            "decimal degrees) for geometry dedup. 1e-7 ≈ 1cm/vertex — exact-match "
            "semantics (float-jitter immunity only), per Remi's security ruling. "
            "Documented canonical value: the app never passes it to the DB (the grid "
            "is pinned inside the geoid_geom_hash_default() wrapper, migration 0001); "
            "a unit test pins this equal to that wrapper literal, so retunes are "
            "migration events, never a config-only change."
        ),
    )

    # --- Bulk write (synchronous multi-geometry POST) ----------------------
    bulk_max_features: int = Field(
        default=1_000,
        ge=1,
        description=(
            "Hard cap on the number of features accepted in one bulk POST "
            "FeatureCollection. A write bound MUST error (413), never silently "
            "truncate — exceeding it rejects the whole request."
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

    # --- Unified auth service seam (Release-1 stretch goal; Eduardo's team,
    #     expected to be OIDC — confirm before wiring). Inert until enabled;
    #     the oidc_* names are kept deliberately to avoid a second rename. ----
    oidc_issuer: str | None = Field(default=None)
    oidc_jwks_url: str | None = Field(default=None)
    oidc_audience: str | None = Field(default=None)

    @model_validator(mode="after")
    def _normalize_root_path(self) -> Settings:
        # Accept "geoid", "/geoid", "/geoid/" -> "/geoid"; "" stays "".
        rp = self.root_path.strip()
        object.__setattr__(self, "root_path", "/" + rp.strip("/") if rp else "")
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
        """Public base for link derivation: BASE_URL (no trailing slash) + ROOT_PATH.

        When the API sits behind a proxy sub-path (root_path, e.g. '/geoid'), every
        minted URI / OGC link must carry that prefix to stay resolvable. root_path is
        already normalised to '' or '/<path>', so this concatenation is safe.
        """
        return self.base_url.rstrip("/") + self.root_path

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_jwks_url)


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings. Override in tests via ``get_settings.cache_clear()``."""
    return Settings()
