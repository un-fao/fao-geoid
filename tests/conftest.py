"""Shared test fixtures.

Integration tests run against an ephemeral PostGIS container (testcontainers).
The image is pinned to ``postgis/postgis:17-3.5`` on ``linux/amd64`` so the GEOS
build — and therefore ``ST_Normalize`` output, the load-bearing part of the dedup
recipe — matches Cloud SQL on every host (native on amd64 CI, emulated on arm64
dev machines). If Docker is unavailable, integration tests skip cleanly so
``pytest -m unit`` still runs anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Force amd64 so the official postgis image (no arm64 manifest) runs everywhere.
os.environ.setdefault("DOCKER_DEFAULT_PLATFORM", "linux/amd64")

# Docker Desktop on macOS exposes its socket under ~/.docker/run, but docker-py
# (used by testcontainers) defaults to /var/run/docker.sock and does not read the
# CLI context. Point both at the Desktop socket when present.
if "DOCKER_HOST" not in os.environ:
    _desktop_socket = Path.home() / ".docker" / "run" / "docker.sock"
    if _desktop_socket.exists():
        os.environ["DOCKER_HOST"] = f"unix://{_desktop_socket}"

# The with-block tears the container down; ryuk (the reaper) is unnecessary and
# can be flaky against Docker Desktop.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

_TRUNCATE = (
    "TRUNCATE place, geoid_registry, change_log, collection, catalog RESTART IDENTITY CASCADE"
)


def _docker_available() -> bool:
    try:
        import docker
    except ImportError:
        return False  # docker package not installed
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False  # installed but daemon down / unreachable


@pytest.fixture(scope="session")
def _postgis():
    if not _docker_available():
        pytest.skip("Docker is not available; skipping PostGIS integration tests")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        "postgis/postgis:17-3.5",
        username="geoid",
        password="geoid",
        dbname="geoid",
        driver="asyncpg",
    ).with_kwargs(platform="linux/amd64")
    with container as pg:
        yield pg


@pytest.fixture(scope="session")
def _migrated(_postgis):
    """Point settings at the container, apply migrations to head, return the URL."""
    from alembic import command
    from alembic.config import Config

    from geoid.config import get_settings

    url = _postgis.get_connection_url()
    os.environ["GEOID_DATABASE_URL"] = url
    os.environ["GEOID_ADMIN_TOKEN"] = "test-admin-token"
    os.environ["GEOID_BASE_URL"] = "http://testserver"
    os.environ["GEOID_INSTANCE_ID"] = "test-instance"
    os.environ["GEOID_PUBLIC_COLLECTION"] = "public"
    get_settings.cache_clear()

    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def db_clean(_migrated):
    """A clean schema with the reserved ``public`` collection, fresh per test."""
    from sqlalchemy import text

    from geoid.config import get_settings
    from geoid.db import dispose_engine, get_sessionmaker
    from geoid.services.bootstrap import ensure_public_collection

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # The immutability / append-only triggers now block TRUNCATE too. The test
        # role is a superuser, so disable triggers for the reset only (replica mode)
        # and restore immediately — a production app role cannot do this.
        await session.execute(text("SET session_replication_role = replica"))
        await session.execute(text(_TRUNCATE))
        await session.execute(text("SET session_replication_role = origin"))
        await ensure_public_collection(session, get_settings())
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
async def session(db_clean):
    """A raw async DB session (for repository / SQL-recipe tests)."""
    from geoid.db import get_sessionmaker

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
async def client(db_clean):
    """An httpx AsyncClient bound to a freshly built app (in-process ASGI)."""
    from httpx import ASGITransport, AsyncClient

    from geoid.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-token"}


# --- Shared geometry fixtures (GeoJSON Features) ----------------------------


def _feature(coords, *, external_id=None, properties=None, geom_type="Polygon"):
    feature = {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": coords},
        "properties": properties or {},
    }
    if external_id is not None:
        feature["id"] = external_id
    return feature


@pytest.fixture
def unit_square_ccw():
    # CCW unit square at origin offset (10,10)
    return _feature([[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]])


@pytest.fixture
def unit_square_reversed():
    # Same square, CW winding + different ring start -> same canonical hash
    return _feature([[[10, 10], [10, 11], [11, 11], [11, 10], [10, 10]]])


@pytest.fixture
def other_square():
    return _feature([[[30, 30], [31, 30], [31, 31], [30, 31], [30, 30]]])
