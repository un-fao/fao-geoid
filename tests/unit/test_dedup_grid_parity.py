"""Parity pin: the config-canonical dedup grid (``GEOID_DEDUP_GRID_DEFAULT``) must
equal the literal pinned inside the ``geoid_geom_hash_default()`` SQL wrapper in
migration 0001.

Relocating the global geometry-dedup UNIQUE onto ``geoid_registry`` dropped the
``BEFORE INSERT`` trigger that used to pin the grid (``1e-7``) as a migration
literal. ``geoid_geom_hash_default(geom)`` is its replacement — it pins the grid
so the app's arbiter insert, the incumbent lookup, and the rehash script never
pass a grid from config. This test keeps the config value and that pinned literal
from drifting apart: a retune must move BOTH, as a migration event.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from geoid.config import Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "versions" / "0001_initial.py"

# The wrapper is the ONLY call site that passes the bare argument `g` plus a numeric
# grid literal: `SELECT geoid_geom_hash(g, 1e-7);`. The parametric definition reads
# `geoid_geom_hash(g geometry, grid ...)`, which this pattern does not match.
_WRAPPER_GRID = re.compile(r"geoid_geom_hash\(g,\s*([0-9.eE+-]+)\s*\)")


def _wrapper_grid_literal() -> float:
    matches = _WRAPPER_GRID.findall(MIGRATION.read_text())
    assert matches, "geoid_geom_hash_default wrapper literal not found in migration 0001"
    return float(matches[-1])


def test_config_default_grid_matches_wrapper_literal(monkeypatch):
    monkeypatch.delenv("GEOID_DEDUP_GRID_DEFAULT", raising=False)
    assert Settings(_env_file=None).dedup_grid_default == _wrapper_grid_literal()
