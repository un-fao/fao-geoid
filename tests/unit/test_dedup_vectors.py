"""Unit tests for scripts/dedup_vectors.py — committed-fixture well-formedness
and the check/generate logic with a fake run_sql (no DB)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "dedup_vectors", ROOT / "scripts" / "dedup_vectors.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the dataclass decorator resolves the module's string
    # annotations through sys.modules[cls.__module__].
    sys.modules["dedup_vectors"] = module
    spec.loader.exec_module(module)
    return module


dedup_vectors = _load_script_module()
FIXTURE = dedup_vectors.load_fixture()
VECTORS = FIXTURE["vectors"]
BY_NAME = {vector["name"]: vector for vector in VECTORS}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --- committed fixture well-formedness -----------------------------------------


def test_fixture_is_recipe_v1_with_provenance():
    assert FIXTURE["recipe_version"] == "v1"
    assert "POSTGIS=" in FIXTURE["generated_on"]["postgis_full_version"]
    assert FIXTURE["generated_on"]["generated_at"]


def test_vector_names_are_unique():
    names = [vector["name"] for vector in VECTORS]
    assert len(names) == len(set(names))


def test_digests_are_64_char_lowercase_hex():
    for vector in VECTORS:
        assert _HEX64.match(vector["sha256"]), vector["name"]


def test_same_as_references_resolve():
    for vector in VECTORS:
        if vector["same_as"] is not None:
            assert vector["same_as"] in BY_NAME, vector["name"]
            assert BY_NAME[vector["same_as"]]["same_as"] is None  # one level deep


def test_corpus_coverage():
    strict = [vector for vector in VECTORS if vector["strict"]]
    advisory = [vector for vector in VECTORS if not vector["strict"]]
    assert len(strict) >= 14
    assert len(advisory) >= 1
    assert {vector["grid"] for vector in VECTORS} >= {1e-7, 9e-5}


def test_same_as_pairs_have_equal_pinned_digests():
    for vector in VECTORS:
        if vector["same_as"] is not None:
            assert vector["sha256"] == BY_NAME[vector["same_as"]]["sha256"], vector["name"]


def test_cell_straddle_pair_pins_distinct_digests():
    # The known caveat: an absolute grid, not a radius — 2e-8 apart, different hash.
    assert BY_NAME["cell_straddle_low"]["sha256"] != BY_NAME["cell_straddle_high"]["sha256"]


def test_collinear_extra_vertex_has_its_own_digest():
    assert BY_NAME["collinear_extra_vertex"]["sha256"] != BY_NAME["baseline_unit_square"]["sha256"]


# --- load_fixture ----------------------------------------------------------------


def test_load_fixture_missing_file_raises_step_error(tmp_path):
    with pytest.raises(dedup_vectors.StepError, match="missing"):
        dedup_vectors.load_fixture(tmp_path / "nope.json")


def test_load_fixture_wrong_recipe_version_raises_step_error(tmp_path):
    path = tmp_path / "v2.json"
    path.write_text(json.dumps({"recipe_version": "v2", "vectors": []}))
    with pytest.raises(dedup_vectors.StepError, match="recipe_version"):
        dedup_vectors.load_fixture(path)


# --- check_vectors ---------------------------------------------------------------

_FAKE_FIXTURE = {
    "recipe_version": "v1",
    "vectors": [
        {
            "name": "pinned_strict",
            "wkt": "POLYGON EMPTY",
            "grid": 1e-7,
            "sha256": "aa" * 32,
            "strict": True,
            "same_as": None,
            "note": None,
        },
        {
            "name": "pinned_advisory",
            "wkt": "POLYGON EMPTY",
            "grid": 1e-7,
            "sha256": "bb" * 32,
            "strict": False,
            "same_as": None,
            "note": None,
        },
    ],
}


def test_check_vectors_all_pass():
    digests = iter(["aa" * 32, "bb" * 32])

    report = dedup_vectors.check_vectors(lambda query, params=None: next(digests), _FAKE_FIXTURE)
    assert report.passed == 2
    assert report.strict_failures == ()
    assert report.advisory_failures == ()


def test_check_vectors_never_crosses_strict_and_advisory():
    report = dedup_vectors.check_vectors(lambda query, params=None: "ff" * 32, _FAKE_FIXTURE)
    assert report.passed == 0
    assert [failure.name for failure in report.strict_failures] == ["pinned_strict"]
    assert [failure.name for failure in report.advisory_failures] == ["pinned_advisory"]
    assert report.strict_failures[0].expected == "aa" * 32
    assert report.strict_failures[0].actual == "ff" * 32


# --- generate_fixture -------------------------------------------------------------


def _fake_run_sql(lib: str, geos: str):
    """Version queries → the given stack; recipe queries → a digest derived from
    the vector's canonical group, so same_as pairs collapse and others differ."""
    groups = {
        (case.wkt, case.grid): case.same_as or case.name for case in dedup_vectors.VECTOR_CASES
    }

    def run_sql(query: str, params: dict | None = None) -> str:
        if "postgis_lib_version" in query:
            return lib
        if "postgis_geos_version" in query:
            return geos
        if "postgis_full_version" in query:
            return f'POSTGIS="{lib}" GEOS="{geos}"'
        root = groups[(params["wkt"], params["grid"])]
        return hashlib.sha256(f"{root}|{params['grid']}".encode()).hexdigest()

    return run_sql


def test_generate_refuses_wrong_postgis_series():
    with pytest.raises(dedup_vectors.StepError, match="refusing"):
        dedup_vectors.generate_fixture(_fake_run_sql("3.6.0", "3.13.0"))


def test_generate_refuses_wrong_geos_series():
    with pytest.raises(dedup_vectors.StepError, match="refusing"):
        dedup_vectors.generate_fixture(_fake_run_sql("3.5.2", "3.13.0-CAPI-1.19.0"))


def test_generate_force_overrides_series_refusal():
    fixture = dedup_vectors.generate_fixture(_fake_run_sql("3.6.0", "3.13.0"), force=True)
    assert fixture["recipe_version"] == "v1"
    assert len(fixture["vectors"]) == len(dedup_vectors.VECTOR_CASES)


def test_generate_on_validated_series_cross_checks_same_as():
    fixture = dedup_vectors.generate_fixture(_fake_run_sql("3.5.2", "3.9.0-CAPI-1.16.2"))
    by_name = {vector["name"]: vector for vector in fixture["vectors"]}
    for vector in fixture["vectors"]:
        if vector["same_as"] is not None:
            assert vector["sha256"] == by_name[vector["same_as"]]["sha256"]


def test_generate_self_check_rejects_inconsistent_engine():
    # An engine where same_as pairs do NOT collapse must be refused.
    def run_sql(query: str, params: dict | None = None) -> str:
        if "version" in query:
            return "3.5.2" if "lib" in query else "3.9.0"
        return hashlib.sha256(params["wkt"].encode()).hexdigest()

    with pytest.raises(dedup_vectors.StepError, match="self-check"):
        dedup_vectors.generate_fixture(run_sql)


# --- CLI argument validation -------------------------------------------------------


def test_cli_requires_exactly_one_mode():
    assert dedup_vectors.main(["--dsn", "postgresql://x/y"]) == 2
    assert dedup_vectors.main(["--check", "--generate", "--dsn", "postgresql://x/y"]) == 2
