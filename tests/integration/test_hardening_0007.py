"""Migration 0007 hardening — the DB holds the invariants the app enforces.

- 2D-only (M3): a Z geometry written PAST the app layer (raw SQL) is rejected by
  the ``geometry(Geometry, 4326)`` column typmod itself — no CHECK needed; this
  pins the schema-level rejection empirically (ADR-006).
- ``ck_collection_grant_email_normalized``: a write bypassing ``grant_repo._norm``
  cannot create case/whitespace-duplicate grants (M4).
- The four identity/dedup functions are PARALLEL SAFE with a pinned search_path;
  their BODIES are unchanged — the golden-vector + deterministic-geoid suites prove
  the output bytes are untouched (M6/L13).
- The dead 0002 paging index is gone (M17).
- The engine runs READ COMMITTED — the stated dependency of
  ``place_repo._resolve_incumbent_slug``'s bounded retry (L13).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from geoid.models import CK_COLLECTION_GRANT_EMAIL_NORMALIZED
from geoid.repositories._pg_errors import pg_fields

pytestmark = pytest.mark.integration

_IDENTITY_FUNCTIONS = (
    "geoid_geom_hash",
    "geoid_geom_hash_default",
    "geoid_from_geom_hash",
    "geoid_id_default",
)


async def _public_collection_id(session):
    return (
        await session.execute(text("SELECT id FROM collection WHERE slug = 'public'"))
    ).scalar_one()


@pytest.mark.parametrize(
    "wkt",
    ["POINT Z (1 2 5)", "POINT M (1 2 3)", "POINT ZM (1 2 3 4)"],
    ids=["z", "m", "zm"],
)
async def test_z_or_m_geometry_bypassing_the_app_is_rejected_by_the_column_type(session, wkt):
    # The geometry(Geometry, 4326) typmod is 2D: PostGIS rejects Z/M at the column
    # type itself ("Geometry has Z dimension but column does not"), below any app
    # code — so no CHECK constraint is needed and none exists.
    collection_id = await _public_collection_id(session)
    with pytest.raises(DBAPIError, match="dimension but column does not"):
        await session.execute(
            text(
                "INSERT INTO place (id, collection_id, geom) VALUES "
                f"(gen_random_uuid(), :c, ST_SetSRID(ST_GeomFromText('{wkt}'), 4326))"
            ),
            {"c": collection_id},
        )
    await session.rollback()


async def test_denormalized_grant_email_violates_the_check(session):
    collection_id = await _public_collection_id(session)
    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(
            text(
                "INSERT INTO collection_grant (id, collection_id, principal_email, role) "
                "VALUES (gen_random_uuid(), :c, 'Alice@X.org', 'editor')"
            ),
            {"c": collection_id},
        )
    constraint, _sqlstate = pg_fields(exc_info.value)
    assert constraint == CK_COLLECTION_GRANT_EMAIL_NORMALIZED


async def test_identity_functions_are_parallel_safe_with_pinned_search_path(session):
    rows = (
        await session.execute(
            text(
                "SELECT proname, proparallel::text, proconfig FROM pg_proc "
                "WHERE proname = ANY(:names)"
            ),
            {"names": list(_IDENTITY_FUNCTIONS)},
        )
    ).all()
    assert {row[0] for row in rows} == set(_IDENTITY_FUNCTIONS)
    for name, parallel, config in rows:
        assert parallel == "s", f"{name} is not PARALLEL SAFE"
        assert config and any("search_path=" in entry for entry in config), (
            f"{name} has no pinned search_path"
        )


async def test_dead_paging_index_is_dropped(session):
    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM pg_indexes WHERE indexname = 'place_collection_created_id_idx'"
            )
        )
    ).scalar_one()
    assert count == 0


async def test_engine_isolation_level_is_read_committed(session):
    # _resolve_incumbent_slug's bounded retry relies on READ COMMITTED taking a
    # fresh snapshot per statement (it breaks under REPEATABLE READ). Pin it so an
    # engine-config change can't silently invalidate the race recovery.
    level = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
    assert level == "read committed"
