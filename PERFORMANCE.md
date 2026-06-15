# GeoID — Performance & Scaling

> **Two honesty caveats up front.**
> 1. **Numbers here are a pessimistic lower bound.** The local stack runs PostGIS
>    under **amd64 emulation on Apple Silicon** (pinned for GEOS hash parity with
>    Cloud SQL). Emulated Postgres is several times slower than native — run the
>    harness on the target infra (Cloud Run + Cloud SQL) for representative figures.
> 2. **The demo target is already met.** A single emulated instance sustains
>    **~530–590 RPS** on the write paths and **~330 RPS** on reads at p50 ≈ 12–43 ms
>    — well past the plan's "a few hundred RPS on one instance" goal. So the work
>    below is about *scale-invariance and headroom*, not chasing raw RPS, in line
>    with the plan's "ship correct first, scale later".

## How to measure

```bash
# stack up (GEOID_DB_PORT avoids a local 5432 clash)
GEOID_DB_PORT=5433 docker compose up -d --build

# self-contained async harness (no k6/locust needed) — mint / dedup / read
uv run python scripts/loadtest.py --scenario all --concurrency 16 --duration 10

# k6 (preferred for real runs) and Locust scripts also provided
GEOID_BASE_URL=http://localhost:8000 k6 run tests/load/k6_smoke.js
```

The three hot paths: **mint** (POST unique polygon → 201), **dedup** (POST identical
geometry → 409 carrying the incumbent geoid; the harness counts 409 as success for
this scenario — it IS the measured path: arbiter conflict + incumbent lookup),
**read** (`GET items?limit=50`).

## Baseline (emulated lower bound, concurrency=16, 8 s)

| Path  | RPS  | p50 ms | p95 ms | p99 ms |
|-------|-----:|-------:|-------:|-------:|
| mint  | ~579 |   11.4 |  103.6 |  221   |
| dedup | ~550 |   14.7 |   97.7 |  251   |
| read  | ~327 |   42.9 |   73.3 |  160   |

`read` is measured against **34,289 rows in one collection** (after bulk-seeding);
it stayed at ~73 ms p95 despite an 8× data increase — see the index result below.

> **mint / dedup re-measured 2026-06-15** after the global geometry-dedup UNIQUE
> moved to `geoid_registry` (the write is now a one-statement arbiter CTE). Warm-run
> numbers match the prior baseline (~554 / ~590) within emulated run-to-run
> variance — **no write-path regression** (same three tables written, same one
> round-trip). A cold *first* run on the fresh volume dipped to ~370 mint RPS; that
> is fresh-cache warmup, not the relocation. The `read` row is retained from the
> 34,289-row measurement — the relocation does not touch the read path, and this
> run's read was against a near-empty collection.

## Correctness under load (a stress test, not just throughput)

