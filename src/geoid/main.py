"""FastAPI application factory — router mount, OpenAPI metadata, lifespan bootstrap."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy.exc import InterfaceError, OperationalError

from geoid import __version__
from geoid.api import grants, health, manage, ogc, places
from geoid.api.errors import register_exception_handlers
from geoid.config import get_settings
from geoid.db import dispose_engine, get_sessionmaker
from geoid.services.bootstrap import ensure_public_collection

logger = logging.getLogger("geoid")

_DESCRIPTION = """\
**GeoID** mints a globally unique, secure, **immutable** identifier (a *geoid*,
UUIDv8) for every geospatial place, deduplicates by canonical geometry, tracks
provenance — served over **OGC API Features**.

* **Write / registry** — `POST /collections/{id}/items` → `{geoid, uri}`
* **Bulk write** — `POST /collections/{id}/items/bulk` (a GeoJSON FeatureCollection,
  processed in-request, returns a per-feature report)
* **Resolve** — `GET /{uuid}`, `GET /collections/{id}/external/{external_id}`
* **Health** — `GET /health` (DB connectivity probe)
"""


_TAGS_METADATA = [
    {"name": "health", "description": "App health check"},
    {"name": "registry", "description": "Write and resolution endpoints"},
    {"name": "manage", "description": "Admin-gated management endpoints"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            await ensure_public_collection(session, settings)
            await session.commit()
        logger.info("bootstrap: ensured public collection %r", settings.public_collection)
    except (OperationalError, InterfaceError, OSError) as exc:
        # Tolerate only a not-ready DB; let programming errors propagate.
        logger.warning("bootstrap skipped (DB not ready?): %s", exc)
    yield
    await dispose_engine()


def _swagger_oauth2(settings):
    """Optional Swagger Authorization-Code+PKCE config (off by default).

    Returns ``(dependencies, init_oauth)``. When the flag is off (or OIDC is not
    enabled — the auth/token URLs derive from the issuer), both are empty/None, so
    ``FastAPI(...)`` sees its defaults and ``/openapi.json`` + ``/docs`` are
    byte-identical to today. When on, an ``OAuth2AuthorizationCodeBearer`` scheme is
    declared (``auto_error=False`` → non-blocking, NOT a hard route gate) purely so
    Swagger renders the PKCE login button; the obtained token rides the same
    Authorization header ``require_principal`` already reads.
    """
    if not (settings.swagger_oauth2_enabled and settings.oidc_enabled):
        return [], None
    from fastapi.security import OAuth2AuthorizationCodeBearer

    oauth2_scheme = OAuth2AuthorizationCodeBearer(
        authorizationUrl=settings.oidc_auth_url or "",
        tokenUrl=settings.oidc_token_url or "",
        auto_error=False,
        scheme_name="KeycloakOAuth2",
    )
    init_oauth = {
        "clientId": settings.swagger_oauth2_client_id,
        "usePkceWithAuthorizationCodeGrant": True,
    }
    return [Depends(oauth2_scheme)], init_oauth


def create_app() -> FastAPI:
    settings = get_settings()
    oauth2_dependencies, swagger_init_oauth = _swagger_oauth2(settings)
    app = FastAPI(
        title="GeoID",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
        root_path=settings.root_path,
        license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
        contact={"name": "FAO GeoID Team"},
        openapi_tags=_TAGS_METADATA,
        dependencies=oauth2_dependencies or None,
        swagger_ui_init_oauth=swagger_init_oauth,
    )
    register_exception_handlers(app)

    # Probe surface first, then the OGC read surface (hidden from /docs), the
    # per-collection grant routes, the registry/resolver, and the admin /manage
    # router. The grants router is mounted BEFORE places so its literal
    # /collections/... routes are matched ahead of the root /{geoid} catch-all;
    # /health is a literal path — no OGC collision.
    app.include_router(health.router)
    app.include_router(ogc.router, include_in_schema=False)  # live, but hidden from /docs
    app.include_router(grants.router)
    app.include_router(places.router)
    app.include_router(manage.router)

    app.state.settings = settings
    return app


app = create_app()
