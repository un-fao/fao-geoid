"""CLI entrypoints — one image, three commands: ``web``, ``migrate``, ``ingest-worker``.

geoid web            # run the API (uvicorn)
geoid migrate        # apply Alembic migrations to head (Cloud Run Job)
geoid ingest-worker  # drain the async bulk-ingest queue, then exit (Cloud Run Job)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("geoid.cli")


def _alembic_ini() -> str:
    """Locate alembic.ini (Docker WORKDIR, repo root, or GEOID_ALEMBIC_INI)."""
    override = os.environ.get("GEOID_ALEMBIC_INI")
    if override:
        return override
    candidates = (
        Path.cwd() / "alembic.ini",
        Path(__file__).resolve().parents[2] / "alembic.ini",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "alembic.ini"


def run_web() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    # WEB_CONCURRENCY>1 spawns multiple worker processes (one event loop each);
    # on Cloud Run the simpler lever is concurrency + horizontal instances.
    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    uvicorn.run(
        "geoid.main:app",
        host=host,
        port=port,
        workers=workers if workers > 1 else None,
        loop="uvloop",  # fail loud if the fast wheels are missing in the runtime image
        http="httptools",
        log_level="info",
    )


def run_migrate() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(_alembic_ini())
    command.upgrade(config, "head")


def run_ingest_worker() -> None:
    """Drain the async bulk-ingest queue once, then exit (Cloud Run Job semantics).

    Builds its own event loop + sessions (never the request-scoped get_session). The
    worker claims all currently-pending jobs via SKIP LOCKED, processes each, and
    exits — a fresh execution is triggered per enqueue (plus a Scheduler fallback).
    """
    import asyncio

    from geoid.config import get_settings
    from geoid.db import dispose_engine
    from geoid.services import ingest_service

    # Standalone Cloud Run Job: nothing else configures logging here, so the
    # info-level drain summary needs a root handler to reach Cloud Logging.
    logging.basicConfig(level=logging.INFO)

    async def _drain() -> int:
        settings = get_settings()
        try:
            claimed = await ingest_service.process_pending_jobs(settings)
        finally:
            await dispose_engine()
        return len(claimed)

    count = asyncio.run(_drain())
    logger.info("ingest-worker: processed %d job(s)", count)


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "web"
    if command == "web":
        run_web()
    elif command == "migrate":
        run_migrate()
    elif command == "ingest-worker":
        run_ingest_worker()
    else:
        logger.error("unknown command %r; use 'web', 'migrate', or 'ingest-worker'", command)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
