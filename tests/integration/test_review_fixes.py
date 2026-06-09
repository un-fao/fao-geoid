"""Regression tests for the adversarial-review fixes."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


# --- #1 dedup_grid immutability once a collection has places ----------------

async def test_dedup_grid_change_blocked_when_places_exist(client, session, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(
            text(
                "UPDATE collection SET metadata = jsonb_build_object('dedup_grid', 1e-6) "
                "WHERE slug = 'public'"
            )
        )
        await session.flush()


async def test_dedup_grid_change_allowed_on_empty_collection(client, admin_headers, session):
    await client.post("/manage/workspaces", headers=admin_headers, json={"slug": "wse"})
    await client.post("/manage/workspaces/wse/collections", headers=admin_headers, json={"slug": "empt"})
    await session.execute(
        text(
            "UPDATE collection SET metadata = jsonb_build_object('dedup_grid', 1e-6) "
            "WHERE slug = 'empt'"
        )
    )
    await session.commit()
    grid = (
        await session.execute(text("SELECT metadata->>'dedup_grid' FROM collection WHERE slug='empt'"))
    ).scalar_one()
    assert grid is not None


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


# --- #2 antimeridian bbox ----------------------------------------------------

def _square_at(x, y, s=0.5):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]]},
        "properties": {},
    }


async def test_antimeridian_bbox_returns_both_sides(client):
    await client.post("/collections/public/items", json=_square_at(179.0, 0.0))     # near +180
    await client.post("/collections/public/items", json=_square_at(-179.5, 0.0))    # near -180
    await client.post("/collections/public/items", json=_square_at(0.0, 0.0))       # far away

    crossing = (await client.get("/collections/public/items?bbox=178,-1,-178,1")).json()
    assert crossing["numberMatched"] == 2  # both antimeridian-adjacent, not the one at 0


async def test_non_crossing_bbox_still_works(client):
    await client.post("/collections/public/items", json=_square_at(179.0, 0.0))
    await client.post("/collections/public/items", json=_square_at(-179.5, 0.0))
    only_east = (await client.get("/collections/public/items?bbox=178,-1,180,1")).json()
    assert only_east["numberMatched"] == 1


# --- #6 GeoJSON content type -------------------------------------------------

async def test_items_response_is_geojson_media_type(client, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    resp = await client.get("/collections/public/items")
    assert resp.headers["content-type"].startswith("application/geo+json")


async def test_item_and_resolver_are_geojson_media_type(client, unit_square_ccw):
    geoid = (await client.post("/collections/public/items", json=unit_square_ccw)).json()["geoid"]
    item = await client.get(f"/collections/public/items/{geoid}")
    resolver = await client.get(f"/geoid/{geoid}")
    assert item.headers["content-type"].startswith("application/geo+json")
    assert resolver.headers["content-type"].startswith("application/geo+json")


# --- #7 real collection extent ----------------------------------------------

async def test_collection_extent_reflects_data(client):
    await client.post("/collections/public/items", json=_square_at(10.0, 20.0, s=1.0))
    desc = (await client.get("/collections/public")).json()
    assert desc["extent"]["spatial"]["bbox"][0] == [10.0, 20.0, 11.0, 21.0]


async def test_empty_collection_extent_is_world(client, admin_headers):
    await client.post("/manage/workspaces", headers=admin_headers, json={"slug": "wsw"})
    await client.post("/manage/workspaces/wsw/collections", headers=admin_headers, json={"slug": "emptyc"})
    desc = (await client.get("/collections/emptyc")).json()
    assert desc["extent"]["spatial"]["bbox"] == [[-180.0, -90.0, 180.0, 90.0]]


# --- #12 Content-Disposition uses the validated slug ------------------------

async def test_bulk_content_disposition_uses_slug(client, unit_square_ccw):
    await client.post("/collections/public/items", json=unit_square_ccw)
    resp = await client.get("/collections/public/bulk")
    assert resp.headers["content-disposition"] == 'attachment; filename="public.geojson"'
