# Load / QA

These exercise the three hot paths to demonstrate **correct dedup under load** and a
**credible scaling story** (single-instance, few-hundred RPS) — not billion-scale.

| Path | What it measures | Target (demo) |
|------|------------------|---------------|
| `mint`  | single-POST (mint + insert) p95 | `< 500 ms` |
| `dedup` | dedup-lookup p95 (same geometry) | `< 300 ms` |
| `read`  | OGC items read p95 | `< 300 ms` |

## Prereqs

A running API + PostGIS (e.g. `docker compose up` from the repo root), and the
reserved `public` collection (auto-created on startup).

## k6 (preferred, no Python dep)

```bash
brew install k6   # or https://k6.io/docs/get-started/installation/
GEOID_BASE_URL=http://localhost:8000 k6 run tests/load/k6_smoke.js
```

The run fails its thresholds if p95 latencies or the error rate exceed the targets
above. `geoid_dedup_hits` counts how many `dedup` POSTs returned `200` (the
incumbent geoid) — proof the dedup path is firing under concurrency.

## Locust (alternative)

```bash
pip install locust          # not a project dependency
locust -f tests/load/locustfile.py --host http://localhost:8000
# open http://localhost:8089
```

## QA with real data

Port a handful of Asset Registry 1.0 / Whisp features into `public` and confirm:
1. identical geometries collapse to one geoid (dedup),
2. a real **QGIS / ogr** client loads the collection
   (`ogrinfo "OAPIF:http://localhost:8000" public`),
3. a Whisp-style feature is accepted as-is (its `properties`, incl. the `_whisp`
   block, are accepted but not stored).
