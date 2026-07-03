"""Integration: non-public collections (``public_read=false``) end to end.

Covers the four 2026-07-03 surfaces: resolver 404-masking (existence-masked, the
body identical to an unknown id's), caller-aware dedup-409 disclosure (single +
bulk), ``GET /me/geoids``, and the sysadmin ``/manage`` item inventory.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# --- public_read default --------------------------------------------------------


async def test_public_read_defaults_true(client, admin_headers):
    await client.post("/manage/collections", headers=admin_headers, json={"id": "plain"})
    listed = (await client.get("/manage/collections", headers=admin_headers)).json()
    by_id = {c["id"]: c for c in listed}
    # Both a freshly created collection and the bootstrap `public` stay readable.
    assert by_id["plain"]["public_read"] is True
    assert by_id["public"]["public_read"] is True
