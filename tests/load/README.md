# Load / performance testing

Load tests are **on-demand campaign tools, never CI gates** — the deploy pipeline gates
on the functional suite, the golden-vector identity canary, and the post-deploy smoke
test; latency/throughput work runs out-of-band against a deployed environment.

## The blessed harness

`scripts/loadtest.py` — dependency-free (httpx via `uv run`), correct on the current API
contract: a duplicate-geometry POST is a **409 carrying the incumbent geoid** (never a
200/201), bulk is `POST /collections/{id}/items/bulk` → 200 + `BulkReport`.

```bash
uv run python scripts/loadtest.py --help
```

The full stress/scalability campaign kit (corpus generator, bulk-ingest runner,
read/write concurrency ramps, Cloud Monitoring pulls, wipe SQL) lives in the gitignored
`local-scripts/stress-campaign/`; campaign reports land in `local-scripts/docs/`.

Historical note: the k6/locust scripts that used to live here predated the dedup-409
contract (they scored a dedup 409 as a failure) and were removed 2026-07-17 — don't
resurrect them; extend `scripts/loadtest.py` or the campaign kit instead.

## When to re-run a campaign

No schedule. Re-run on these triggers:

- the bulk-write contract or `GEOID_BULK_MAX_FEATURES` changes
- Cloud Run instance shape / concurrency or Cloud SQL tier is resized
- before onboarding the real ~40k-feature client load
- a DB-tier or scaling decision needs fresh numbers (see `local-scripts/docs/SCALING.md`)

## QA with real data

Port a handful of Asset Registry 1.0 / Whisp features into `public` and confirm:
1. identical geometries collapse to one geoid (dedup 409 naming the incumbent),
2. a real **QGIS / ogr** client resolves a geoid
   (`GET /{geoid}` — there is no public items listing to load as a layer),
3. a Whisp-style feature is accepted as-is (its `properties`, incl. the `_whisp`
   block, are accepted but not stored — geoid-prov/0.2).
