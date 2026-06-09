"""Place data access — the dedup insert and the OGC read queries.

The insert is the product's hot path. ``geom_hash`` is computed by the BEFORE
INSERT trigger; we dedup with ``ON CONFLICT ON CONSTRAINT
uq_place_collection_geom_hash DO NOTHING`` so that ONLY an identical-geometry
clash is swallowed — an external_id or geoid clash still raises 23505 and is
mapped to 409. When the insert is swallowed we look up the incumbent geoid using
the SAME ``geoid_geom_hash`` SQL function the trigger uses, so the two can never
drift.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.models import Place

# Geometry built identically everywhere: GeoJSON -> geometry, SRID pinned to 4326.
_GEOM_EXPR = "ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)"


@dataclass(frozen=True)
class InsertResult:
    geoid: uuid.UUID
    created: bool  # True = newly minted; False = deduplicated to an incumbent


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


async def insert_with_dedup(
    session: AsyncSession,
    *,
    geoid: uuid.UUID,
    collection_id: uuid.UUID,
    geojson: str,
    external_id: str | None,
    provenance: dict[str, Any],
    originating_instance: str | None,
    dedup_grid_default: float,
    data_quality_status: str = "unverified",
) -> InsertResult:
    """Insert a place, deduplicating on identical geometry within the collection."""
    insert_stmt = text(
        f"""
        INSERT INTO place (
            id, collection_id, geom, external_id, provenance,
            originating_instance, data_quality_status
        )
        VALUES (
            :geoid, :collection_id, {_GEOM_EXPR}, :external_id,
            CAST(:provenance AS jsonb), :originating_instance, :data_quality_status
        )
        ON CONFLICT ON CONSTRAINT uq_place_collection_geom_hash DO NOTHING
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
        "data_quality_status": data_quality_status,
    }
    row = (await session.execute(insert_stmt, params)).first()
    if row is not None:
        return InsertResult(geoid=row[0], created=True)

    # Swallowed by the geom_hash dedup -> resolve the incumbent via the SAME recipe.
    # The grid fallback (:grid_default) must match the BEFORE-INSERT trigger's so the
    # recomputed hash can't drift from the stored one; both default to the configured
    # GEOID_DEDUP_GRID_DEFAULT when a collection carries no explicit dedup_grid.
    lookup_stmt = text(
        f"""
        SELECT id FROM place
        WHERE collection_id = :collection_id
          AND geom_hash = geoid_geom_hash(
                {_GEOM_EXPR},
                COALESCE(
                    (SELECT (metadata->>'dedup_grid')::double precision
                       FROM collection WHERE id = :collection_id),
                    :grid_default
                )
          )
        """
    )
    incumbent = (
        await session.execute(
            lookup_stmt,
            {
                "collection_id": collection_id,
                "geojson": geojson,
                "grid_default": dedup_grid_default,
            },
        )
    ).first()
    if incumbent is None:
        raise RuntimeError("dedup conflict but incumbent geoid not found (recipe drift?)")
    return InsertResult(geoid=incumbent[0], created=False)


# --- Read path --------------------------------------------------------------

_READ_COLUMNS = """
    p.id AS geoid,
    c.slug AS collection_slug,
    ST_AsGeoJSON(p.geom) AS geometry,
    p.external_id,
    p.provenance,
    p.data_quality_status,
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
        await session.execute(
            stmt, {"collection_id": collection_id, "external_id": external_id}
        )
    ).mappings().first()
    return dict(row) if row else None


def _base_item_query(collection_id: uuid.UUID) -> Select[Any]:
    return select(
        Place.id.label("geoid"),
        Place.external_id,
        Place.provenance,
        Place.data_quality_status,
        Place.created_at,
        Place.predecessor_id,
        Place.originating_instance,
        func.ST_AsGeoJSON(Place.geom).label("geometry"),
    ).where(Place.collection_id == collection_id)


def _bbox_predicate(bbox: tuple[float, float, float, float]) -> ColumnElement[bool]:
    """ST_Intersects predicate for a CRS84 bbox, handling antimeridian crossing.

    A normal bbox is one envelope. A crossing bbox (west minx > east maxx) is the
    union of [minx,180] and [-180,maxx], since a single ST_MakeEnvelope cannot
    wrap the antimeridian.
    """
    minx, miny, maxx, maxy = bbox
    if minx <= maxx:
        return func.ST_Intersects(Place.geom, func.ST_MakeEnvelope(minx, miny, maxx, maxy, 4326))
    west = func.ST_Intersects(Place.geom, func.ST_MakeEnvelope(minx, miny, 180.0, maxy, 4326))
    east = func.ST_Intersects(Place.geom, func.ST_MakeEnvelope(-180.0, miny, maxx, maxy, 4326))
    return or_(west, east)


def queryable_field_mapping() -> dict[str, Any]:
    """Map CQL2 queryable names to ORM columns (incl. geometry for spatial ops)."""
    return {
        "geoid": Place.id,
        "external_id": Place.external_id,
        "data_quality_status": Place.data_quality_status,
        "created_at": Place.created_at,
        "geometry": Place.geom,
        "geom": Place.geom,
    }


async def list_items(
    session: AsyncSession,
    collection_id: uuid.UUID,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    cql_clause: Any | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, number_matched) for a collection with optional bbox + CQL2."""
    query = _base_item_query(collection_id)

    if bbox is not None:
        query = query.where(_bbox_predicate(bbox))

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
                select(func.count()).select_from(Place).where(
                    Place.collection_id == collection_id
                )
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


async def iter_collection_geojson(
    session: AsyncSession, collection_id: uuid.UUID
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield (geoid, geometry_geojson, external_id, provenance) rows for bulk export."""
    stmt = text(
        """
        SELECT p.id AS geoid,
               ST_AsGeoJSON(p.geom) AS geometry,
               p.external_id,
               p.provenance,
               p.data_quality_status,
               p.created_at
        FROM place p
        WHERE p.collection_id = :collection_id
        ORDER BY p.created_at ASC, p.id ASC
        """
    )
    # stream_results -> asyncpg uses a server-side cursor (a portal), so memory
    # stays flat and bytes start flowing immediately instead of buffering the
    # whole collection first.
    streamed = stmt.execution_options(stream_results=True, max_row_buffer=500)
    result = await session.stream(streamed, {"collection_id": collection_id})
    async for row in result.mappings():
        yield dict(row)
