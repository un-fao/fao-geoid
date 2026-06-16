"""Bulk-ingest data access — set-based staging + the SAME arbiter CTE as the single row.

The whole bulk write is a thin set-based wrapper over ``place_repo.insert_place``:
features are validated row-by-row in Python, minted a UUIDv7 each, ``COPY``-ed into
a per-transaction TEMP staging table, and then a single arbiter CTE inserts the
``geoid_registry`` rows ``ON CONFLICT DO NOTHING`` and the ``place`` rows only for the
registry winners — byte-identical to the single-row hot path because both compute
the hash with ``geoid_geom_hash_default`` (never in Python), so the GEOS-3.11.4
golden-vector parity holds.

Partial success is the contract. The set-based ``INSERT INTO place`` aborts the WHOLE
statement on a 23505 (``ON CONFLICT DO NOTHING`` only covers the geometry hinge, not
the external_id UNIQUE) or a 23514 CHECK, so every per-row failure mode is screened
OUT of staging FIRST, as classify-and-``DELETE`` steps, before the arbiter ever runs:

1. geometry invalid / non-polygon  (would raise 23514)
2. external_id conflict — in-batch AND against existing rows  (would raise 23505)
3. intra-batch geometry twins  (the wCTE single snapshot means both would lose
   ``ON CONFLICT`` with no committed incumbent — so collapse to one up front)

Reports are built from RETURNING + a staging join taken AFTER the arbiter statement
(reads in the same transaction see the rows the arbiter just inserted), never from a
re-query inside the write CTE.

Staging columns are all types asyncpg binary-encodes natively (int4, uuid, text):
provenance is staged as ``text`` and cast to ``jsonb`` in the arbiter SELECT — asyncpg
has no binary encoder for ``jsonb``/PostGIS geometry, so neither is ever COPY-ed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _geom_from(col: str) -> str:
    """GeoJSON-text column -> EPSG:4326 geometry — the SINGLE source of the SRID+parse
    expression, pinned equal to the single-row path (``place_repo._GEOM_EXPR``) so bulk
    and single-row geom_hashes stay byte-identical."""
    return f"ST_SetSRID(ST_GeomFromGeoJSON({col}), 4326)"


# Aliased form for the arbiter's ``FROM {_STAGING} s``; bare form for the un-aliased
# screens. Both come from _geom_from so the SRID/parse can never drift between them.
_GEOM = _geom_from("s.geojson_text")
_GEOM_BARE = _geom_from("geojson_text")

_STAGING = "ingest_staging"

_STAGING_COLUMNS = ("row_no", "geoid", "geojson_text", "external_id", "provenance")


@dataclass(frozen=True)
class StagingRow:
    """One validated, minted feature on its way into staging."""

    row_no: int
    geoid: uuid.UUID
    geojson_text: str
    external_id: str | None
    provenance: dict[str, Any]


async def _asyncpg_connection(session: AsyncSession) -> Any:
    """The raw asyncpg connection bound to the session's current transaction.

    ``COPY`` must run on the same connection/transaction as the TEMP table DDL and
    the arbiter, so we reach through SQLAlchemy's adapter to the driver connection
    rather than opening a second one.
    """
    sa_conn = await session.connection()
    raw = await sa_conn.get_raw_connection()
    return raw.driver_connection


async def create_staging(session: AsyncSession) -> None:
    """Create the per-transaction TEMP staging table (dropped at COMMIT)."""
    await session.execute(
        text(
            f"""
            CREATE TEMP TABLE {_STAGING} (
                row_no        int  NOT NULL,
                geoid         uuid NOT NULL,
                geojson_text  text NOT NULL,
                external_id   text,
                provenance    text NOT NULL,
                -- Materialized ONCE by populate_geom_hashes (after the invalid-geometry
                -- screen) so the dominant geoid_geom_hash_default() runs once per row,
                -- not again in the twin screen, the arbiter, and the conflict join.
                geom_hash     bytea
            ) ON COMMIT DROP
            """
        )
    )


async def copy_into_staging(session: AsyncSession, rows: Sequence[StagingRow]) -> None:
    """Bulk-load staging via asyncpg ``COPY`` (text/native codecs only — no jsonb)."""
    if not rows:
        return
    records = [
        (r.row_no, r.geoid, r.geojson_text, r.external_id, json.dumps(r.provenance)) for r in rows
    ]
    conn = await _asyncpg_connection(session)
    await conn.copy_records_to_table(_STAGING, records=records, columns=list(_STAGING_COLUMNS))


async def reject_invalid_geometry(session: AsyncSession) -> list[dict[str, Any]]:
    """Delete + return staging rows whose geometry would fail the place CHECKs.

    Catches non-polygonal types and ``ST_IsValid`` failures (e.g. self-intersection)
    that the ``PlaceCreate`` schema does not — these would raise 23514 and abort the
    whole set-based insert. Returns ``[{row_no, detail}]``.
    """
    stmt = text(
        f"""
        DELETE FROM {_STAGING} s
        USING (
            SELECT row_no,
                   GeometryType(g) AS gtype,
                   ST_IsValid(g)   AS is_valid,
                   ST_IsValidReason(g) AS reason
            FROM (
                SELECT row_no, {_GEOM_BARE} AS g
                FROM {_STAGING}
            ) q
        ) c
        WHERE s.row_no = c.row_no
          AND (NOT c.is_valid OR c.gtype NOT IN ('POLYGON', 'MULTIPOLYGON'))
        RETURNING s.row_no,
            CASE WHEN c.gtype NOT IN ('POLYGON', 'MULTIPOLYGON')
                 THEN 'unsupported geometry type: ' || c.gtype
                 ELSE COALESCE(c.reason, 'invalid geometry') END AS detail
        """
    )
    return [dict(m) for m in (await session.execute(stmt)).mappings().all()]


async def populate_geom_hashes(session: AsyncSession) -> None:
    """Compute ``geoid_geom_hash_default`` ONCE per surviving staging row.

    Run AFTER the invalid-geometry screen (so every remaining geometry is a valid
    polygon and the hash is final) and BEFORE the twin screen / arbiter / conflict
    join, which then all read this materialized ``geom_hash`` instead of recomputing
    the catalog's most expensive per-row function (ST_MakeValid + ST_Normalize +
    ST_ReducePrecision + sha256) two more times per row.
    """
    await session.execute(
        text(f"UPDATE {_STAGING} SET geom_hash = geoid_geom_hash_default({_GEOM_BARE})")
    )


async def reject_external_id_conflicts(
    session: AsyncSession, collection_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Delete + return staging rows with a duplicate external_id (the #1 abort risk).

    Two screens, both 23505-equivalent: duplicates WITHIN the batch (keep the first
    occurrence) and duplicates against EXISTING place rows in the collection. The
    set-based place insert's ``ON CONFLICT DO NOTHING`` covers only the geometry
    hinge, so an unguarded external_id dup would abort the whole statement.
    """
    in_batch = text(
        f"""
        DELETE FROM {_STAGING} s
        USING (
            SELECT row_no,
                   row_number() OVER (PARTITION BY external_id ORDER BY row_no) AS rn
            FROM {_STAGING}
            WHERE external_id IS NOT NULL
        ) d
        WHERE s.row_no = d.row_no AND d.rn > 1
        RETURNING s.row_no, s.external_id,
                  'duplicate external_id within the batch' AS detail
        """
    )
    existing = text(
        f"""
        DELETE FROM {_STAGING} s
        WHERE s.external_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM place p
              WHERE p.collection_id = :collection_id AND p.external_id = s.external_id
          )
        RETURNING s.row_no, s.external_id,
                  'external_id already exists in the collection' AS detail
        """
    )
    rejects = [dict(m) for m in (await session.execute(in_batch)).mappings().all()]
    rejects += [
        dict(m)
        for m in (await session.execute(existing, {"collection_id": collection_id}))
        .mappings()
        .all()
    ]
    return rejects


