"""scripts/rehash_geom_hashes.py against a real PostGIS — the recovery rehearsal.

Engine drift cannot be produced inside one container, so the helpers simulate it
the other way around: doctor ``geoid_geom_hash`` to a "drifted" variant (raw WKB
digest — no MakeValid/ReducePrecision/Normalize), insert places so their stored
hashes are computed under the doctored recipe, then restore the canonical body
(from migration 0001) and run the script as an operator would (subprocess with
GEOID_DATABASE_URL). The stored hashes now LOOK like hashes from a different
engine build, which is exactly the state the script exists to repair.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rehash_geom_hashes.py"

# The canonical recipe body, verbatim from migrations/versions/0001_initial.py.
CANONICAL_FN = """
    CREATE OR REPLACE FUNCTION geoid_geom_hash(g geometry, grid double precision)
    RETURNS bytea LANGUAGE sql IMMUTABLE STRICT AS $func$
        SELECT digest(
            ST_AsBinary(
                ST_Normalize(ST_ReducePrecision(ST_MakeValid(g), grid)),
                'NDR'
            ),
            'sha256'
        );
    $func$;
"""

# "Drifted engine" stand-in: hashes the raw geometry, so sub-grid jitter and
# ring permutations do NOT collapse — values differ from the canonical recipe.
DRIFTED_FN = """
    CREATE OR REPLACE FUNCTION geoid_geom_hash(g geometry, grid double precision)
    RETURNS bytea LANGUAGE sql IMMUTABLE STRICT AS $func$
        SELECT digest(ST_AsBinary(g, 'NDR'), 'sha256');
    $func$;
