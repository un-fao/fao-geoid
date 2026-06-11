#!/usr/bin/env python3
"""Golden vectors for the dedup hash recipe (``geoid_geom_hash``, recipe v1).

The recipe is GEOS-bound (``ST_ReducePrecision`` + ``ST_Normalize``), so its
output can drift across PostGIS/GEOS builds. This module pins known WKT→sha256
digests — generated on the validated stack (amd64 ``postgis/postgis:17-3.5`` →
PostGIS 3.5.x / GEOS 3.9.x) — so any candidate engine can be checked for hash
parity BEFORE data is loaded. Consumed by ``scripts/bootstrap_db.py`` (the
"golden vectors" step) and standalone:

    uv run python scripts/dedup_vectors.py --check --dsn postgresql://...
    uv run python scripts/dedup_vectors.py --generate --dsn ... \\
        --out scripts/data/dedup_golden_vectors_v1.json

The check recomputes each digest with an INLINE copy of the recipe SQL, not the
deployed ``geoid_geom_hash()``: the bootstrap parity check runs before migrate,
so the function may not exist yet — only postgis + pgcrypto are needed. The
integration suite separately pins inline == ``geoid_geom_hash()``, tying the
deployed function to this corpus.

Vectors marked ``"strict": false`` are advisory-only: the ``ST_MakeValid`` leg
can drift without ever changing a STORED hash (stored rows are always valid via
the CHECK constraint), so MakeValid-only drift warns instead of failing.

Regenerating the corpus is a recipe-version event, never a casual fix —
``--generate`` refuses on a non-3.5/3.9 stack unless ``--force``.

Exit codes (CLI):
    0 ok · 1 step failed · 2 config/usage error · 3 strict digest drift
"""

from __future__ import annotations

import argparse
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "scripts" / "data" / "dedup_golden_vectors_v1.json"

RECIPE_VERSION = "v1"
# The stack the corpus digests were generated on (mirrors scripts/bootstrap_db.py).
EXPECTED_POSTGIS_SERIES = "3.5"
EXPECTED_GEOS_SERIES = "3.9"

# Inline copy of the geoid_geom_hash recipe (migrations/versions/0001_initial.py).
# Kept inline so the check works on a database that has postgis + pgcrypto but no
# GeoID schema yet. The integration suite pins this string == the deployed function.
RECIPE_SQL = (
    "SELECT encode(digest(ST_AsBinary(ST_Normalize(ST_ReducePrecision("
    "ST_MakeValid(ST_GeomFromText(%(wkt)s, 4326)), %(grid)s)), 'NDR'), 'sha256'), 'hex')"
)

# run_sql(query, params) -> first column of the first row. Injected so the same
# logic runs over psycopg (bootstrap/CLI) or any other scalar-returning executor.
RunSQL = Callable[..., str]


class ConfigError(Exception):
    """Bad or missing configuration — exit 2, nothing was touched."""


class StepError(Exception):
    """A check/generate step failed — exit 1; the message carries remediation hints."""


@dataclass(frozen=True)
class VectorCase:
    name: str
    wkt: str
    grid: float
    strict: bool = True
    same_as: str | None = None
    note: str | None = None


_SQUARE = "POLYGON((0 0,1 0,1 1,0 1,0 0))"

