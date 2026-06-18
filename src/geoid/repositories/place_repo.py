"""Place data access — the arbiter-CTE insert and the OGC read queries.

The insert is the product's hot path. ``geoid_registry`` is the global
geometry-dedup arbiter: a single-statement CTE inserts the registry row
(``geom_hash`` via ``geoid_geom_hash_default``) with ``ON CONFLICT ... DO
NOTHING`` on ``uq_geoid_registry_geom_hash``, then inserts the ``place`` row ONLY
if the registry arbiter won. So an identical-geometry clash (catalog-wide) writes
no row and never aborts the transaction, while an external_id or geoid clash on
the ``place`` leg still raises 23505 (mapped to 409 by constraint name) and rolls
the whole statement — including the registry row — back. When the place row is
not written we look up the incumbent geoid in ``geoid_registry`` using the SAME
``geoid_geom_hash_default`` wrapper, so the stored and recomputed hashes can never
drift; the service then raises ``GeometryConflictError`` (→ 409 + incumbent geoid).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.models import Place

# Geometry built identically everywhere: GeoJSON -> geometry, SRID pinned to 4326.
_GEOM_EXPR = "ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)"


@dataclass(frozen=True)
class InsertResult:
    geoid: uuid.UUID
    created: bool  # True = newly minted; False = an identical geometry already exists
    collection_slug: str | None = None  # the INCUMBENT's collection when created=False


_POLYGONAL = ("POLYGON", "MULTIPOLYGON")


async def geometry_invalid_reason(session: AsyncSession, geojson: str) -> str:
    """Human-readable reason a geometry failed the DB CHECK — ERROR PATH ONLY.

    Called only after an insert raised a 23514 check violation, so the geometry
    already parsed; this recovers ST_IsValidReason (and flags a non-polygon type)
    for the 422 body. Keeping it off the happy path is the write-path optimization.
    """
    stmt = text(
        f"SELECT GeometryType(g) AS t, ST_IsValidReason(g) AS reason "
        f"FROM (SELECT {_GEOM_EXPR} AS g) s"
    )
    row = (await session.execute(stmt, {"geojson": geojson})).mappings().first()
    if row is None:
        return "invalid geometry"
    if row["t"] not in _POLYGONAL:
        return f"unsupported geometry type: {row['t']}"
    return row["reason"] or "invalid geometry"


async def insert_place(
    session: AsyncSession,
    *,
    geoid: uuid.UUID,
    collection_id: uuid.UUID,
    geojson: str,
    external_id: str | None,
    provenance: dict[str, Any],
    originating_instance: str | None,
) -> InsertResult:
    """Insert a place; an identical geometry ANYWHERE in the catalog conflicts.

    Single-statement arbiter CTE: the ``arb`` leg inserts into ``geoid_registry``
    (the global dedup arbiter) ``ON CONFLICT ... DO NOTHING`` on the geom_hash
    UNIQUE, and the ``place`` leg inserts ``FROM arb`` — so the place row is
    written ONLY if the registry arbiter won. Both inserts are one statement /
    one transaction, so an external_id or CHECK failure on the place leg rolls the
    registry row back too (no orphan), and the AFTER INSERT trigger then appends
    change_log. A duplicate geometry returns no row (DO NOTHING, not a raised
    exception) — the hot dedup path never aborts the transaction.

    Race safety: Postgres speculative insertion makes a concurrent loser block on
    the winner's XID with its own transaction still healthy, and under READ
    COMMITTED the follow-up incumbent lookup takes a fresh snapshot that sees the
    winner's committed registry row — so every conflict resolves to a real
    incumbent geoid. This breaks if the isolation level is raised to REPEATABLE READ.

    Dual-violation precedence: when a submission duplicates BOTH the geometry and
    an existing external_id, the geometry arbiter wins — the registry leg runs
    first and DOES NOTHING, so the place leg inserts no row and its external_id
    index is never touched; the request yields the geometry 409 (incumbent geoid
    attached) rather than the external_id 409. Pinned by
    ``test_review_fixes.py::test_dual_geometry_and_external_id_duplicate_yields_geometry_409``.
    """
    insert_stmt = text(
        f"""
        WITH arb AS (
            INSERT INTO geoid_registry (geoid, place_id, collection_id, geom_hash)
            VALUES (
                :geoid, :geoid, :collection_id, geoid_geom_hash_default({_GEOM_EXPR})
            )
            ON CONFLICT ON CONSTRAINT uq_geoid_registry_geom_hash DO NOTHING
            RETURNING geoid
        )
        INSERT INTO place (
            id, collection_id, geom, external_id, provenance,
            originating_instance
        )
        SELECT
            :geoid, :collection_id, {_GEOM_EXPR}, :external_id,
            CAST(:provenance AS jsonb), :originating_instance
        FROM arb
        RETURNING id
        """
    )
    params = {
        "geoid": geoid,
        "collection_id": collection_id,
        "geojson": geojson,
        "external_id": external_id,
        "provenance": json.dumps(provenance),
        "originating_instance": originating_instance,
    }
    row = (await session.execute(insert_stmt, params)).first()
    if row is not None:
        return InsertResult(geoid=row[0], created=True)

    # The registry arbiter swallowed the insert -> resolve the incumbent
    # (catalog-wide) via the SAME geoid_geom_hash_default wrapper that computed
    # the stored hash, so the recomputed and stored hashes can't drift.
    lookup_stmt = text(
        f"""
        SELECT r.geoid, c.slug
          FROM geoid_registry r JOIN collection c ON c.id = r.collection_id
         WHERE r.geom_hash = geoid_geom_hash_default({_GEOM_EXPR})
        """
    )
    incumbent = (await session.execute(lookup_stmt, {"geojson": geojson})).first()
    if incumbent is None:
        raise RuntimeError("dedup conflict but incumbent geoid not found (recipe drift?)")
    return InsertResult(geoid=incumbent[0], created=False, collection_slug=incumbent[1])


# --- Read path --------------------------------------------------------------

_READ_COLUMNS = """
    p.id AS geoid,
    c.slug AS collection_slug,
    ST_AsGeoJSON(p.geom) AS geometry,
    p.external_id,
    p.provenance,
    p.created_at,
    p.predecessor_id,
    p.originating_instance
