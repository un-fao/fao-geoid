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
            "Deployment mode (env: GEOID_ENVIRONMENT). Outside development OIDC "
            "must be configured or the service refuses to boot — see the validator. "
            "'review' is a deployed pre-production env: it enforces the OIDC-required "
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

    # --- Data-layer anonymous / federation knobs ---------------------------
    public_collection: str = Field(
        default="public",
        description="Reserved collection slug that accepts anonymous writes.",
    )
    instance_id: str = Field(
        default="fao-central",
        description="This instance's id, stamped into provenance + originating_instance.",
    )

    # --- Geometry identity precision ----------------------------------------
    dedup_grid_default: float = Field(
        default=1e-7,
        gt=0,
        description=(
            "The ONE global identity-lattice cell size (decimal degrees) for "
            "geometry dedup/identity. 1e-7 ≈ 1cm/vertex — exact-match semantics "
            "(float-jitter immunity only). "
            "Documented canonical value: the app never passes it to the DB — recipe "
            "v2 quantizes by the integer SCALE = 10^7 pinned inside migration 0008's "
            "geoid_quantize_v2() (and mirrored by domain/geometry_identity.SCALE); a "
            "unit test pins SCALE == round(1 / this value), so retunes are migration "
            "events (identity-version events under v2), never a config-only change."
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

    # --- Async bulk import jobs (OGC Processes subset; POST /items/import) -----
    job_executor: Literal["inline", "cloud_run_job"] = Field(
        default="inline",
        description=(
            "How submitted import jobs run: 'inline' = an asyncio task in this "
            "process (dev/compose/tests, zero cloud deps); 'cloud_run_job' = one "
            "Cloud Run Job execution per import (the deployed shape)."
        ),
    )
    import_job_name: str | None = Field(
        default=None,
        description=(
            "Fully-qualified Cloud Run Job name the executor triggers "
            "(projects/P/locations/R/jobs/NAME). Required iff "
            "job_executor='cloud_run_job'."
        ),
    )
    job_max_bytes: int = Field(
        default=104_857_600,
        ge=1,
        description="Per-blob download cap (bytes); counted while streaming, never trusted.",
    )
    job_max_features: int = Field(
        default=100_000,
        ge=1,
        description=(
            "Hard cap on total features across one import job. A write bound MUST "
            "error (job fails), never silently truncate — enforced mid-stream."
        ),
    )
    job_max_files: int = Field(
        default=1_000, ge=1, description="Cap on objects a gs:// prefix job may ingest."
    )
    job_max_concurrent: int = Field(
        default=3,
        ge=1,
        description="Submit-time valve: active (accepted/running) jobs beyond this → 429.",
    )
    job_stale_seconds: int = Field(
        default=600,
        ge=1,
        description=(
            "On-read reaper threshold: a 'running' job whose heartbeat (updated_at) "
            "is older than this is flipped to failed when its status is next read."
        ),
    )
    job_allowed_url_hosts: str = Field(
        default=(
            "storage.googleapis.com,*.storage.googleapis.com,"
            "s3.amazonaws.com,*.s3.amazonaws.com,*.amazonaws.com"
        ),
        description=(
            "CSV host allowlist for https import refs ('*.suffix' wildcards). THE "
            "SSRF control: only vendor storage hosts are fetchable by the worker."
        ),
    )
    job_allowed_buckets: str = Field(
        default="",
        description=(
            "CSV allowlist of GCS buckets reachable via native gs:// refs/prefixes. "
            "Default EMPTY = gs:// disabled — kills the confused-deputy problem "
            "(callers pointing our service account at any bucket it can read)."
        ),
    )

    # --- OIDC resource-server auth (Keycloak) — the ONLY authentication path.
    #     oidc_enabled gates on issuer + jwks_url. Unset (development only) the
    #     service is anonymous-only: every bearer credential is rejected with 401.
    #     Outside development both MUST be set or the service refuses to boot
    #     (see _require_oidc_outside_development). ----------------------------
    oidc_issuer: str | None = Field(
        default=None,
        description="OIDC issuer (the realm URL); auth/token endpoints derive from it.",
    )
    oidc_jwks_url: str | None = Field(
        default=None,
        description="JWKS URI; PyJWKClient caches signing keys by kid in-process.",
    )
    oidc_audience: str = Field(
        default="geoid-be",
        description=(
            "Required audience: a valid Keycloak token must carry this in `aud` (our "
            "API's client id, geoid-be). Validated only when OIDC is enabled; setting "
            "it does NOT by itself enable OIDC."
        ),
    )
    oidc_roles_client: str = Field(
        default="geoid-roles",
        description="resource_access client whose `roles` array carries GeoID roles.",
    )
    oidc_admin_role: str = Field(
        default="geoid.sysadmin",
        description="Role (under oidc_roles_client) granting the global sysadmin tier.",
    )
    oidc_leeway_seconds: int = Field(
        default=30, ge=0, description="Clock-skew leeway (seconds) for exp/nbf validation."
    )

    # --- Swagger OAuth2 (Authorization Code + PKCE) — OFF by default. The paste-a-
    #     bearer button is always present (it carries a Keycloak JWT); this flag only
    #     adds the interactive login flow, gated so /openapi.json and /docs are
    #     byte-identical to today when it is off.
    swagger_oauth2_enabled: bool = Field(default=False)
    swagger_oauth2_client_id: str = Field(default="geoid-fe")

    @model_validator(mode="after")
    def _normalize_root_path(self) -> Settings:
        # Accept "geoid", "/geoid", "/geoid/" -> "/geoid"; "" stays "".
        rp = self.root_path.strip()
        object.__setattr__(self, "root_path", "/" + rp.strip("/") if rp else "")
        return self

    @model_validator(mode="after")
    def _require_import_job_name(self) -> Settings:
        # Fail fast at boot, not at first submit: the cloud_run_job executor cannot
        # dispatch without the fully-qualified job name.
        if self.job_executor == "cloud_run_job" and not self.import_job_name:
            raise ValueError(
                "GEOID_IMPORT_JOB_NAME is required when GEOID_JOB_EXECUTOR=cloud_run_job "
                "(projects/P/locations/R/jobs/NAME)."
            )
        return self

    @property
    def job_allowed_url_hosts_list(self) -> tuple[str, ...]:
        return tuple(h.strip().lower() for h in self.job_allowed_url_hosts.split(",") if h.strip())

    @property
    def job_allowed_buckets_list(self) -> tuple[str, ...]:
        return tuple(b.strip() for b in self.job_allowed_buckets.split(",") if b.strip())

    @model_validator(mode="after")
    def _require_oidc_outside_development(self) -> Settings:
        # Fail fast so a missing OIDC env var can never leave a deployed environment
        # with zero authentication paths (Keycloak is the only credential validator).
        if self.environment != "development" and not self.oidc_enabled:
            raise ValueError(
                f"OIDC is not configured in {self.environment!r}; set GEOID_OIDC_ISSUER "
                "and GEOID_OIDC_JWKS_URL (Keycloak is the only authentication path; "
                "running without it is only allowed when GEOID_ENVIRONMENT=development)."
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

    @property
    def oidc_auth_url(self) -> str | None:
        """Keycloak authorization endpoint derived from the issuer (None if unset)."""
        if not self.oidc_issuer:
            return None
        return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/auth"

    @property
    def oidc_token_url(self) -> str | None:
        """Keycloak token endpoint derived from the issuer (None if unset)."""
        if not self.oidc_issuer:
            return None
        return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/token"


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings. Override in tests via ``get_settings.cache_clear()``."""
    return Settings()
