"""FastAPI application factory — router mount, OpenAPI metadata, lifespan bootstrap."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import InterfaceError, OperationalError

from geoid import __version__
from geoid.api import bulk, health, manage, ogc, places
from geoid.api.errors import register_exception_handlers
from geoid.config import get_settings
from geoid.db import dispose_engine, get_sessionmaker
from geoid.services.bootstrap import ensure_public_collection

logger = logging.getLogger("geoid")

_DESCRIPTION = """\
**GeoID** mints a globally unique, secure, **immutable** identifier (a *geoid*,
UUIDv7, framed as a `did:web` Decentralized Identifier) for every geospatial place,
deduplicates by canonical geometry, tracks provenance, and accepts **anonymous
contributions** — served over **OGC API Features**.

* **Write / registry** — `POST /collections/{id}/items` → `{geoid, did, uri}`
* **Resolve** — `GET /geoid/{uuid}`, `GET /collections/{id}/external/{external_id}`
* **OGC API Features read** — landing, `/conformance`, `/collections`, items (bbox + CQL2 + paging)
* **Bulk** — `GET /collections/{id}/bulk` (public GeoJSON export)
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
        license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
        contact={"name": "FAO GeoID Team"},
    )
    register_exception_handlers(app)

    # Probe surface first, then the read surface (OGC) at the root, the registry,
    # management, and bulk routers. /health is a literal path — no OGC collision.
    app.include_router(health.router)
    app.include_router(ogc.router)
    app.include_router(places.router)
    app.include_router(manage.router)
    app.include_router(bulk.router)

    app.state.settings = settings
    return app


app = create_app()
