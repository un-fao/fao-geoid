#!/usr/bin/env python3
"""Seed GeoID with the dummy sample plots and demonstrate the core behaviours.

    # stack must be running (docker compose up, or `uv run uvicorn geoid.main:app`)
    uv run python scripts/seed_samples.py

Env:
    GEOID_BASE_URL    default http://localhost:8000
    GEOID_COLLECTION  default public  (anonymous-writable; no token needed)
    GEOID_ADMIN_TOKEN required only when seeding a managed (non-anon) collection
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

BASE = os.environ.get("GEOID_BASE_URL", "http://localhost:8000").rstrip("/")
COLLECTION = os.environ.get("GEOID_COLLECTION", "public")
ADMIN_TOKEN = os.environ.get("GEOID_ADMIN_TOKEN")
SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"} if ADMIN_TOKEN else {}


def _post(client: httpx.Client, feature: dict) -> httpx.Response:
    return client.post(f"{BASE}/collections/{COLLECTION}/items", json=feature, headers=_headers())


def main() -> int:
    with httpx.Client(timeout=30.0) as client:
        try:
            client.get(f"{BASE}/conformance").raise_for_status()
        except Exception as exc:
            print(f"✗ cannot reach GeoID at {BASE} ({exc}).")
            print(
                "  Start it first:  docker compose up --build   (or uv run uvicorn geoid.main:app)"
            )
            return 1
        print(f"→ GeoID at {BASE}, collection '{COLLECTION}'\n")

        first_geoid = None
        print("== Minting sample plots ==")
        feature_collection = json.loads((SAMPLES / "plots.geojson").read_text())
        for feature in feature_collection["features"]:
            resp = _post(client, feature)
            body = resp.json()
            ext = str(feature.get("id"))
            if resp.status_code == 201:
                print(f"  [201] {ext:<22} {'minted':<18} geoid={body['geoid']}")
            elif (
                resp.status_code == 409 and body.get("constraint") == "uq_geoid_registry_geom_hash"
            ):
                # Re-running the seed: the geometry is already registered.
                print(f"  [409] {ext:<22} {'duplicate→existing':<18} geoid={body['geoid']}")
            else:
                print(f"  [{resp.status_code}] {ext:<22} ERROR {body}")
                continue
            if first_geoid is None:
                first_geoid = body["geoid"]

        print("\n== Dedup demo (GH-COCOA-001's geometry, reversed winding, no id) ==")
        dup = json.loads((SAMPLES / "duplicate_of_GH-COCOA-001.geojson").read_text())
        resp = _post(client, dup)
        body = resp.json()
        status = "409 Conflict" if resp.status_code == 409 else f"?? {resp.status_code}"
        print(
            f"  [{status}] insert rejected; incumbent geoid={body.get('geoid')} "
            f"(collection={body.get('collection')})"
        )

        print("\n== Validation demo (self-intersecting bow-tie) ==")
        invalid = json.loads((SAMPLES / "invalid_selfintersecting.geojson").read_text())
        resp = _post(client, invalid)
        print(f"  [{resp.status_code}] {resp.json().get('reason')}")

        if first_geoid:
            # Resolver lives at the app root (BASE already includes any /geoid root_path).
            props = client.get(f"{BASE}/{first_geoid}").json()["properties"]
            print(f"\n== Resolve {first_geoid} ==")
            print(f"  uri:         {props['uri']}")
            print(f"  external_id: {props['external_id']}   commodity: {props.get('commodity')}")
            print(f"  provenance:  {props['_geoid_provenance']['client']}")

        print(f"\n✓ done — explore at {BASE}/docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
