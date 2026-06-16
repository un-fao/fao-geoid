"""Integration tests for the place-set surface (Phase 5): CQL2 queryable + links."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _square(x: float, y: float) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]],
        },
        "properties": {},
    }


async def _bulk(client, admin_headers, features) -> dict:
    body = {
        "inputs": {
            "collection": "public",
            "items": {"type": "FeatureCollection", "features": features},
        }
    }
    return (
        await client.post("/processes/bulk-ingest/execution", headers=admin_headers, json=body)
    ).json()


async def test_queryables_advertises_ingest_batch_id(client):
    doc = (await client.get("/collections/public/queryables")).json()
    assert "ingest_batch_id" in doc["properties"]


async def test_filter_items_by_ingest_batch_id(client, admin_headers):
    report = await _bulk(client, admin_headers, [_square(10, 10), _square(20, 20)])
    batch = report["batch_id"]
    # A second batch that must NOT match the filter.
    await _bulk(client, admin_headers, [_square(30, 30)])

    resp = await client.get(f"/collections/public/items?filter=ingest_batch_id='{batch}'")
    assert resp.status_code == 200
    fc = resp.json()
    assert fc["numberMatched"] == 2
    for feature in fc["features"]:
        assert feature["properties"]["ingest_batch_id"] == batch


async def test_item_exposes_place_set_property_and_link(client, admin_headers):
    report = await _bulk(client, admin_headers, [_square(40, 40)])
    batch = report["batch_id"]
    geoid = report["accepted"][0]["geoid"]

    feature = (await client.get(f"/geoid/{geoid}")).json()
    assert feature["properties"]["ingest_batch_id"] == batch
    hrefs = [link["href"] for link in feature["links"]]
    assert any(href.endswith(f"/place-sets/{batch}") for href in hrefs)


async def test_filter_by_unknown_batch_returns_empty_page(client, admin_headers):
    await _bulk(client, admin_headers, [_square(50, 50)])
    resp = await client.get(
        "/collections/public/items?filter=ingest_batch_id='019e0000-0000-7000-8000-000000000000'"
    )
    assert resp.status_code == 200
    assert resp.json()["numberMatched"] == 0
