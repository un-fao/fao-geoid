"""FastAPI application factory — router mount, OpenAPI metadata, lifespan bootstrap."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import InterfaceError, OperationalError

from geoid import __version__
from geoid.api import health, manage, ogc, places
from geoid.api.errors import register_exception_handlers
from geoid.config import get_settings
from geoid.db import dispose_engine, get_sessionmaker
from geoid.services.bootstrap import ensure_public_collection

logger = logging.getLogger("geoid")

_DESCRIPTION = """\
**GeoID** mints a globally unique, secure, **immutable** identifier (a *geoid*,
UUIDv7) for every geospatial place, deduplicates by canonical geometry, tracks
provenance, and accepts **anonymous contributions** — served over **OGC API
Features**.

* **Write / registry** — `POST /collections/{id}/items` → `{geoid, uri}`
* **Bulk write** — `POST /collections/{id}/items/bulk` (a GeoJSON FeatureCollection,
  synchronous, partial success → a per-feature report)
* **Resolve** — `GET /geoid/{uuid}`, `GET /collections/{id}/external/{external_id}`
* **OGC API Features read** — landing, `/conformance`, `/collections`, items (CQL2 + paging)
* **Health** — `GET /health` (DB connectivity probe)
"""


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


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="GeoID",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
        # Behind a proxy sub-path (e.g. /geoid/v1) so Swagger/openapi.json resolve there;
        # "" (default) leaves the app at root. The proxy strips the prefix before forwarding.
        root_path=settings.root_path,
        license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
        contact={"name": "FAO GeoID Team"},
    )
    register_exception_handlers(app)

    # Probe surface first, then the read surface (OGC) at the root, the registry,
    # and management routers. /health is a literal path — no OGC collision.
    app.include_router(health.router)
    app.include_router(ogc.router)
    app.include_router(places.router)
    app.include_router(manage.router)

    app.state.settings = settings
    return app


app = create_app()
