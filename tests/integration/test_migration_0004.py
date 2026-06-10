"""Migration 0004's data-mutating statements, exercised against a real 0003 state.

The session fixture migrates a pristine schema 0001→head in one pass, where 0004's
stamp/restamp UPDATEs match zero rows — so without this test the only data-mutating
migration in the repo would never execute its branches in CI. Here we build the
state 0004 was written for (collections created AT revision 0003) in a scratch
database, upgrade to head, and assert every branch:

- empty + stamped with the old default (9e-5)  -> restamped 1e-7
- empty + stamped with an interim value (1e-6) -> untouched
- empty + stamped with non-numeric junk        -> untouched, migration does NOT abort
- empty + unstamped                            -> stamped 1e-7
- populated + unstamped                        -> stamped 9e-5 (effective grid
  preserved: stored hashes still match, identical re-insert still collides)
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

_SCRATCH_DB = "mig0004_scratch"
_POLY = "SRID=4326;POLYGON((0 0,0.001 0,0.001 0.001,0 0.001,0 0))"


def _plain_dsn(sqlalchemy_url: str, dbname: str) -> str:
    """postgresql+asyncpg:// SQLAlchemy URL -> bare asyncpg DSN for ``dbname``."""
    base = sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + "/" + dbname


async def _admin_execute(dsn: str, sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _seed_0003_state(dsn: str) -> tuple[dict[str, uuid.UUID], bytes]:
    """Create the five collection shapes (and one place) as 0003-era data."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        ws = uuid.uuid4()
        await conn.execute("INSERT INTO workspace (id, slug) VALUES ($1, 'ws')", ws)
        collections = {
            "stamped-default-empty": '{"dedup_grid": 9e-05}',
            "stamped-interim-empty": '{"dedup_grid": 1e-06}',
            "stamped-broken-empty": '{"dedup_grid": "ten-meters"}',
            "unstamped-empty": "{}",
            "unstamped-populated": "{}",
        }
        ids: dict[str, uuid.UUID] = {}
        for slug, meta in collections.items():
            cid = uuid.uuid4()
            ids[slug] = cid
            await conn.execute(
                "INSERT INTO collection (id, workspace_id, slug, metadata) "
                "VALUES ($1, $2, $3, $4::jsonb)",
                cid, ws, slug, meta,
            )
        # Hashed by the BEFORE-INSERT trigger with 0003's 9e-5 fallback (unstamped).
        place_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO place (id, collection_id, geom) "
            "VALUES ($1, $2, ST_GeomFromEWKT($3))",
            place_id, ids["unstamped-populated"], _POLY,
        )
        hash_at_0003 = await conn.fetchval(
            "SELECT geom_hash FROM place WHERE id = $1", place_id
        )
        return ids, hash_at_0003
    finally:
        await conn.close()


async def _assert_post_0004(dsn: str, ids: dict[str, uuid.UUID], hash_at_0003: bytes) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        async def grid(slug: str) -> str | None:
            return await conn.fetchval(
                "SELECT metadata->>'dedup_grid' FROM collection WHERE slug = $1", slug
            )

        assert float(await grid("stamped-default-empty")) == 1e-7
        assert float(await grid("stamped-interim-empty")) == 1e-6
        assert await grid("stamped-broken-empty") == "ten-meters"
        assert float(await grid("unstamped-empty")) == 1e-7
        assert float(await grid("unstamped-populated")) == 9e-5

        # The populated collection's effective grid was preserved: the stored hash
        # is untouched and an identical re-submission still collides (dedup intact).
        hash_now = await conn.fetchval("SELECT geom_hash FROM place")
        assert hash_now == hash_at_0003
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO place (id, collection_id, geom) "
                "VALUES ($1, $2, ST_GeomFromEWKT($3))",
                uuid.uuid4(), ids["unstamped-populated"], _POLY,
            )
    finally:
        await conn.close()


def test_0004_stamps_and_restamps_collections_created_at_0003(_migrated):
    # Sync test on purpose: alembic's async env.py calls asyncio.run(), which
    # cannot nest inside a running event loop.
    from alembic import command
    from alembic.config import Config

    from geoid.config import get_settings

    main_url: str = _migrated
    admin_dsn = _plain_dsn(main_url, main_url.rsplit("/", 1)[1])
    scratch_dsn = _plain_dsn(main_url, _SCRATCH_DB)
    scratch_url = main_url.rsplit("/", 1)[0] + "/" + _SCRATCH_DB

    asyncio.run(_admin_execute(admin_dsn, f"DROP DATABASE IF EXISTS {_SCRATCH_DB} WITH (FORCE)"))
    asyncio.run(_admin_execute(admin_dsn, f"CREATE DATABASE {_SCRATCH_DB}"))
    try:
        # migrations/env.py reads GEOID_DATABASE_URL via get_settings().
        os.environ["GEOID_DATABASE_URL"] = scratch_url
        get_settings.cache_clear()
        config = Config(str(ROOT / "alembic.ini"))

        command.upgrade(config, "0003_dedup_grid_default_10m")
        ids, hash_at_0003 = asyncio.run(_seed_0003_state(scratch_dsn))
        command.upgrade(config, "head")  # must survive the non-numeric stamp
        asyncio.run(_assert_post_0004(scratch_dsn, ids, hash_at_0003))
    finally:
        os.environ["GEOID_DATABASE_URL"] = main_url
        get_settings.cache_clear()
        asyncio.run(
            _admin_execute(admin_dsn, f"DROP DATABASE IF EXISTS {_SCRATCH_DB} WITH (FORCE)")
        )
