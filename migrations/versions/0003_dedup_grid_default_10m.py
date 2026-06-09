"""retune the default dedup grid from 1e-7 (~1cm) to 9e-5 (~10m / vertex)

The Release-1 thread settled coordinate precision as configurable, default ~10m
(0.00009°/vertex). The per-collection grid lives in `collection.metadata->>'dedup_grid'`
and is now *stamped* at collection creation from `GEOID_DEDUP_GRID_DEFAULT`, so the
value read by both the trigger and the incumbent-lookup is the source of truth. This
migration only retunes the FALLBACK baked into `place_set_geom_hash()` (used when a
collection carries no explicit grid) from 1e-7 to 9e-5 so the SQL and Python defaults
agree. The trigger itself references the function by name and is left untouched.

Safe to apply unconditionally in Release-1: no geometries exist yet, so no stored
`geom_hash` can drift. The recipe (`geoid_geom_hash`) is unchanged.

Revision ID: 0003_dedup_grid_default_10m
Revises: 0002_paging_index
Create Date: 2026-06-08
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_dedup_grid_default_10m"
down_revision: str | None = "0002_paging_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_geom_hash_fn(fallback: str) -> str:
    """The BEFORE-INSERT geom_hash trigger function with a given default grid."""
    return f"""
        CREATE OR REPLACE FUNCTION place_set_geom_hash()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $func$
        DECLARE
            v_grid double precision;
        BEGIN
            SELECT COALESCE((c.metadata->>'dedup_grid')::double precision, {fallback})
              INTO v_grid
              FROM collection c
             WHERE c.id = NEW.collection_id;
            IF v_grid IS NULL THEN
                v_grid := {fallback};
            END IF;
            NEW.geom_hash := geoid_geom_hash(NEW.geom, v_grid);
            RETURN NEW;
        END;
        $func$;
    """


def upgrade() -> None:
    op.execute(_set_geom_hash_fn("9e-5"))


def downgrade() -> None:
    op.execute(_set_geom_hash_fn("1e-7"))
