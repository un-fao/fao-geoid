"""Release-1 coordinate-precision behaviour: ONE global dedup grid (~1cm,
1e-7 deg/vertex — exact-match semantics per Remi's ruling), pinned in the
BEFORE-INSERT trigger by migration 0001. Geometry uniqueness is catalog-wide,
and an identical submission fails with a 409 carrying the incumbent geoid.

These tests demonstrate the grid in action via the live write path: sub-cell
float jitter collapses onto the incumbent (409), a shift of several cells is a
different place (201, distinct geoid).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# 1e-7 deg ≈ 1cm. A square 10 cells/side with every corner on the grid, so the
# snap is unambiguous and the perturbations below can't straddle a cell boundary.
_SIDE = 1e-6


def _square(dx: float) -> dict:
    """A CCW square translated east by ``dx`` degrees (corners otherwise on-grid)."""
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [dx, 0.0],
                    [dx + _SIDE, 0.0],
                    [dx + _SIDE, _SIDE],
                    [dx, _SIDE],
                    [dx, 0.0],
                ]
            ],
        },
        "properties": {},
    }


async def test_within_cell_conflicts_across_cell_mints(client):
    base_resp = await client.post("/collections/public/items", json=_square(0.0))
    assert base_resp.status_code == 201
    base = base_resp.json()

    # +1e-8 deg (~1mm): every vertex snaps back to the base cell -> identical
    # canonical geometry -> 409 carrying the incumbent geoid.
    same_cell = await client.post("/collections/public/items", json=_square(1e-8))
    assert same_cell.status_code == 409
    body = same_cell.json()
    assert body["geoid"] == base["geoid"]
    assert body["constraint"] == "uq_geoid_registry_geom_hash"

    # +3e-7 deg (3 cells, ~3cm): a clearly different cell -> a distinct geoid.
    other_cell = await client.post("/collections/public/items", json=_square(3e-7))
    assert other_cell.status_code == 201
    assert other_cell.json()["geoid"] != base["geoid"]
