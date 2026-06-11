# Sample data

Dummy EUDR/Whisp-style plots for local testing, driven by `scripts/seed_samples.py`.

| File | What it shows |
|------|---------------|
| `plots.geojson` | A FeatureCollection of 5 valid plots (cocoa/soy/palm in GH/CI/BR/ID), each with a Whisp `_whisp` provenance block. The feature `id` becomes the geoid's collection-scoped `external_id`. Includes a MultiPolygon estate. |
| `duplicate_of_GH-COCOA-001.geojson` | The same geometry as GH-COCOA-001 but reversed winding and no id → **duplicate rejected** (POST returns 409 with the incumbent geoid in the body — geometry uniqueness is catalog-wide). |
| `invalid_selfintersecting.geojson` | A self-intersecting bow-tie → **reject-don't-repair** (422 + `ST_IsValidReason`). |

## Run it

```bash
# 1. bring the stack up (use GEOID_DB_PORT if 5432 is taken locally)
GEOID_DB_PORT=5433 docker compose up -d --build

# 2. seed + demonstrate mint / dedup / validation / OGC read / bulk export
uv run python scripts/seed_samples.py
```

Then explore `http://localhost:8000/docs`, or:

```bash
curl -s 'http://localhost:8000/collections/public/items?limit=100' | jq '.numberMatched'
curl -s 'http://localhost:8000/collections/public/bulk' -o public.geojson   # copyable open data
ogrinfo "OAPIF:http://localhost:8000" public                                # a real ogr client
```
