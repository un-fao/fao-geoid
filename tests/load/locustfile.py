"""Locust alternative to the k6 script (same three hot paths).

    pip install locust  # not a project dependency
    locust -f tests/load/locustfile.py --host http://localhost:8000

Then open http://localhost:8089 and drive load. Tasks are weighted to emphasise
the mint + dedup-lookup hot paths while exercising the OGC read surface.
"""

from __future__ import annotations

import random

from locust import HttpUser, between, task

_COLLECTION = "public"
_ITEMS = f"/collections/{_COLLECTION}/items"


def _polygon(x: float, y: float, size: float) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]],
        },
        "properties": {},
    }


class GeoIDUser(HttpUser):
    wait_time = between(0.01, 0.1)

    @task(5)
    def mint_unique(self) -> None:
        x = random.uniform(-179, 179)
        y = random.uniform(-89, 89)
        with self.client.post(
            _ITEMS, json=_polygon(x, y, 0.0001), name="POST mint", catch_response=True
        ) as resp:
            if resp.status_code in (200, 201):
                resp.success()
            else:
                resp.failure(f"unexpected status {resp.status_code}")

    @task(3)
    def dedup_same(self) -> None:
        # Same geometry every time -> exercises the dedup-lookup hot path.
        with self.client.post(
            _ITEMS, json=_polygon(1.234567, 2.345678, 0.001), name="POST dedup", catch_response=True
        ) as resp:
            if resp.status_code in (200, 201):
                resp.success()
            else:
                resp.failure(f"unexpected status {resp.status_code}")

    @task(4)
    def read_items(self) -> None:
        self.client.get(f"{_ITEMS}?limit=50", name="GET items")
