"""CLI entrypoints — one image, three commands: ``web``, ``migrate``, ``import``.

geoid web      # run the API (uvicorn)
geoid migrate  # apply Alembic migrations to head (Cloud Run Job)
geoid import   # run ONE async import job to completion (Cloud Run Job);
               # job id from GEOID_JOB_ID (the per-execution env override) or argv
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

    # uvicorn's log_level configures only the uvicorn.* loggers; geoid.* records
    # propagate to a handler-less root where only WARNING+ leaks out via
    # logging.lastResort. Configure the root here so app INFO (OIDC rejections,
    # bootstrap) is actually emitted in the deployed image.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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


def run_import() -> int:
    import asyncio

    # Same rationale as run_web: without a root handler the worker's INFO records
    # (claim/finish/failure) never emit in the deployed image.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    job_id = os.environ.get("GEOID_JOB_ID") or (sys.argv[2] if len(sys.argv) > 2 else None)
    if not job_id:
        logger.error("no job id: set GEOID_JOB_ID or pass it as the second argument")
        return 2

    from geoid.db import dispose_engine
    from geoid.services import import_service

    async def _run() -> int:
        try:
            return await import_service.run_job(job_id)
        finally:
            await dispose_engine()

    return asyncio.run(_run())


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "web"
    if command == "web":
        run_web()
    elif command == "migrate":
        run_migrate()
    elif command == "import":
        return run_import()
    else:
        logger.error("unknown command %r; use 'web', 'migrate' or 'import'", command)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
