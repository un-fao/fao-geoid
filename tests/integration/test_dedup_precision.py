"""Release-1 coordinate-precision behaviour: the dedup grid defaults to ~10m
(0.00009 deg/vertex), is stamped onto every collection at creation, and snaps
geometries to an absolute grid.

The stamped value in ``collection.metadata->>'dedup_grid'`` is the source of truth
read by BOTH the BEFORE-INSERT trigger and the incumbent-lookup, so these tests pin
it for the public (bootstrap) collection and an admin-created one, then demonstrate
the grid in action via the live write path.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from geoid.config import get_settings

pytestmark = pytest.mark.integration

# 9e-5 deg ≈ 10m. A ~100m square (10 cells/side) with every corner on the grid, so
# the snap is unambiguous and the perturbations below can't straddle a cell boundary.
_SIDE = 0.0009


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


async def test_public_collection_is_stamped_with_default_grid(session):
    grid = (
        await session.execute(
            text(
                "SELECT (metadata->>'dedup_grid')::double precision "
                "FROM collection WHERE slug = :slug"
            ),
            {"slug": "public"},
        )
    ).scalar_one()
    assert grid == get_settings().dedup_grid_default


async def test_admin_collection_is_stamped_with_default_grid(client, admin_headers):
    await client.post("/manage/workspaces", headers=admin_headers, json={"slug": "ws"})
    resp = await client.post(
        "/manage/workspaces/ws/collections", headers=admin_headers, json={"slug": "c"}
    )
    assert resp.status_code == 201
    assert resp.json()["metadata"]["dedup_grid"] == get_settings().dedup_grid_default


async def test_admin_supplied_grid_overrides_the_default(client, admin_headers):
    await client.post("/manage/workspaces", headers=admin_headers, json={"slug": "ws2"})
    resp = await client.post(
        "/manage/workspaces/ws2/collections",
        headers=admin_headers,
        json={"slug": "fine", "metadata": {"dedup_grid": 1e-6}},
    )
    assert resp.status_code == 201
    assert resp.json()["metadata"]["dedup_grid"] == 1e-6


async def test_within_cell_collapses_across_cell_is_distinct(client):
    # The public collection carries the configured ~10m grid (9e-5 deg).
    base = (await client.post("/collections/public/items", json=_square(0.0))).json()
    # +1e-5 deg (~1m): every vertex snaps back to the base cell -> same geoid (dedup).
    same_cell = (
        await client.post("/collections/public/items", json=_square(1e-5))
    ).json()
    # +0.00027 deg (3 cells, ~30m): a clearly different cell -> a distinct geoid.
    other_cell = (
        await client.post("/collections/public/items", json=_square(0.00027))
    ).json()

    assert same_cell["geoid"] == base["geoid"]
    assert same_cell["deduplicated"] is True
    assert other_cell["geoid"] != base["geoid"]
    assert other_cell["deduplicated"] is False