async def reject_in_batch_geometry_twins(session: AsyncSession) -> list[dict[str, Any]]:
    """Delete + return staging rows that duplicate an earlier row's geometry IN-batch.

    Two identical geometries in one batch would both lose ``ON CONFLICT`` (no
    committed incumbent under the wCTE single snapshot), so collapse each hash group
    to its first row here. Returns ``[{row_no, external_id, winner_geoid}]`` — the
    winner is the surviving twin that proceeds to the arbiter.
    """
    stmt = text(
        f"""
        DELETE FROM {_STAGING} s
        USING (
            SELECT row_no,
                   first_value(geoid) OVER (PARTITION BY geom_hash ORDER BY row_no)
                       AS winner_geoid,
                   row_number() OVER (PARTITION BY geom_hash ORDER BY row_no) AS rn
            FROM {_STAGING}
        ) d
        WHERE s.row_no = d.row_no AND d.rn > 1
        RETURNING s.row_no, s.external_id, d.winner_geoid
        """
    )
    return [dict(m) for m in (await session.execute(stmt)).mappings().all()]


async def run_arbiter(
    session: AsyncSession,
    *,
    collection_id: uuid.UUID,
    originating_instance: str | None,
    batch_id: uuid.UUID,
) -> list[uuid.UUID]:
    """The set-based arbiter: the SAME CTE as ``place_repo.insert_place``, over staging.

    Inserts ``geoid_registry`` rows ``ON CONFLICT DO NOTHING`` on the geometry UNIQUE,
    then ``place`` rows only for the registry winners. Returns the accepted geoids
    (place ids). Staging rows whose geometry already exists in the catalog win no
    registry row and write no place row — they are geometry conflicts, reported via
    :func:`geometry_conflicts`.
    """
    stmt = text(
        f"""
        WITH src AS (
            SELECT row_no, geoid,
                   {_GEOM} AS g,
                   geom_hash,
                   external_id,
                   CAST(provenance AS jsonb) AS provenance
            FROM {_STAGING} s
        ),
        arb AS (
            INSERT INTO geoid_registry (geoid, place_id, collection_id, geom_hash)
            SELECT geoid, geoid, :collection_id, geom_hash
            FROM src
            ON CONFLICT ON CONSTRAINT uq_geoid_registry_geom_hash DO NOTHING
            RETURNING geoid
        )
        INSERT INTO place (
            id, collection_id, geom, external_id, provenance,
            originating_instance, ingest_batch_id
        )
        SELECT s.geoid, :collection_id, s.g, s.external_id, s.provenance,
               :originating_instance, :batch_id
        FROM src s JOIN arb a ON a.geoid = s.geoid
        RETURNING id
        """
    )
    params = {
        "collection_id": collection_id,
        "originating_instance": originating_instance,
        "batch_id": batch_id,
    }
    rows = (await session.execute(stmt, params)).all()
    return [r[0] for r in rows]


async def geometry_conflicts(session: AsyncSession) -> list[dict[str, Any]]:
    """Resolve incumbent geoids for every remaining staging row, in ONE join.

    Run AFTER the arbiter, in the same transaction. Each surviving staging row joins
    its geom_hash to exactly one ``geoid_registry`` row: its OWN, just-inserted row if
    it was accepted (``staged_geoid == incumbent_geoid``), or the pre-existing
    incumbent if it lost the arbiter (a geometry conflict). Callers filter out the
    accepted ones. Joins on the materialized staging ``geom_hash`` (the same value the
    arbiter inserted), so the lookup can never drift from the stored hash.
    """
    stmt = text(
        f"""
        SELECT s.row_no,
               s.geoid AS staged_geoid,
               s.external_id,
               r.geoid AS incumbent_geoid,
               c.slug  AS incumbent_collection
        FROM {_STAGING} s
        JOIN geoid_registry r ON r.geom_hash = s.geom_hash
        JOIN collection c ON c.id = r.collection_id
        """
    )
    return [dict(m) for m in (await session.execute(stmt)).mappings().all()]