VECTOR_CASES: tuple[VectorCase, ...] = (
    VectorCase(
        name="baseline_unit_square",
        wkt=_SQUARE,
        grid=1e-7,
        note="canonical CCW unit square at the Release-1 default grid (~1cm/vertex)",
    ),
    VectorCase(
        name="ring_start_rotation",
        wkt="POLYGON((1 1,0 1,0 0,1 0,1 1))",
        grid=1e-7,
        same_as="baseline_unit_square",
        note="same ring, different start vertex — ST_Normalize collapses it",
    ),
    VectorCase(
        name="cw_winding",
        wkt="POLYGON((0 0,0 1,1 1,1 0,0 0))",
        grid=1e-7,
        same_as="baseline_unit_square",
        note="same ring, clockwise winding — ST_Normalize collapses it",
    ),
    VectorCase(
        name="hole_order_canonical",
        wkt="POLYGON((0 0,10 0,10 10,0 10,0 0),(2 2,3 2,3 3,2 3,2 2),(6 6,7 6,7 7,6 7,6 6))",
        grid=1e-7,
        note="two-hole polygon",
    ),
    VectorCase(
        name="hole_order_swapped",
        wkt="POLYGON((0 0,10 0,10 10,0 10,0 0),(6 6,7 6,7 7,6 7,6 6),(2 2,3 2,3 3,2 3,2 2))",
        grid=1e-7,
        same_as="hole_order_canonical",
        note="holes listed in the other order — ST_Normalize collapses it",
    ),
    VectorCase(
        name="multipart_order_canonical",
        wkt="MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)),((5 5,6 5,6 6,5 6,5 5)))",
        grid=1e-7,
        note="two-part multipolygon",
    ),
    VectorCase(
        name="multipart_order_swapped",
        wkt="MULTIPOLYGON(((5 5,6 5,6 6,5 6,5 5)),((0 0,1 0,1 1,0 1,0 0)))",
        grid=1e-7,
        same_as="multipart_order_canonical",
        note="parts listed in the other order — ST_Normalize collapses it",
    ),
    VectorCase(
        name="subgrid_jitter",
        wkt="POLYGON((0.00000001 0,1 0,1 1,0 1,0.00000001 0))",
        grid=1e-7,
        same_as="baseline_unit_square",
        note="1e-8 jitter, below the 1e-7 grid — ST_ReducePrecision absorbs it",
    ),
    VectorCase(
        name="collinear_extra_vertex",
        wkt="POLYGON((0 0,0.5 0,1 0,1 1,0 1,0 0))",
        grid=1e-7,
        note="baseline square plus a collinear midpoint vertex — deliberately its OWN "
        "digest: the recipe does not simplify, so an inserted vertex is detectable",
    ),
    VectorCase(
        name="supragrid_shift",
        wkt="POLYGON((0.001 0,1 0,1 1,0 1,0.001 0))",
        grid=1e-7,
        note="0.001-degree shift, far above the grid — a different place, own digest",
    ),
    VectorCase(
        name="cell_straddle_low",
        wkt="POLYGON((20.00000004 20,21 20,21 21,20 21,20.00000004 20))",
        grid=1e-7,
        note="x = 20 + 0.4 grid cells, snaps DOWN — with cell_straddle_high this pins "
        "the known cell-boundary caveat (the grid is absolute, not a radius)",
    ),
    VectorCase(
        name="cell_straddle_high",
        wkt="POLYGON((20.00000006 20,21 20,21 21,20 21,20.00000006 20))",
        grid=1e-7,
        note="x = 20 + 0.6 grid cells, snaps UP — differs from cell_straddle_low "
        "although the inputs are only 2e-8 apart",
    ),
    VectorCase(
        name="negative_coords",
        wkt="POLYGON((-10 -10,-9 -10,-9 -9,-10 -9,-10 -10))",
        grid=1e-7,
        note="negative coordinates (western/southern hemispheres)",
    ),
    VectorCase(
        name="high_precision_floats",
        wkt=(
            "POLYGON((12.3456789012345 -45.6789012345678,13.3456789012345 -45.6789012345678,"
            "13.3456789012345 -44.6789012345678,12.3456789012345 -44.6789012345678,"
            "12.3456789012345 -45.6789012345678))"
        ),
        grid=1e-7,
        note="15-significant-figure coordinates — exercises float round-tripping",
    ),
    VectorCase(
        name="grid9e5_baseline",
        wkt=_SQUARE,
        grid=9e-5,
        note="unit square at a coarse ~10m grid — exercises the recipe at a "
        "non-default gridsize (grid is a function parameter, not a constant)",
    ),
    VectorCase(
        name="grid9e5_jitter",
        wkt="POLYGON((0.000001 0,1 0,1 1,0 1,0.000001 0))",
        grid=9e-5,
        same_as="grid9e5_baseline",
        note="1e-6 jitter, below the 9e-5 grid — collapses to grid9e5_baseline",
    ),
    VectorCase(
        name="bowtie_makevalid",
        wkt="POLYGON((0 0,1 1,1 0,0 1,0 0))",
        grid=1e-7,
        strict=False,
        note="self-intersecting bowtie exercising the ST_MakeValid leg — ADVISORY only "
        "(D4): stored rows are always valid (CHECK constraint), so MakeValid drift "
        "can never change a stored hash",
    ),
)


@dataclass(frozen=True)
class VectorFailure:
    name: str
    expected: str
    actual: str


@dataclass(frozen=True)
class VectorReport:
    strict_failures: tuple[VectorFailure, ...]
    advisory_failures: tuple[VectorFailure, ...]
    passed: int


def compute_digest(run_sql: RunSQL, wkt: str, grid: float) -> str:
    return run_sql(RECIPE_SQL, {"wkt": wkt, "grid": grid})


def load_fixture(path: Path = FIXTURE_PATH) -> dict:
    try:
        fixture = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise StepError(
            f"golden-vector fixture missing: {path}\n"
            "  Run from a synced checkout — scripts/data/ ships with the repo."
        ) from exc
    if fixture.get("recipe_version") != RECIPE_VERSION:
        raise StepError(
            f"fixture {path.name} carries recipe_version "
            f"{fixture.get('recipe_version')!r}, this script checks {RECIPE_VERSION!r}"
        )
    return fixture


