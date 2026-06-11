"""Unit tests for scripts/rehash_geom_hashes.py — config validation and the pure
collision-resolution planner (decision D5; no DB, no subprocess)."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "rehash_geom_hashes", ROOT / "scripts" / "rehash_geom_hashes.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the dataclass decorator resolves the module's string
    # annotations through sys.modules[cls.__module__].
    sys.modules["rehash_geom_hashes"] = module
    spec.loader.exec_module(module)
    return module


rehash = _load_script_module()

COLL_A = uuid.UUID(int=0xA)
COLL_B = uuid.UUID(int=0xB)


def _row(n: int, collection_id: uuid.UUID, old_hash: str, new_hash: str) -> object:
    # uuid.UUID(int=n) keeps id ordering aligned with n — the planner only relies
    # on id order (UUIDv7 = mint-time order in production).
    return rehash.ScanRow(
        id=uuid.UUID(int=n), collection_id=collection_id,
        old_hash=old_hash, new_hash=new_hash,
    )


# --- config ---------------------------------------------------------------------

def test_config_error_on_missing_env(monkeypatch):
    monkeypatch.delenv("GEOID_DATABASE_URL", raising=False)
    with pytest.raises(rehash.ConfigError, match="GEOID_DATABASE_URL"):
        rehash._load_config([])


def test_config_error_on_non_postgresql_url(monkeypatch):
    monkeypatch.setenv("GEOID_DATABASE_URL", "mysql://geoid:pw@host:3306/geoid")
    with pytest.raises(rehash.ConfigError, match="postgresql"):
        rehash._load_config([])


def test_main_exits_2_on_config_error(monkeypatch):
    monkeypatch.delenv("GEOID_DATABASE_URL", raising=False)
    assert rehash.main(["--dry-run"]) == 2


def test_config_dsn_strips_async_driver(monkeypatch):
    monkeypatch.setenv("GEOID_DATABASE_URL", "postgresql+asyncpg://geoid:pw@host:5432/geoid")
    cfg = rehash._load_config(["--dry-run", "--yes"])
    assert cfg.dsn == "postgresql://geoid:pw@host:5432/geoid"
    assert cfg.dry_run and cfg.assume_yes


# --- plan_rehash ------------------------------------------------------------------

def test_plan_with_no_collisions_updates_every_drifted_row():
    rows = [
        _row(1, COLL_A, old_hash="aa", new_hash="a1"),
        _row(2, COLL_A, old_hash="bb", new_hash="b1"),
        _row(3, COLL_A, old_hash="cc", new_hash="cc"),  # unchanged
    ]
    plan = rehash.plan_rehash(rows)
    assert [row.id for row in plan.updates] == [uuid.UUID(int=1), uuid.UUID(int=2)]
    assert plan.skipped_pairs == ()
    assert plan.unchanged_count == 1


def test_plan_collision_earliest_id_wins_later_skipped():
    rows = [
        _row(1, COLL_A, old_hash="aa", new_hash="HH"),
        _row(2, COLL_A, old_hash="bb", new_hash="HH"),
    ]
    plan = rehash.plan_rehash(rows)
    assert [row.id for row in plan.updates] == [uuid.UUID(int=1)]
    assert len(plan.skipped_pairs) == 1
    pair = plan.skipped_pairs[0]
    assert pair.loser_id == uuid.UUID(int=2)
    assert pair.winner_id == uuid.UUID(int=1)
    assert pair.geom_hash == "HH"
    assert pair.collection_id == COLL_A


def test_plan_incumbent_unchanged_row_beats_earlier_drifted_row():
    # Updating the drifted row would itself violate uq_place_collection_geom_hash:
    # the later row already HOLDS the hash. It must win despite the later id.
    rows = [
        _row(1, COLL_A, old_hash="aa", new_hash="HH"),
        _row(2, COLL_A, old_hash="HH", new_hash="HH"),
    ]
    plan = rehash.plan_rehash(rows)
    assert plan.updates == ()
    assert plan.unchanged_count == 1
    assert len(plan.skipped_pairs) == 1
    assert plan.skipped_pairs[0].loser_id == uuid.UUID(int=1)
    assert plan.skipped_pairs[0].winner_id == uuid.UUID(int=2)


def test_plan_cascade_demotes_update_targeting_a_skipped_rows_retained_hash():
    # r2 loses the HH group to r1 and so RETAINS its old hash XX; r3's planned
    # update targets XX — applying it would collide with r2's retained value, so
    # r3 must be demoted too (fixed-point), paired with r2.
    rows = [
        _row(1, COLL_A, old_hash="aa", new_hash="HH"),
        _row(2, COLL_A, old_hash="XX", new_hash="HH"),
        _row(3, COLL_A, old_hash="bb", new_hash="XX"),
    ]
    plan = rehash.plan_rehash(rows)
    assert [row.id for row in plan.updates] == [uuid.UUID(int=1)]
    losers = {pair.loser_id: pair for pair in plan.skipped_pairs}
    assert set(losers) == {uuid.UUID(int=2), uuid.UUID(int=3)}
    assert losers[uuid.UUID(int=3)].winner_id == uuid.UUID(int=2)
    assert losers[uuid.UUID(int=3)].geom_hash == "XX"


def test_plan_collisions_are_scoped_per_collection():
    rows = [
        _row(1, COLL_A, old_hash="aa", new_hash="HH"),
        _row(2, COLL_B, old_hash="bb", new_hash="HH"),
    ]
    plan = rehash.plan_rehash(rows)
    assert [row.id for row in plan.updates] == [uuid.UUID(int=1), uuid.UUID(int=2)]
    assert plan.skipped_pairs == ()


def test_plan_is_pure_and_frozen():
    rows = (
        _row(1, COLL_A, old_hash="aa", new_hash="HH"),
        _row(2, COLL_A, old_hash="bb", new_hash="HH"),
    )
    snapshot = tuple(rows)
    first = rehash.plan_rehash(rows)
    second = rehash.plan_rehash(rows)
    assert first == second
    assert rows == snapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.unchanged_count = 99
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.updates[0].new_hash = "zz"
