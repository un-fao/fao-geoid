"""retune the default dedup grid from 9e-5 (~10m) to 1e-7 (~1cm / vertex)

Remi's final security ruling settled geometry dedup as EXACT MATCH only: the grid
exists to neutralize float jitter, not to merge nearby shapes. 1e-7 deg ≈ 1cm per
vertex is jitter-immunity-only — submissions differing by anything a human would
call "a different polygon" mint distinct geoids. This reverses the interim ~10m
retune (0003), which predated the ruling.

Four steps, one transaction. The trigger-disable ALTER takes an ACCESS EXCLUSIVE
lock on ``collection``, so in-flight place inserts (whose BEFORE-INSERT trigger
reads collection.metadata) serialize against this migration instead of racing the
stamp updates:

1. Re-issue ``place_set_geom_hash()`` with the 1e-7 fallback so the SQL and
   Python (``GEOID_DEDUP_GRID_DEFAULT``) defaults agree.
2. Stamp UNSTAMPED **populated** collections at 9e-5 — the fallback their rows
   were hashed under at revision 0003 — so step 1 cannot silently change their
   effective grid and re-mint geoids for already-registered geometries. The
   dedup-grid freeze trigger exists to guard exactly that invariant but only
   fires on metadata UPDATEs; it is disabled around this one backfill because
   the backfill *preserves* each collection's effective grid.
3. Stamp UNSTAMPED **empty** collections at 1e-7 (no hashes to preserve).
4. Restamp default-stamped (= 9e-5) **empty** collections to 1e-7. The cast is
   wrapped in a jsonb_typeof CASE (CASE guarantees evaluation order; bare WHERE
   conjunctions do not), so a historical non-numeric dedup_grid value — now
   rejected at the API boundary — can never abort the upgrade. Populated stamped
   collections are deliberately untouched. An explicit admin-chosen 9e-5 on an
   empty collection is indistinguishable from the stamped default and moves with
   it (acknowledged: no Release-1 production data exists).

``downgrade()`` re-issues the function with the 9e-5 fallback only — a true
mirror of 0003's pattern. It deliberately does NOT restamp: by downgrade time,
stamps may encode explicit admin choices that a value-equality rewrite would
silently destroy.

Revision ID: 0004_dedup_grid_default_1cm
Revises: 0003_dedup_grid_default_10m
Create Date: 2026-06-09
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_dedup_grid_default_1cm"
down_revision: str | None = "0003_dedup_grid_default_10m"
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


def _stamp_unstamped(value: str, *, populated: bool) -> str:
    """Stamp collections whose dedup_grid is absent or JSON null (``->>`` yields
    SQL NULL for both). Non-numeric junk values are NOT matched here — they are
    left for step 4's typeof guard to skip."""
    exists = "EXISTS" if populated else "NOT EXISTS"
    return f"""
        UPDATE collection c
           SET metadata = jsonb_set(
                   c.metadata, '{{dedup_grid}}', to_jsonb({value}::double precision)
               )
         WHERE c.metadata->>'dedup_grid' IS NULL
           AND {exists} (SELECT 1 FROM place p WHERE p.collection_id = c.id);
    """


# CASE (not a bare AND) so the ::double precision cast provably never runs on a
# non-numeric value — SQL gives no evaluation-order guarantee for conjunctions.
_RESTAMP_EMPTY_DEFAULTS = """
    UPDATE collection c
       SET metadata = jsonb_set(
               c.metadata, '{dedup_grid}', to_jsonb(1e-7::double precision)
           )
     WHERE CASE WHEN jsonb_typeof(c.metadata->'dedup_grid') = 'number'
                THEN (c.metadata->>'dedup_grid')::double precision
                     = 9e-5::double precision
                ELSE false
           END
       AND NOT EXISTS (SELECT 1 FROM place p WHERE p.collection_id = c.id);
"""


def upgrade() -> None:
    op.execute(_set_geom_hash_fn("1e-7"))
    op.execute("ALTER TABLE collection DISABLE TRIGGER collection_guard_dedup_grid_bu;")
    op.execute(_stamp_unstamped("9e-5", populated=True))
    op.execute("ALTER TABLE collection ENABLE TRIGGER collection_guard_dedup_grid_bu;")
    op.execute(_stamp_unstamped("1e-7", populated=False))
    op.execute(_RESTAMP_EMPTY_DEFAULTS)


def downgrade() -> None:
    op.execute(_set_geom_hash_fn("9e-5"))