def check_vectors(run_sql: RunSQL, fixture: dict) -> VectorReport:
    """Recompute every pinned vector; classify mismatches strict vs advisory."""
    strict_failures: list[VectorFailure] = []
    advisory_failures: list[VectorFailure] = []
    passed = 0
    for vector in fixture["vectors"]:
        actual = compute_digest(run_sql, vector["wkt"], vector["grid"])
        if actual == vector["sha256"]:
            passed += 1
            continue
        failure = VectorFailure(vector["name"], vector["sha256"], actual)
        if vector.get("strict", True):
            strict_failures.append(failure)
        else:
            advisory_failures.append(failure)
    return VectorReport(tuple(strict_failures), tuple(advisory_failures), passed)


def _series(version: str) -> str:
    return ".".join(version.split(".")[:2])


def generate_fixture(run_sql: RunSQL, *, force: bool = False) -> dict:
    lib = run_sql("SELECT postgis_lib_version()")
    geos = run_sql("SELECT postgis_geos_version()")
    full = run_sql("SELECT postgis_full_version()")
    if not force and (
        _series(lib) != EXPECTED_POSTGIS_SERIES
        or _series(geos.split("-")[0]) != EXPECTED_GEOS_SERIES
    ):
        raise StepError(
            f"refusing to generate golden vectors on PostGIS {lib} / GEOS {geos}: the "
            f"corpus is pinned to {EXPECTED_POSTGIS_SERIES}.x / {EXPECTED_GEOS_SERIES}.x.\n"
            "  Regenerating on a different stack is a recipe-version event — pass "
            "--force only if that is intended."
        )
    digests = {case.name: compute_digest(run_sql, case.wkt, case.grid) for case in VECTOR_CASES}
    for case in VECTOR_CASES:
        if case.same_as and digests[case.name] != digests[case.same_as]:
            raise StepError(
                f"self-check failed: {case.name!r} should canonicalize identically to "
                f"{case.same_as!r} but did not — engine or corpus bug; not writing the fixture"
            )
    return {
        "recipe_version": RECIPE_VERSION,
        "generated_on": {
            "postgis_full_version": full,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "vectors": [
            {
                "name": case.name,
                "wkt": case.wkt,
                "grid": case.grid,
                "sha256": digests[case.name],
                "strict": case.strict,
                "same_as": case.same_as,
                "note": case.note,
            }
            for case in VECTOR_CASES
        ],
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check or (re)generate the dedup-hash golden-vector corpus."
    )
    parser.add_argument("--check", action="store_true",
                        help="recompute the pinned vectors against the live engine")
    parser.add_argument("--generate", action="store_true",
                        help="compute fresh digests and write the fixture (recipe-version event)")
    parser.add_argument("--dsn", required=True, help="libpq DSN/URL of the database to use")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH,
                        help="fixture to check against (default: the committed corpus)")
    parser.add_argument("--out", type=Path, default=FIXTURE_PATH,
                        help="where --generate writes the fixture")
    parser.add_argument("--force", action="store_true",
                        help="allow --generate on a non-validated PostGIS/GEOS series")
    args = parser.parse_args(argv)
    if args.check == args.generate:
        raise ConfigError("pass exactly one of --check / --generate")
    return args


def _psycopg_run_sql(conn) -> RunSQL:
    def run_sql(query: str, params: dict | None = None) -> str:
        return conn.execute(query, params).fetchone()[0]

    return run_sql


def main(argv: list[str] | None = None) -> int:
    import psycopg

    try:
        args = _parse_args(argv)
    except ConfigError as exc:
        print(f"✗ {exc}")
        return 2

    try:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            for extension in ("postgis", "pgcrypto"):
                # Best effort — an unprivileged role can still check as long as
                # the extensions are already installed.
                with contextlib.suppress(psycopg.Error):
                    conn.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            run_sql = _psycopg_run_sql(conn)

            if args.generate:
                fixture = generate_fixture(run_sql, force=args.force)
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(fixture, indent=2) + "\n")
                print(f"✓ wrote {len(fixture['vectors'])} vectors to {args.out}")
                print(f"  {fixture['generated_on']['postgis_full_version']}")
                return 0

            fixture = load_fixture(args.fixture)
            report = check_vectors(run_sql, fixture)
            for failure in report.advisory_failures:
                print(f"⚠ advisory vector {failure.name!r} drifted "
                      f"(expected {failure.expected}, got {failure.actual})")
            for failure in report.strict_failures:
                print(f"✗ STRICT vector {failure.name!r} drifted "
                      f"(expected {failure.expected}, got {failure.actual})")
            print(f"{report.passed}/{len(fixture['vectors'])} vectors match")
            return 3 if report.strict_failures else 0
    except StepError as exc:
        print(f"✗ {exc}")
        return 1
    except psycopg.Error as exc:
        print(f"✗ database error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
