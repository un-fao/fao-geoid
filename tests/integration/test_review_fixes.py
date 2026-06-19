"""Regression tests for the adversarial-review fixes."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


# --- dual-violation precedence: the geometry 409 wins over external_id's -----
# The arbiter CTE inserts the geoid_registry row first (ON CONFLICT DO NOTHING on
# the geom_hash UNIQUE); the place row — and so its external_id index — is only
# touched if the registry arbiter won. So a submission duplicating BOTH geometry
# and external_id yields the GEOMETRY conflict (409 carrying the incumbent geoid),
# never the external_id 409.


async def test_dual_geometry_and_external_id_duplicate_yields_geometry_409(client, unit_square_ccw):
    feature = dict(unit_square_ccw, id="dup-ext")
    base = await client.post("/collections/public/items", json=feature)
    assert base.status_code == 201

    resub = await client.post("/collections/public/items", json=feature)
    assert resub.status_code == 409
    assert resub.json()["constraint"] == "uq_geoid_registry_geom_hash"
    assert resub.json()["geoid"] == base.json()["geoid"]


async def test_geometry_dup_with_another_rows_external_id_yields_geometry_409(
    client, unit_square_ccw, other_square
):
    a = await client.post("/collections/public/items", json=dict(unit_square_ccw, id="ext-a"))
    b = await client.post("/collections/public/items", json=dict(other_square, id="ext-b"))
    assert a.status_code == 201 and b.status_code == 201

    # A's geometry + B's external_id: the geometry arbiter prechecks first, so
    # the 409 carries A's geoid; the conflicting external_id is never reached.
    resub = await client.post("/collections/public/items", json=dict(unit_square_ccw, id="ext-b"))
    assert resub.status_code == 409
    assert resub.json()["constraint"] == "uq_geoid_registry_geom_hash"
    assert resub.json()["geoid"] == a.json()["geoid"]


# --- #4 TRUNCATE is blocked on place ----------------------------------------


async def test_place_truncate_is_blocked(client, session, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(text("TRUNCATE place"))
        await session.flush()


# --- #5 hinge tables are append-only ----------------------------------------


async def test_geoid_registry_delete_is_blocked(client, session, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM geoid_registry WHERE geoid = :g"), {"g": geoid})
        await session.flush()


async def test_change_log_update_is_blocked(client, session, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(text("UPDATE change_log SET op = 'tampered'"))
        await session.flush()


def _square_at(x, y, s=0.5):
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]],
        },
        "properties": {},
    }


# --- #6 GeoJSON content type -------------------------------------------------


async def test_items_response_is_geojson_media_type(client, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    resp = await client.get("/collections/public/items")
    assert resp.headers["content-type"].startswith("application/geo+json")


async def test_item_and_resolver_are_geojson_media_type(client, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    item = await client.get(f"/collections/public/items/{geoid}")
    resolver = await client.get(f"/{geoid}")
    assert item.headers["content-type"].startswith("application/geo+json")
    assert resolver.headers["content-type"].startswith("application/geo+json")


# --- #7 real collection extent ----------------------------------------------


async def test_collection_extent_reflects_data(client):
    await client.post("/collections/public/items", json=_square_at(10.0, 20.0, s=1.0))
    desc = (await client.get("/collections/public")).json()
    assert desc["extent"]["spatial"]["bbox"][0] == [10.0, 20.0, 11.0, 21.0]


async def test_empty_collection_extent_is_world(client, admin_headers):
    await client.post("/manage/collections", headers=admin_headers, json={"id": "emptyc"})
    desc = (await client.get("/collections/emptyc")).json()
    assert desc["extent"]["spatial"]["bbox"] == [[-180.0, -90.0, 180.0, 90.0]]
