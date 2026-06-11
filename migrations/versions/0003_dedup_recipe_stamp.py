"""record the dedup-recipe version and the engine stack hashes were computed under

``geoid_geom_hash`` is GEOS-bound (``ST_ReducePrecision`` + ``ST_Normalize``), so
its output can drift across PostGIS/GEOS builds. Drift is survivable — the hash is
operational dedup, not identity (the geoid is UUIDv7; the hash never reaches the
``geoid_registry``/``change_log`` hinges) — but an operator must be able to answer
"which stack were the stored hashes computed under?" after an engine upgrade.
``dedup_recipe_stamp`` is that record:

- ``recipe_version``   version of the hash *recipe itself* (the SQL inside
  ``geoid_geom_hash``). 'v1' = sha256(ST_AsBinary(ST_Normalize(ST_ReducePrecision(
  ST_MakeValid(g), grid)), 'NDR')). It only changes when the recipe SQL changes —
  a deliberate, breaking event with its own golden-vector corpus.
- ``postgis_version`` / ``geos_version`` / ``postgis_full``   the *engine stack*
  the hashes were computed under (``postgis_lib_version()`` etc. at stamp time;
  ``postgis_full`` is the full forensic string). These change on instance
  upgrades/migrations while recipe_version stays 'v1'.
- ``stamped_by``   'migration:0003' for this initial stamp; 'rehash-script' for
  rows appended by ``scripts/rehash_geom_hashes.py`` after a re-hash (or a
  verified no-op) on a new stack.

Reading the table: the LATEST row is the stack the current ``place.geom_hash``
values are valid under. If the live ``postgis_geos_version()`` series differs from
it, run the golden-vector check (``scripts/dedup_vectors.py --check``) and, on
drift, the audited re-hash procedure (``scripts/rehash_geom_hashes.py``,
runbook: docs/DEPLOYMENT.md §14).

Design note (D1): this table is ops bookkeeping, not a data-integrity hinge — it
deliberately gets NO triggers (the global user-trigger inventory stays at 8, as
``scripts/bootstrap_db.py`` asserts) and NO ORM model. The migration-time INSERT
captures the live stack, asserting that every place row existing at upgrade time
was hashed under it.

Revision ID: 0003_dedup_recipe_stamp
Revises: 0002_paging_index
Create Date: 2026-06-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_dedup_recipe_stamp"
down_revision: str | None = "0002_paging_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dedup_recipe_stamp (
            id              bigserial PRIMARY KEY,
            recipe_version  text NOT NULL,
            postgis_version text NOT NULL,
            geos_version    text NOT NULL,
            postgis_full    text NOT NULL,
            stamped_by      text NOT NULL,
            note            text,
            stamped_at      timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        INSERT INTO dedup_recipe_stamp
            (recipe_version, postgis_version, geos_version, postgis_full,
             stamped_by, note)
        SELECT 'v1', postgis_lib_version(), postgis_geos_version(),
               postgis_full_version(), 'migration:0003',
               'initial stamp: every existing place.geom_hash was computed under this stack';
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dedup_recipe_stamp;")
