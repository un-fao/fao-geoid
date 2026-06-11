"""Stress/correctness: dedup must stay race-free under concurrent identical POSTs."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_concurrent_identical_posts_converge_to_one_geoid(client, session, unit_square_ccw):
    """K simultaneous POSTs of the SAME geometry → exactly one mint, one row.

    Exercises the ON CONFLICT ON CONSTRAINT ... DO NOTHING + incumbent-lookup race:
    one INSERT wins (201); the rest block on the unique index, see the conflict,
    and fail with a 409 that carries the winner's geoid. The invariant is one
    place row total.
    """
    k = 16
    responses = await asyncio.gather(
        *[client.post("/collections/public/items", json=unit_square_ccw) for _ in range(k)]
    )

    statuses = [r.status_code for r in responses]
    geoids = {r.json()["geoid"] for r in responses}

    assert all(s in (201, 409) for s in statuses), statuses
    assert statuses.count(201) == 1, f"expected exactly one mint, got {statuses}"
    assert statuses.count(409) == k - 1
    # Every loser's 409 body must carry the winner's geoid (no empty conflicts).
    assert len(geoids) == 1, f"expected a single geoid, got {geoids}"
    for resp in responses:
        if resp.status_code == 409:
            assert resp.json()["constraint"] == "uq_place_geom_hash"

    place_count = (await session.execute(text("SELECT count(*) FROM place"))).scalar_one()
    registry_count = (
        await session.execute(text("SELECT count(*) FROM geoid_registry"))
    ).scalar_one()
    changelog_count = (await session.execute(text("SELECT count(*) FROM change_log"))).scalar_one()
    assert place_count == 1
    assert registry_count == 1
    assert changelog_count == 1


async def test_concurrent_distinct_posts_all_mint(client, session):
    """K simultaneous POSTs of DISTINCT geometries → K distinct geoids, K rows."""
    k = 16

    def square(i: int) -> dict:
        x = -150 + i  # distinct, well-separated
        return {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[x, 0], [x + 0.5, 0], [x + 0.5, 0.5], [x, 0.5], [x, 0]]],
            },
            "properties": {},
        }

    responses = await asyncio.gather(
        *[client.post("/collections/public/items", json=square(i)) for i in range(k)]
    )
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["geoid"] for r in responses}) == k
    place_count = (await session.execute(text("SELECT count(*) FROM place"))).scalar_one()
    assert place_count == k
