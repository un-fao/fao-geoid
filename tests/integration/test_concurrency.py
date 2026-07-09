"""Stress/correctness: dedup must stay race-free under concurrent identical POSTs."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_concurrent_identical_posts_converge_to_one_geoid(
    client, admin_headers, session, unit_square_ccw
):
    """K simultaneous POSTs of the SAME geometry → exactly one mint, one row.

    Exercises the arbiter CTE (registry ON CONFLICT DO NOTHING + incumbent-lookup)
    race: one arbiter insert wins (201); the rest block on the registry's geom_hash
    UNIQUE, see the conflict, and fail with a 409 that carries the winner's geoid.
    The invariant is one place row total. Sysadmin caller on every POST so each
    loser's 409 discloses (is_admin short-circuits before any grant query — the
    race semantics and DB traffic are unchanged).
    """
    k = 16
    responses = await asyncio.gather(
        *[
            client.post("/collections/public/items", headers=admin_headers, json=unit_square_ccw)
            for _ in range(k)
        ]
    )

    # Assert statuses BEFORE reading geoids so a stray 500 surfaces with its body,
    # not as an opaque KeyError on a missing "geoid" key.
    statuses = [r.status_code for r in responses]
    offenders = [(r.status_code, r.json()) for r in responses if r.status_code not in (201, 409)]
    assert not offenders, f"unexpected statuses: {offenders}"
    assert statuses.count(201) == 1, f"expected exactly one mint, got {statuses}"
    assert statuses.count(409) == k - 1, f"expected {k - 1} conflicts, got {statuses}"

    # Every winner/loser response converges on the one deterministic geoid, and
    # every loser's 409 body must carry the incumbent geoid (no empty conflicts).
    geoids = {r.json()["geoid"] for r in responses if r.status_code in (201, 409)}
    assert len(geoids) == 1, f"expected a single geoid, got {geoids}"
    for resp in responses:
        if resp.status_code == 409:
            body = resp.json()
            assert body["constraint"] == "uq_geoid_registry_geom_hash", body
            assert body["geoid"], body

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


async def test_resolve_incumbent_returns_committed_collection(client, session, unit_square_ccw):
    """The incumbent-recovery helper reads back the incumbent's facts once visible.

    Covers ``_resolve_incumbent``'s success path deterministically (no race):
    mint a place via the API (committed), then call the helper directly — it must
    return the incumbent's collection facts (id/slug/creator) from the
    now-visible registry row. The concurrent path only reaches this helper when
    the in-statement LEFT JOIN missed.
    """
    from geoid.repositories.place_repo import _resolve_incumbent
    from geoid.schemas.place import PlaceCreate, geometry_to_geojson

    assert (await client.post("/collections/public/items", json=unit_square_ccw)).status_code == 201

    geojson = geometry_to_geojson(PlaceCreate.model_validate(unit_square_ccw))
    incumbent = await _resolve_incumbent(session, geojson)
    assert incumbent is not None
    collection_id, slug, created_by = incumbent
    assert slug == "public"
    assert collection_id is not None
    assert created_by is None  # minted anonymously above
