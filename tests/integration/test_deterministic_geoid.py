"""Integration tests for the deterministic, content-addressed geoid (UUIDv8).

The geoid is derived DB-side from the geometry's canonical ``geom_hash`` (migration
0004), so identity is a pure function of the geometry: same geometry -> same geoid on
every deployment, and the geoid always matches the stored hash it was derived from.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from geoid.domain import identifiers

pytestmark = pytest.mark.integration


def _polygon_feature(coords: list[list[list[float]]]) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": {},
    }


async def test_minted_geoid_is_version_8_and_derived_from_stored_hash(
    client, session, unit_square_ccw
):
    # GEOS-robust: whatever hash this stack computes, the geoid is its UUIDv8
    # derivation — identity and dedup can never disagree.
    resp = await client.post("/collections/public/items", json=unit_square_ccw)
    assert resp.status_code == 201
    geoid = resp.json()["geoid"]
    assert uuid.UUID(geoid).version == 8

    row = (
        await session.execute(
            text("SELECT geom_hash FROM geoid_registry WHERE geoid = :g"), {"g": geoid}
        )
    ).first()
    assert row is not None
    assert identifiers.geoid_from_geom_hash(bytes(row[0])) == uuid.UUID(geoid)


async def test_baseline_unit_square_mints_the_pinned_geoid(client):
    # Cross-deployment determinism: POLYGON((0 0,1 0,1 1,0 1,0 0)) is the GEOS-stable
    # strict golden vector (sha256 c624e284...). Its geoid is fixed on ANY instance,
    # so this exact value must come back here and on Cloud SQL alike.
    feature = _polygon_feature([[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]])
    resp = await client.post("/collections/public/items", json=feature)
    assert resp.status_code == 201
    assert resp.json()["geoid"] == "c624e284-23aa-8c24-85c0-cda6e2ecab95"


async def test_winding_independent_input_mints_the_same_pinned_geoid(client):
    # The recipe normalizes winding/ring-start, so a CW unit square mints the SAME
    # deterministic geoid as the CCW one above (and 409s if both are posted).
    feature = _polygon_feature([[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]])
    resp = await client.post("/collections/public/items", json=feature)
    assert resp.status_code == 201
    assert resp.json()["geoid"] == "c624e284-23aa-8c24-85c0-cda6e2ecab95"


async def test_distinct_geometries_get_independently_derived_geoids(
    client, session, unit_square_ccw, other_square
):
    a = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    b = (await client.post("/collections/public/items", json=other_square)).json()["geoid"]
    assert a != b
    for geoid in (a, b):
        row = (
            await session.execute(
                text("SELECT geom_hash FROM geoid_registry WHERE geoid = :g"), {"g": geoid}
            )
        ).first()
        assert identifiers.geoid_from_geom_hash(bytes(row[0])) == uuid.UUID(geoid)