`tests/integration/test_concurrency.py` fires **16 simultaneous identical POSTs**
and asserts they converge to **exactly one geoid and one `place` row** (one 201,
fifteen 409s — each 409 body carrying the winner's geoid). This proves the arbiter
CTE (registry `ON CONFLICT … DO NOTHING` + incumbent-lookup) race is conflict-free
under concurrency — the load-bearing dedup guarantee. A companion test fires 16
*distinct* POSTs and asserts all mint.

## Optimizations applied (with evidence)

### 1. Composite paging index — the load-bearing read fix (migration 0002)

`CREATE INDEX place_collection_created_id_idx ON place (collection_id, created_at, id)`
backs `WHERE collection_id=:c ORDER BY created_at, id LIMIT/OFFSET`. EXPLAIN ANALYZE
of the page-1 query at 34,289 rows:

| | Plan | Exec time | Buffers |
|---|---|---:|---:|
| **with index** | Index-Only Scan | **2.6 ms** | **8** |
| without index  | Seq Scan(34,289) + top-N heapsort | 9.4 ms | 1,131 |

The time is ~3.6× better, but the **buffer count (1,131 → 8)** is the real story:
the plan is now **O(limit), independent of collection size**. At ~1e6 rows the
seq-scan path degrades ~30× while the index stays flat. (The unfiltered `count(*)`
for `numberMatched` also uses this index as an index-only scan once a collection is
a *subset* of the table; when a collection is ~the whole table the planner correctly
prefers a seq scan.)

### 2. Batched collection extents — kills an N+1

`GET /collections` previously ran one `ST_Extent` full scan **per collection** in a
Python loop. `place_repo.collection_extents()` now computes all of them in **one
`GROUP BY` query** (`place_repo.py`), so the endpoint is one scan instead of N.

### 3. True streaming bulk export — flat memory

`iter_collection_geojson` now sets `stream_results=True` (asyncpg server-side
cursor), so the public `/bulk` endpoint streams in batches instead of buffering the
whole collection in memory before the first byte. (The engine also sets a bounded
pool + `statement_timeout` / `idle_in_transaction_session_timeout` so a slow client
can't pin a connection indefinitely.)

### 4. One-round-trip write happy path

The per-POST pre-validation `SELECT` (ST_GeomFromGeoJSON + ST_IsValid) was removed
from the happy path. The DB `CHECK` constraints already reject invalid geometry
(SQLSTATE 23514); we recover `ST_IsValidReason` for the 422 body **only on that
error path** (`registry_service.create_place`). Valid POSTs drop from 3 DB
round-trips to 2. Effect at demo scale is a modest constant-factor win (mint p99
384→294 ms, dedup p50 14→11.8 ms) — the 422-with-reason and reject-don't-repair
contracts are unchanged (all geometry-validation tests stay green).

Since the global geometry-dedup UNIQUE moved onto `geoid_registry`, the write is now
a **single-statement arbiter CTE**: one statement inserts the `geoid_registry` row
(`ON CONFLICT … DO NOTHING` on `uq_geoid_registry_geom_hash`) and the `place` row
only if the arbiter won. It is **still one round-trip** for the happy path (the
registry + place inserts ride in the same statement), so the structural cost is
unchanged — and a **re-measurement on 2026-06-15 confirmed** mint/dedup hold within
emulated run-to-run variance of the prior baseline (see the Baseline table above).

### 5. Explicit uvloop + httptools

`geoid web` now runs uvicorn with `loop="uvloop", http="httptools"` (fail-loud if
the fast wheels are missing) and honours `WEB_CONCURRENCY` for multi-worker
processes.

## Recommended demo config (Cloud Run + Cloud SQL)

- **Cloud Run:** `--min-instances=1` (no cold start at the demo), `--max-instances=5`
  (bounds the connection math), `--concurrency≈20–40`, `--cpu=1 --memory=512Mi`.
- **Pool:** keep `GEOID_DB_POOL_SIZE`/`GEOID_DB_MAX_OVERFLOW` modest so
  `max-instances × (pool+overflow)` stays under the Cloud SQL `max_connections`
  (e.g. 5 × 30 = 150). Tune `--concurrency` to the pool so requests don't queue on
  `db_pool_timeout`.
- **asyncpg statement cache:** leave it **on** for a direct/private-IP Cloud SQL
  connection (it speeds the repeated text() queries). If a *transaction-mode* pooler
  (PgBouncer) is later introduced, set `statement_cache_size=0` then.

## Deferred — the post-demo scaling roadmap

> The **tooling verdict** (Postgres vs. +DuckDB / +Redis / +parallelism), the
> **billion-row scaling ladder**, and the **dedup-under-sharding crux** live in
> [`docs/SCALING.md`](docs/SCALING.md) (recorded ADR-style). The bullets below are
> the measured-baseline view of that roadmap; `SCALING.md` cross-references each.

Intentionally **not** built now (YAGNI; the plan defers scaling):

- **Keyset/cursor paging** to replace OFFSET (OFFSET cost grows with depth; the
  composite index already makes the keyset variant an O(limit) range scan). In the
  interim, `GEOID_MAX_OFFSET` (default 100k) caps offset depth on both the public
  items endpoint and the admin item-ids listing, and `rel=next` links stop at the
  cap. Consequence: full id enumeration of a collection larger than
  `max_offset + limit` needs the cap raised until keyset paging lands.
- **Approximate / optional `numberMatched`** (the OGC spec allows omitting it) via
  `pg_class.reltuples` or a maintained per-collection counter, for collections too
  large to count exactly per request.
- **`ST_Subdivide`** derived index for collections holding very large / high-vertex
  polygons (keeps GiST `ST_Intersects` selective).
- **A vertex-count guard** (`ST_NPoints`) to reject or async-queue pathological
  geometries.
- **Partition `place` by `collection_id` → Citus shard** (the `geoid_registry`
  hinge already holds **both** global-uniqueness invariants separately — geoid (PK)
  *and* geometry (`uq_geoid_registry_geom_hash`, relocated there in `0001`) — so
  `place` carries no global UNIQUE and this stays additive: under Citus
  `geoid_registry` becomes a *reference table*, since Citus requires unique keys to
  include the distribution column).
- **Read replicas** for the OGC read path (the write path needs the primary; the
  read path is replica-safe).
- **Federation pull-feed** over the monotonic `change_log.seq` cursor.
- **Batch geoid lookup** — `POST /geoid/lookup` taking a list of geoids with a
  configurable cap (FAO DynaStore uses 10k), partitioning malformed UUIDs out
  *before* the DB query so one bad input never fails the batch (their
  `_partition_uuid_inputs` pattern); response is a GeoJSON FeatureCollection with
  `numberRequested`/`numberReturned`/`invalid`/`notFound` foreign members.
  Mirroring DynaStore's request/response shapes keeps a future merge of the two
  products cheap, whichever direction it goes.
- **Batch write + job-based bulk ingest/export** — `POST .../items` accepting a
  whole FeatureCollection with per-row rejections and HTTP **207**
  `IngestionReport{accepted_ids, rejections[], total}` partial-success semantics
  (each rejection carries the matcher that fired: geometry-dedup vs external-id),
  again mirroring DynaStore's shapes; larger file-based ingest/export runs as
  async jobs on a `FOR UPDATE SKIP LOCKED` PostgreSQL queue (no new
  infrastructure) and rides on the same milestone.
