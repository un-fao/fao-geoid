"""Integration tests for immutability (DB trigger) and the two federation hinges."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


async def _mint(client, unit_square_ccw) -> str:
    resp = await client.post("/collections/public/items", json=unit_square_ccw)
    assert resp.status_code == 201
    return resp.json()["geoid"]


async def test_update_on_place_is_blocked_by_trigger(client, session, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(
            text("UPDATE place SET external_id = 'hacked' WHERE id = :id"), {"id": geoid}
        )
        await session.flush()


async def test_delete_on_place_is_blocked_by_trigger(client, session, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM place WHERE id = :id"), {"id": geoid})
        await session.flush()


async def test_geoid_registry_hinge_populated_on_mint(client, session, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    row = (
        await session.execute(
            text("SELECT place_id, collection_id FROM geoid_registry WHERE geoid = :g"),
            {"g": geoid},
        )
    ).first()
    assert row is not None
    assert str(row[0]) == geoid


async def test_change_log_hinge_records_create(client, session, unit_square_ccw):
    geoid = await _mint(client, unit_square_ccw)
    row = (
        await session.execute(
            text("SELECT op, payload FROM change_log WHERE geoid = :g ORDER BY seq DESC LIMIT 1"),
            {"g": geoid},
        )
    ).first()
    assert row is not None
    assert row[0] == "create"


async def test_duplicate_geometry_409_leaves_hinges_untouched(
    client, session, unit_square_ccw, unit_square_reversed
):
    await client.post("/collections/public/items", json=unit_square_ccw)
    second = await client.post("/collections/public/items", json=unit_square_reversed)
    assert second.status_code == 409  # identical geometry → insert fails

    place_count = (await session.execute(text("SELECT count(*) FROM place"))).scalar_one()
    registry_count = (await session.execute(text("SELECT count(*) FROM geoid_registry"))).scalar_one()
    changelog_count = (await session.execute(text("SELECT count(*) FROM change_log"))).scalar_one()
    assert place_count == 1
    assert registry_count == 1
    assert changelog_count == 1  # the rejected second POST adds nothing