"""

SQUARE = "POLYGON((10 10,11 10,11 11,10 11,10 10))"
SQUARE_JITTERED = "POLYGON((10.00000001 10,11 10,11 11,10 11,10.00000001 10))"
OTHER_SQUARE = "POLYGON((30 30,31 30,31 31,30 31,30 30))"


def _place_id(n: int) -> str:
    # Ordered ids standing in for UUIDv7 mint order (the planner relies on order).
    return str(uuid.UUID(int=n))


@pytest.fixture
def db(_migrated):
    """Sync admin connection to the shared migrated DB; clean tables, and restore
    the canonical hash function + trigger state even if a test fails mid-doctor."""
    import psycopg

    dsn = _migrated.replace("postgresql+asyncpg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE place, geoid_registry, change_log, collection, catalog "
            "RESTART IDENTITY CASCADE"
        )
        conn.execute("SET session_replication_role = origin")
        try:
            yield conn
        finally:
            conn.execute(CANONICAL_FN)
            conn.execute("ALTER TABLE place ENABLE TRIGGER place_block_mutation_bud")


def _make_collection(db, slug: str) -> str:
    catalog_id, collection_id = str(uuid.uuid4()), str(uuid.uuid4())
    db.execute("INSERT INTO catalog (id, slug) VALUES (%s, %s)", (catalog_id, f"cat-{slug}"))
    db.execute(
        "INSERT INTO collection (id, catalog_id, slug) VALUES (%s, %s, %s)",
        (collection_id, catalog_id, slug),
    )
    return collection_id


def _insert_place(db, place_id: str, collection_id: str, wkt: str) -> None:
    # geom_hash is computed by the BEFORE INSERT trigger — under whatever
    # geoid_geom_hash body is currently installed.
    db.execute(
        "INSERT INTO place (id, collection_id, geom) VALUES (%s, %s, ST_GeomFromText(%s, 4326))",
        (place_id, collection_id, wkt),
    )


def _stored_hash(db, place_id: str) -> str:
    return db.execute(
        "SELECT encode(geom_hash, 'hex') FROM place WHERE id = %s", (place_id,)
    ).fetchone()[0]


def _canonical_hash(db, place_id: str, grid: float) -> str:
    return db.execute(
        "SELECT encode(geoid_geom_hash(geom, %s), 'hex') FROM place WHERE id = %s",
        (grid, place_id),
    ).fetchone()[0]


def _latest_stamp(db):
    return db.execute(
        "SELECT stamped_by, note FROM dedup_recipe_stamp ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _stamp_count(db) -> int:
    return db.execute("SELECT count(*) FROM dedup_recipe_stamp").fetchone()[0]


def _run_rehash(url: str, *flags: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GEOID_")}
    env["GEOID_DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--yes", *flags],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_rehash_recomputes_drifted_hashes_and_stamps(db, _migrated):
    db.execute(DRIFTED_FN)
    collection_id = _make_collection(db, "drifted")
    _insert_place(db, _place_id(1), collection_id, SQUARE)
    _insert_place(db, _place_id(2), collection_id, OTHER_SQUARE)
    before = {n: _stored_hash(db, _place_id(n)) for n in (1, 2)}
    db.execute(CANONICAL_FN)

    result = _run_rehash(_migrated)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 to update" in result.stdout

    for n in (1, 2):
        after = _stored_hash(db, _place_id(n))
        assert after != before[n]
        assert after == _canonical_hash(db, _place_id(n), 1e-7)
    stamped_by, note = _latest_stamp(db)
    assert stamped_by == "rehash-script"
    assert note == "updated 2, skipped 0, unchanged 0"


def test_dry_run_mutates_nothing(db, _migrated):
    db.execute(DRIFTED_FN)
    collection_id = _make_collection(db, "dry")
    _insert_place(db, _place_id(1), collection_id, SQUARE)
    before = _stored_hash(db, _place_id(1))
    stamps_before = _stamp_count(db)
    db.execute(CANONICAL_FN)

    result = _run_rehash(_migrated, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 to update" in result.stdout
    assert "nothing mutated" in result.stdout
    assert _stored_hash(db, _place_id(1)) == before
    assert _stamp_count(db) == stamps_before


def test_collision_keeps_loser_reports_pair_and_leaves_hinges_alone(db, _migrated):
    # Under the doctored recipe the jittered square hashes differently, so both
    # rows insert; under the canonical recipe they collide — a discovered duplicate.
    db.execute(DRIFTED_FN)
    collection_id = _make_collection(db, "collide")
    _insert_place(db, _place_id(1), collection_id, SQUARE)
    _insert_place(db, _place_id(2), collection_id, SQUARE_JITTERED)
    loser_hash_before = _stored_hash(db, _place_id(2))
    hinges_before = (
        db.execute("SELECT count(*) FROM geoid_registry").fetchone()[0],
        db.execute("SELECT count(*) FROM change_log").fetchone()[0],
    )
    db.execute(CANONICAL_FN)

    result = _run_rehash(_migrated)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "discovered-duplicate" in result.stdout
    assert _place_id(1) in result.stdout and _place_id(2) in result.stdout

    # Earliest id won the canonical hash; the loser retains its old hash.
    assert _stored_hash(db, _place_id(1)) == _canonical_hash(db, _place_id(1), 1e-7)
    assert _stored_hash(db, _place_id(2)) == loser_hash_before
    assert db.execute("SELECT count(*) FROM place").fetchone()[0] == 2  # never delete
    hinges_after = (
        db.execute("SELECT count(*) FROM geoid_registry").fetchone()[0],
        db.execute("SELECT count(*) FROM change_log").fetchone()[0],
    )
    assert hinges_after == hinges_before
    assert _latest_stamp(db)[1] == "updated 1, skipped 1, unchanged 0"


def test_cross_collection_collision_is_global_exit_3_both_rows_survive(db, _migrated):
    # Geometry uniqueness is GLOBAL: the same canonical geometry in TWO
    # different collections is a discovered duplicate. The script reports the
    # pair (exit 3) and never deletes either row.
    db.execute(DRIFTED_FN)
    coll_a = _make_collection(db, "xcoll-a")
    coll_b = _make_collection(db, "xcoll-b")
    _insert_place(db, _place_id(1), coll_a, SQUARE)
    _insert_place(db, _place_id(2), coll_b, SQUARE_JITTERED)
    loser_hash_before = _stored_hash(db, _place_id(2))
    db.execute(CANONICAL_FN)

    result = _run_rehash(_migrated)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "discovered-duplicate" in result.stdout
    assert _place_id(1) in result.stdout and _place_id(2) in result.stdout

    # Earliest id won the canonical hash; the loser retains its old hash.
    assert _stored_hash(db, _place_id(1)) == _canonical_hash(db, _place_id(1), 1e-7)
    assert _stored_hash(db, _place_id(2)) == loser_hash_before
    assert db.execute("SELECT count(*) FROM place").fetchone()[0] == 2  # never delete


def test_place_is_still_immutable_after_rehash(db, _migrated):
    import psycopg

    db.execute(DRIFTED_FN)
    collection_id = _make_collection(db, "immutable")
    _insert_place(db, _place_id(1), collection_id, SQUARE)
    db.execute(CANONICAL_FN)
    assert _run_rehash(_migrated).returncode == 0

    enabled = db.execute(
        "SELECT tgenabled FROM pg_trigger WHERE tgname = 'place_block_mutation_bud'"
    ).fetchone()[0]
    assert enabled == "O"  # 'O' = enabled (origin)
    with pytest.raises(psycopg.errors.RestrictViolation, match="immutable"):
        db.execute("UPDATE place SET external_id = 'nope' WHERE id = %s", (_place_id(1),))


def test_noop_run_still_appends_a_stamp(db, _migrated):
    collection_id = _make_collection(db, "noop")
    _insert_place(db, _place_id(1), collection_id, SQUARE)  # canonical fn → no drift
    stamps_before = _stamp_count(db)

    result = _run_rehash(_migrated)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 to update" in result.stdout
    assert _stamp_count(db) == stamps_before + 1
    stamped_by, note = _latest_stamp(db)
    assert stamped_by == "rehash-script"
    assert note == "updated 0, skipped 0, unchanged 1"