"""


async def get_by_geoid(session: AsyncSession, geoid: uuid.UUID) -> dict[str, Any] | None:
    stmt = text(
        f"""
        SELECT {_READ_COLUMNS}
        FROM place p JOIN collection c ON c.id = p.collection_id
        WHERE p.id = :geoid
        """
    )
    row = (await session.execute(stmt, {"geoid": geoid})).mappings().first()
    return dict(row) if row else None


async def get_by_external_id(
    session: AsyncSession, collection_id: uuid.UUID, external_id: str
) -> dict[str, Any] | None:
    stmt = text(
        f"""
        SELECT {_READ_COLUMNS}
        FROM place p JOIN collection c ON c.id = p.collection_id
        WHERE p.collection_id = :collection_id AND p.external_id = :external_id
        """
    )
    row = (
        (await session.execute(stmt, {"collection_id": collection_id, "external_id": external_id}))
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _base_item_query(collection_id: uuid.UUID) -> Select[Any]:
    return select(
        Place.id.label("geoid"),
        Place.external_id,
        Place.provenance,
        Place.created_at,
        Place.predecessor_id,
        Place.originating_instance,
        func.ST_AsGeoJSON(Place.geom).label("geometry"),
    ).where(Place.collection_id == collection_id)


def queryable_field_mapping() -> dict[str, Any]:
    """Map CQL2 queryable names to ORM columns (incl. geometry for spatial ops).

    This set IS the public filter contract: the queryables document advertises
    exactly these names with ``additionalProperties: false``, and anything else
    is rejected with 400 — a unit test pins the two in sync.
    """
    return {
        "geoid": Place.id,
        "external_id": Place.external_id,
        "created_at": Place.created_at,
        "geometry": Place.geom,
    }


async def list_items(
    session: AsyncSession,
    collection_id: uuid.UUID,
    *,
    cql_clause: Any | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, number_matched) for a collection with an optional CQL2 filter."""
    query = _base_item_query(collection_id)

    if cql_clause is not None:
        query = query.where(cql_clause)

    # COUNT(*) OVER () gives the full filtered total from the same snapshot as the
    # page, avoiding the count/data race a separate count query had.
    query = query.add_columns(func.count().over().label("total"))
    query = query.order_by(Place.created_at.asc(), Place.id.asc()).limit(limit).offset(offset)

    rows = [dict(m) for m in (await session.execute(query)).mappings().all()]
    number_matched = rows[0]["total"] if rows else 0
    for row in rows:
        row.pop("total")  # drop the synthetic window column
    return rows, number_matched


async def list_item_ids(
    session: AsyncSession, collection_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[uuid.UUID], int]:
    """1.2 slice: ids only, ordered, with a total count."""
    stmt = (
        select(Place.id)
        .where(Place.collection_id == collection_id)
        .order_by(Place.created_at.asc(), Place.id.asc())
        .limit(limit)
        .offset(offset)
    )
    ids = [r[0] for r in (await session.execute(stmt)).all()]
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(Place).where(Place.collection_id == collection_id)
            )
        ).scalar_one()
    )
    return ids, total


async def collection_extent(
    session: AsyncSession, collection_id: uuid.UUID
) -> tuple[float, float, float, float] | None:
    """The real CRS84 bbox over a collection's geometries, or None when empty."""
    stmt = text(
        """
        SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
        FROM (SELECT ST_Extent(geom) AS e FROM place WHERE collection_id = :c) s
        """
    )
    row = (await session.execute(stmt, {"c": collection_id})).first()
    if row is None or row[0] is None:
        return None
    return (float(row[0]), float(row[1]), float(row[2]), float(row[3]))


async def collection_extents(
    session: AsyncSession, collection_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[float, float, float, float]]:
    """Batch real extents for many collections in ONE query (avoids the N+1 on /collections)."""
    if not collection_ids:
        return {}
    stmt = text(
        """
        SELECT collection_id, ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
        FROM (
            SELECT collection_id, ST_Extent(geom) AS e
            FROM place
            WHERE collection_id = ANY(:ids)
            GROUP BY collection_id
        ) s
        """
    )
    rows = (await session.execute(stmt, {"ids": list(collection_ids)})).all()
    out: dict[uuid.UUID, tuple[float, float, float, float]] = {}
    for r in rows:
        if r[1] is not None:
            out[r[0]] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
    return out
