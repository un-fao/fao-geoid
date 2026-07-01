"""The golden-vector corpus against a real PostGIS — pins ``geoid_geom_hash``
output to exact digests.

The committed fixture (scripts/data/dedup_golden_vectors_v1.json) is pinned to the
Cloud SQL production stack (PostGIS 3.6.x / GEOS 3.11.x) — the system of record.
The CI container is postgis/postgis:17-3.5 (GEOS 3.9.0), a DIFFERENT GEOS build
(no stock image ships Google's GEOS 3.11.4), so it reproduces every STRICT
(GEOS-stable) digest bit-for-bit but NOT the advisory GEOS-build-sensitive vectors
(cell_straddle_high, grid9e5_*); those are validated against the real target via
scripts/dedup_vectors.py --check at bootstrap. This is the only place engine-build
hash drift on the STABLE vectors becomes visible before data is loaded — the
relational suite (test_dedup_recipe.py) would pass on a drifted engine as long as
it drifts consistently.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "scripts" / "data" / "dedup_golden_vectors_v1.json"

_FIXTURE = json.loads(FIXTURE_PATH.read_text())
_VECTORS = _FIXTURE["vectors"]
_STRICT = [vector for vector in _VECTORS if vector["strict"]]
_ADVISORY = [vector for vector in _VECTORS if not vector["strict"]]


def _load_dedup_vectors():
    spec = importlib.util.spec_from_file_location(
        "dedup_vectors", ROOT / "scripts" / "dedup_vectors.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["dedup_vectors"] = module
    spec.loader.exec_module(module)
    return module


dedup_vectors = _load_dedup_vectors()

# The module's inline recipe (pyformat params for psycopg) as a SQLAlchemy stmt —
# same SQL string, so this file pins the inline copy to the deployed function (D3).
_INLINE_SQL = text(dedup_vectors.RECIPE_SQL.replace("%(wkt)s", ":wkt").replace("%(grid)s", ":grid"))
_FUNCTION_SQL = text("SELECT encode(geoid_geom_hash(ST_GeomFromText(:wkt, 4326), :grid), 'hex')")


async def _digest(session, stmt, wkt: str, grid: float) -> str:
    return (await session.execute(stmt, {"wkt": wkt, "grid": grid})).scalar_one()


@pytest.fixture
def sync_conn(_migrated):
    import psycopg

    dsn = _migrated.replace("postgresql+asyncpg://", "postgresql://")
    with psycopg.connect(dsn) as conn:
        yield conn


@pytest.mark.parametrize("vector", _STRICT, ids=lambda vector: vector["name"])
async def test_function_digest_matches_pinned_vector(session, vector):
    actual = await _digest(session, _FUNCTION_SQL, vector["wkt"], vector["grid"])
    assert actual == vector["sha256"], (
        f"geoid_geom_hash drifted from the pinned corpus on {vector['name']!r} — "
        "engine stack or recipe changed"
    )


async def test_inline_recipe_matches_deployed_function_for_every_vector(session):
    # D3: the bootstrap check uses the inline copy; this pins it to the function.
    for vector in _VECTORS:
        inline = await _digest(session, _INLINE_SQL, vector["wkt"], vector["grid"])
        deployed = await _digest(session, _FUNCTION_SQL, vector["wkt"], vector["grid"])
        assert inline == deployed, vector["name"]


def test_check_vectors_strict_pass_advisory_may_drift_on_ci_container(sync_conn):
    # The corpus is pinned to the Cloud SQL production stack (GEOS 3.11.x); this CI
    # container is GEOS 3.9.0. STRICT (GEOS-stable) vectors must still match
    # bit-for-bit; the GEOS-build-sensitive advisory vectors are allowed to drift
    # here (they are validated against the real target via --check at bootstrap).
    run_sql = dedup_vectors._psycopg_run_sql(sync_conn)
    report = dedup_vectors.check_vectors(run_sql, dedup_vectors.load_fixture())
    assert report.strict_failures == ()
    assert report.passed >= len(_STRICT)
    drifted = {failure.name for failure in report.advisory_failures}
    # The allowed-drift set IS the fixture's advisory set — derived, not hardcoded,
    # so re-classifying a vector in the corpus automatically updates this gate.
    assert drifted <= {vector["name"] for vector in _ADVISORY}


def test_advisory_drift_is_never_classified_strict(sync_conn):
    # Corrupt only the advisory digests: the report must route them to the
    # advisory bucket (bootstrap warns) and keep strict_failures empty (exit 0).
    corrupted = {
        "recipe_version": _FIXTURE["recipe_version"],
        "vectors": [
            {**vector, "sha256": "0" * 64} if not vector["strict"] else vector
            for vector in _VECTORS
        ],
    }
    run_sql = dedup_vectors._psycopg_run_sql(sync_conn)
    report = dedup_vectors.check_vectors(run_sql, corrupted)
    assert report.strict_failures == ()
    assert {failure.name for failure in report.advisory_failures} == {
        vector["name"] for vector in _ADVISORY
    }
