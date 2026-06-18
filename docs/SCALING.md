# GeoID — Scaling Verdict & ADR

> **Scope.** This document is the *verdict* on how GeoID scales toward billions of
> geometries and whether PostgreSQL alone is enough, recorded ADR-style so the
> reasoning survives. It is the home of the **tooling decision** (Postgres vs.
> +DuckDB / +Redis / +parallelism) and the **billion-row scaling ladder**.
> Measured baselines and applied optimizations live in
> [`../PERFORMANCE.md`](../PERFORMANCE.md); this doc cross-references its deferred
> roadmap rather than restating it.
>
> **Status.** The *decisions* below are recorded now. The bulk **write** they
> motivate now ships as **one synchronous route** — `POST /collections/{id}/items/bulk`
> takes a GeoJSON `FeatureCollection`, sized for hundreds-to-thousands of geometries
> (see §8). The heavier async pipeline (OGC API - Processes/Jobs, the `ingest_job`
> queue, `ingest-worker`, GCS export) was **removed as overkill** for the actual
> requirement; the set-based `COPY`-staging ladder below stays the recorded plan for
> *if* volumes ever reach millions. The *deferred* columnar/keyset items remain the
> "soon", not the "now".

---

## 1. Verdict in one line

**Keep PostgreSQL + PostGIS as the authoritative system of record; augment it for
specific access patterns; do not replace it.**

PostGIS uniquely owns geometry validation (`ST_IsValid` / `ST_MakeValid`), the
SHA-256 normalized-geometry dedup recipe
(`sha256(ST_AsBinary(ST_Normalize(ST_ReducePrecision(ST_MakeValid(geom), 1e-7)), 'NDR'))`),
GiST spatial filtering (`ST_Intersects` / `ST_Within`), trigger-enforced
immutability, and the OGC OLTP read surface. **Nothing in DuckDB or Redis replaces
those.** Every augmentation below is *additive* and *threshold-triggered* — adopted
when a named metric crosses a named line, never speculatively.

**The single most important finding is not about throughput.** Demo-scale RPS is
already met (~530–590 write RPS, ~330 read RPS on one emulated instance —
[`PERFORMANCE.md`](../PERFORMANCE.md)). GeoID's hardest scaling problem *was*
preserving its **global geometry-dedup invariant** — a `UNIQUE` on `geom_hash`,
which is a *non-partition* column — once `place` is partitioned or sharded. **That
invariant has already been relocated** off `place` onto the unpartitioned
`geoid_registry` (`uq_geoid_registry_geom_hash`, enforced live since the `0001`
squash), so `place` is sharding-ready *in fact*, not just in plan. The scaling
story is now dominated by that (resolved) relocation (§3), not by raw write speed.

---

## 2. The scaling ladder

Sequenced rungs. **Exhaust each rung before climbing to the next** — every rung up
adds operational cost, and the demo targets are already met two rungs below where
most "scale Postgres" advice starts.

| Rung | Trigger to climb | Move | GeoID specifics |
|---|---|---|---|
| **0 — now** | < ~100 GB; demo RPS met | Tuned indexes + bounded pool | Already done: composite paging index (migration 0002), one-round-trip write happy path, batched extents, streaming export. Next cheap wins: **keyset paging**, **approximate `numberMatched`**, **PgBouncer** once `max-instances × (pool+overflow)` nears Cloud SQL `max_connections`. |
| **1** | Read pressure on the OGC surface | **Read replicas** | OGC `GET` is replica-safe; `POST` stays on the primary. Needs a read/write session split in `db.py`. Watch **read-your-writes**: route a writer's own immediate read back to the primary. |
| **2** | `place` too large / write volume saturates one table | **Partition `place`** (native declarative, hash by `collection_id`) | **Already unblocked:** a partitioned-table unique must include the partition key (§3), so global `UNIQUE(geom_hash)` cannot live on a partitioned `place` — but it no longer does. Global geometry-uniqueness already lives on the **unpartitioned `geoid_registry`** (`uq_geoid_registry_geom_hash`, since `0001`); partitioning `place` needs no schema relocation. |
| **3** | Single primary saturated (CPU / IOPS / connections) | **Citus** — `place` distributed by `collection_id`, `geoid_registry` as a **reference table** | Same constraint as rung 2: a Citus distributed unique must include the distribution column. Global dedup already lives on `geoid_registry`; rung 3 only makes that table a **replicated reference table** so its `UNIQUE` is enforced cluster-wide. Mirrors Instagram's logical-shards pattern (§4). |
| **Parallel track** | OLAP / billion-row export / analytical scans | **Columnar offload** (DuckDB / Parquet) | Runs *off* the primary. Periodic Parquet snapshots to GCS via a future snapshot job (an object-storage seam re-introduced if/when this is built). Never burden the OLTP primary with analytical full scans. Independent of rungs 1–3 — adopt whenever analytical read demand appears. |

---

## 3. The dedup-under-scale crux (the heart of the verdict)

GeoID enforces **one geometry → one geoid across the whole catalog** via a single
`UNIQUE (geom_hash)` — now `uq_geoid_registry_geom_hash` on `geoid_registry` (it
*used* to be `uq_place_geom_hash` on `place`; the relocation below is done). This is
a content-addressed, *global* uniqueness invariant on a column that is **not** a
natural partition key. That is precisely the property that horizontal scaling cannot
preserve for free — and the reason it lives on the unpartitioned registry, not on
the partition/shard candidate `place`.

### What the databases actually require (verified against primary docs)

- **PostgreSQL — declarative partitioning.** From the
  [partitioning docs](https://www.postgresql.org/docs/current/ddl-partitioning.html),
  §5.12.2.3 (Limitations), verbatim:

  > "To create a unique or primary key constraint on a partitioned table, the
  > partition keys must not include any expressions or function calls and the
  > constraint's columns must include all of the partition key columns. This
  > limitation exists because the individual indexes making up the constraint can
  > only directly enforce uniqueness within their own partitions; therefore, the
  > partition structure itself must guarantee that there are not duplicates in
  > different partitions."

  ⇒ On a `place` partitioned by `collection_id`, a bare `UNIQUE (geom_hash)` is
  **impossible**. We could only get `UNIQUE (collection_id, geom_hash)` — which is
  *per-collection*, not the global invariant GeoID requires.

- **Citus — distributed tables.** From the
  [Citus DDL reference](https://docs.citusdata.com/en/v11.2/develop/reference_ddl.html),
  verbatim:

  > "Primary keys and uniqueness constraints must include the distribution column."

  and: *"Adding them to a non-distribution column will generate an error."* ⇒ On a
  `place` distributed by `collection_id`, `UNIQUE (geom_hash)` is again rejected
  outright.

### GeoID's resolution (an engineering decision the architecture pre-committed to)

The fix — **now implemented** — is to **move global geometry-uniqueness off `place`
and onto a separate, unpartitioned table** (`geoid_registry`): a plain unique index
on a normal table under native partitioning (rung 2), and a **Citus reference
table** under sharding (rung 3). A Citus reference table is replicated in full to
every worker node, so a `UNIQUE` on it is enforced cluster-wide — which is exactly
the global guarantee `place` loses once distributed.

**Honesty note on the citation.** The two docs above rigorously establish the
*constraint* (a partitioned/distributed unique must include the partition/
distribution column). Neither page literally prescribes "use a separate
non-partitioned table to hold the global unique" as the named workaround — the
Postgres page does not mention it, and the Citus page frames reference tables as a
*choice* for small, cross-column-unique data. The separate-table resolution is
**GeoID's own design inference**, sound because (a) an unpartitioned table sidesteps
the partition-key rule entirely, and (b) Citus reference-table replication makes a
reference-table `UNIQUE` genuinely global. Treat it as an architectural decision
grounded in verified database semantics, not as a verbatim vendor recommendation.

### The registry hinge now IS that table

`geoid_registry` was explicitly designed for this role
(`migrations/versions/0001_initial.py`) and now fills it: it carries `geom_hash` +
`uq_geoid_registry_geom_hash` (the relocated global invariant), is **append-only**,
and carries **no FK to `place`** (Citus reference tables cannot FK into distributed
tables, and the FK would serialize cross-shard inserts) — the migration comments
call it "Citus-shard ready". The app's **arbiter CTE** (`place_repo.insert_place`)
is its writer: it inserts the registry row (`ON CONFLICT … DO NOTHING` on the
geom_hash UNIQUE) and writes the `place` row only if the arbiter won, in one
statement. The `place_after_insert` trigger now only appends `change_log`; the
append-only guard triggers keep registry rows immutable.

### Known-unknown — resolved and implemented in `0001`

The plan flagged a question: *does `geoid_registry` carry `geom_hash`, or only the
geoid?* It originally carried only `geoid` (PK), `place_id`, and `collection_id`.
That gap is now **closed** — the relocation was folded into `0001_initial.py` (a
pre-launch squash; no users, no data, so no new migration and no backfill). The
squash:

1. adds `geom_hash bytea NOT NULL` to `geoid_registry`,
2. adds `CONSTRAINT uq_geoid_registry_geom_hash UNIQUE (geom_hash)` there (the
   relocated global invariant),
3. drops `place.geom_hash` and `uq_place_geom_hash` (and the `BEFORE INSERT` hash
   trigger — the user-trigger inventory is now **7**, was 8),
4. pins the grid in a `geoid_geom_hash_default(geom)` SQL wrapper, and
5. makes the app's **arbiter CTE** the writer of the registry row and the
   incumbent lookup (both via that wrapper); `place_after_insert` now only appends
   `change_log`.

So **`place` carries no global UNIQUE and is partition/shard-ready today.** The
gating dependency for rungs 2 and 3 is **discharged** — the collision policy in
`scripts/rehash_geom_hashes.py` (incumbent-unchanged wins, else earliest UUIDv7)
already operates on `geoid_registry.geom_hash`.

---

## 4. The Instagram talk — what transfers, what doesn't

Source: *"How Instagram Scaled Postgres to 2 Billion Users"* (Algoroq, YouTube
`YLoYcwnqVzM`). YouTube transcripts are not fetchable server-side, so the substance
was cross-checked against the primary
[Instagram Engineering — "Sharding & IDs at Instagram"](https://instagram-engineering.com/sharding-ids-at-instagram-1cf5a71e5a5c).

**What transfers to GeoID**

- **A natural partition key.** Instagram shards by `user_id`; GeoID's analogue is
  `collection_id`. Both give even-ish fan-out without a global hotspot.
- **Logical shards as schemas.** Instagram packs many logical shards into few
  physical machines via Postgres schemas; Citus implements the same idea natively.
- **Replication alongside sharding** for read scale-out.
- **Push work into the database.** GeoID already does this hard — dedup, identity
  minting context, immutability, and audit are all DB-enforced (7 triggers + the
  canonical hash function and its grid-pinned `geoid_geom_hash_default` wrapper,
  the dedup arbiter being a single-statement CTE), not application logic that can
  drift.

**What GeoID already has**

- **Time-ordered reads via the composite index, NOT the PK.** Instagram engineered
  time-sortable IDs so `ORDER BY id` ≈ `ORDER BY created_at`. GeoID deliberately gives
  up that PK property: the geoid is a **deterministic, content-addressed UUIDv8**
  (ADR-004), so it is *not* time-ordered and distributes randomly in the b-tree (the
  cost of federation-stable identity — see ADR-004's consequences). Time-ordered
  paging instead leans entirely on the composite index `(collection_id, created_at,
  id)`, which is why that index — not the PK — is the load-bearing paging structure.
- **Caveat:** the geoid does **not** embed a shard id. Routing is therefore by
  `collection_id`, not by parsing the geoid — you cannot recover the shard from the
  identifier the way Instagram's IDs encode it.

**The crucial difference (why the talk is necessary but not sufficient)**

Instagram sharded **user-partitioned data with no cross-shard uniqueness
requirement** — two users' rows never need to be unique against each other. GeoID
carries a **global content-dedup invariant** (`geom_hash`) that *spans* every
collection and therefore *fights* sharding (§3). The Instagram playbook gets `place`
distributed; it says nothing about preserving a global unique across shards. That
gap is GeoID-specific and is the reason the scaling story is dominated by the
registry hinge rather than by throughput.

---

## 5. Tooling verdict — Postgres alone, or +DuckDB / +Redis / +parallelism?

| Tool | Verdict | Role / when |
|---|---|---|
| **PostgreSQL + PostGIS** | **Keep — system of record.** | All writes, geometry validation, the dedup recipe, GiST spatial filtering, OGC OLTP reads, and the `change_log` event log. Non-negotiable; everything else orbits it. |
| **Parallelism / bulk loading** | **Adopt for bulk ingest — highest near-term value.** | `COPY` into an **UNLOGGED/TEMP staging table** (asyncpg `copy_records_to_table`), then one set-based `INSERT … SELECT … ON CONFLICT DO NOTHING` — orders of magnitude faster than per-row `INSERT` at millions of rows. **PgBouncer** (transaction pool) for connection fan-out (set asyncpg `statement_cache_size=0` when that pooler lands — already flagged in `PERFORMANCE.md`). Postgres **parallel query** for analytical counts/exports. Async job queue via `SELECT … FOR UPDATE SKIP LOCKED` — no new infrastructure. |
| **DuckDB** | **Adopt later — OLAP/export complement, never a replacement.** | Bulk *fetch* and analytics at billions of rows: periodic **Parquet snapshots** of `place` / `geoid_registry` to GCS (via a re-introduced object-storage seam), queried by DuckDB far faster than OFFSET pagination — and **off the primary**. Optional `pg_duckdb` on a read replica. **Never** for writes, validation, or topology — it does not own the geometry recipe. |
| **Redis / caching** | **Adopt later — accelerator that must fail-open.** | (a) **Cache-aside** for OGC reads: because `place` is immutable, invalidation is trivial — bump a per-collection version key on insert; `GET /…/items/{geoid}` is effectively cache-forever. (b) **Bloom filter** of known `geom_hash` to pre-skip dedup DB hits during bulk ingest — the DB `UNIQUE` stays the source of truth. **Redis down ⇒ fall back to Postgres** (slower, still correct). Not needed at demo scale. |

The shape of the verdict: **one authoritative engine, a fast bulk-load path on top
of it, and two optional accelerators (columnar reads, cache) that are correctness-
neutral** — if either accelerator is absent or down, the system is slower but never
wrong.

---

## 6. DDIA principles applied (Kleppmann)

- **Replication (Ch. 5).** Single-leader (Postgres streaming replication) is the
  right model; GeoID needs no multi-leader / multi-master. Append-only immutability
  means **zero write-write replication conflicts** — DDIA's hardest replication
  problem is *designed out* of the data model. The one thing to mind is
  read-your-writes on replicas (§2, rung 1).
- **Partitioning (Ch. 6).** Hash by `collection_id` (even distribution, no hotspot)
  over time-based partitioning (which hotspots the newest partition — exactly where
  GeoID's writes land). Global `geom_hash` uniqueness is DDIA's expensive
  *global / term-partitioned secondary index*; GeoID answers it with the
  reference-table hinge (§3) rather than a scatter-gather index.
- **Transactions (Ch. 7).** The arbiter CTE (registry `ON CONFLICT DO NOTHING` +
  incumbent lookup) is a race-safe upsert (proven under 16-way concurrent identical
  POSTs — see `PERFORMANCE.md` and `tests/integration/test_concurrency.py`). Immutability
  removes lost-update and write-skew anomalies. **Keep `READ COMMITTED`** — the
  incumbent lookup is known to break under `REPEATABLE READ` (the conflicting row
  inserted by a concurrent transaction is invisible to the snapshot, so the lookup
  finds nothing to return).
- **Batch vs. stream / derived data (Ch. 10–11).** `change_log` is already an
  ordered event log keyed by a monotonic `seq` (`bigserial` PK) — DDIA's
  "log as source of truth". Bulk ingest is the *batch* job; the federation pull-feed
  over `change_log.seq` is the *stream* with **consistent-prefix reads**; the
  Parquet/DuckDB snapshots are *derived materialized views*. The architecture
  already lines up with the chapter's framing.
- **Consistency (Ch. 9).** No distributed consensus is required. Global uniqueness
  stays a **single linearizable point** — one Postgres primary today, the
  reference table under sharding tomorrow. This is *bounded, deliberate
  centralization* of the one invariant that needs it, rather than paying for full
  consensus across the whole system.

---

## 7. ADR decisions (recorded, with status)

Each **DEFERRED** decision names its trigger metric and links to the matching
[`PERFORMANCE.md`](../PERFORMANCE.md) deferred bullet so the two docs cannot drift.

### Accepted

- **ADR-001 — Postgres + PostGIS is the system of record.** It owns validation,
  dedup, spatial filtering, immutability, audit. No replacement. *(See §1, §5.)*
- **ADR-002 — Bulk ingest via `COPY` → UNLOGGED staging → set-based
  `ON CONFLICT DO NOTHING`.** Preserves every existing trigger-enforced invariant
  while getting batch throughput. *(Implements `PERFORMANCE.md` → "Batch write +
  job-based bulk ingest/export".)*
- **ADR-003 — Global geometry-uniqueness lives on the unpartitioned/reference
  `geoid_registry` — IMPLEMENTED in `0001` (the pre-launch squash).**
  `geoid_registry` carries `geom_hash` + `uq_geoid_registry_geom_hash`, `place`
  carries no geom_hash UNIQUE, and the app's single-statement arbiter CTE enforces
  dedup. This is the sanctioned cross-shard uniqueness mechanism and is now live, so
  partitioning/sharding inherit it for free (under Citus, `geoid_registry` becomes a
  reference table). *(See §3; `PERFORMANCE.md` → "Partition `place` … Citus shard".)*
- **ADR-004 — Augmentations are additive and threshold-triggered, and accelerators
  must fail-open.** DuckDB and Redis may never sit on the correctness path; if
  absent, the system is slower, not wrong. *(See §5.)*

### Deferred (with trigger metric)

- **ADR-005 — Read replicas.** *Trigger:* OGC read RPS sustained near primary CPU
  limits, or read latency p95 regressing under write load. *Link:* `PERFORMANCE.md`
  → "Read replicas".
- **ADR-006 — Partition `place` (native, hash by `collection_id`).** *Trigger:*
  `place` size or single-table write volume degrades planning/vacuum; ≳ ~100 GB or
  ~1e8 rows as a rule of thumb. *Prereq:* ADR-003 — **complete** (the registry
  already holds the global UNIQUE). *Link:*
  `PERFORMANCE.md` → "Partition `place` by `collection_id` → Citus shard".
- **ADR-007 — Citus (distribute `place` by `collection_id`; `geoid_registry` as a
  reference table).** *Trigger:* a single primary is saturated on CPU/IOPS/
  connections after replicas + partitioning. *Prereq:* ADR-003 (**complete**),
  ADR-006. *Link:* same `PERFORMANCE.md` bullet as ADR-006.
- **ADR-008 — DuckDB / Parquet columnar offload.** *Trigger:* analytical or
  billion-row export demand that would force full scans on the OLTP primary.
  *Link:* this document is the system-of-record for the columnar decision; the
  `PERFORMANCE.md` pointer (added this pass) routes readers here.
- **ADR-009 — Redis cache-aside + `geom_hash` bloom filter.** *Trigger:* OGC read
  RPS or bulk-ingest dedup-probe volume exceeds what tuned Postgres serves within
  latency budget. *Link:* as ADR-008, recorded here with a `PERFORMANCE.md` pointer.
- **ADR-010 — Keyset (seek) paging to replace OFFSET.** *Trigger:* deep enumeration
  needs to exceed `GEOID_MAX_OFFSET`, or OFFSET-depth latency regresses. *Link:*
  `PERFORMANCE.md` → "Keyset/cursor paging".
- **ADR-011 — Approximate / optional `numberMatched`.** *Trigger:* a collection
  grows too large to `COUNT(*)` exactly within the request budget. *Link:*
  `PERFORMANCE.md` → "Approximate / optional `numberMatched`".

---

## 8. The bulk write this verdict motivates (synchronous; the async ladder is deferred)

The verdict above existed to de-risk the **bulk ingest/fetch** work named in
`PERFORMANCE.md`'s roadmap. Remi then **simplified the requirement**: the real need is
to submit *hundreds to a few thousand* geometries in one request, **synchronously** —
not a file-upload / async-job pipeline. So the heavyweight OGC API - Processes/Jobs
surface, the durable `ingest_job` queue (migration `0004`), the `ingest-worker`, the
GCS export, and the storage/notify seams were **removed as overkill**, and the bulk
write is now **one route**.

**The route.** `POST /collections/{id}/items/bulk` takes a GeoJSON `FeatureCollection`
(RFC 7946 §3.3) and returns **HTTP 200** with a `BulkReport{ summary{received,
accepted, rejected}, accepted[{index, geoid, uri, item_url, external_id}],
rejected[{index, reason, detail, geoid?, uri?, collection?, external_id?}] }`.
`GEOID_BULK_MAX_FEATURES` (default **1000**) caps the body — a write bound **errors
413**, never truncates. Per-feature **partial success** is the contract: one bad
geometry or duplicate never fails the batch, and each reject's `reason` maps 1:1 to
the single-row 4xx (`schema_invalid` / `invalid_geometry` 422; `geometry_conflict`
[+ incumbent geoid] / `external_id_conflict` / `geoid_conflict` 409). It **reuses the
single-row machinery verbatim** — `PlaceCreate` validation, then the same
`place_repo.insert_place` arbiter CTE (`INSERT INTO geoid_registry … ON CONFLICT ON
CONSTRAINT uq_geoid_registry_geom_hash DO NOTHING`, always via
`geoid_geom_hash_default`) — so bulk and single-row geoids/hashes are byte-identical.
Each feature runs in its own **SAVEPOINT** (`begin_nested`) so an aborting insert
(external_id / CHECK / malformed GeoJSON) rolls back just that feature; a geometry
duplicate is swallowed by the arbiter without aborting at all. Auth mirrors the single
route (anon only into a `writable_anon` collection), checked once up front (403).

**If volumes ever grow.** The set-based `COPY` → TEMP-staging → one set-based arbiter
CTE path (ADR-002 above; `copy_records_to_table`, text/jsonb, classify-and-DELETE the
abort risks before the insert) remains the **recorded plan** for a future
millions-of-rows load, alongside an async `SELECT … FOR UPDATE SKIP LOCKED` job queue
and an export process. None of it is built today — the synchronous route covers the
requirement, and the ladder is re-openable without re-deciding. Still deferred from
§5/§7: keyset paging to retire OFFSET (ADR-010); DuckDB-off-primary Parquet snapshots
(ADR-008); approximate `numberMatched` (ADR-011).

---

## Sources (verified this pass)

- **PostgreSQL — table partitioning limitations** (unique/PK must include all
  partition key columns), quoted verbatim in §3:
  <https://www.postgresql.org/docs/current/ddl-partitioning.html> (§5.12.2.3).
- **Citus — DDL reference** (PK/unique must include the distribution column),
  quoted verbatim in §3:
  <https://docs.citusdata.com/en/v11.2/develop/reference_ddl.html>.
- **Instagram Engineering — "Sharding & IDs at Instagram"** (primary source used to
  verify the video's substance, §4):
  <https://instagram-engineering.com/sharding-ids-at-instagram-1cf5a71e5a5c>.
  Video: *"How Instagram Scaled Postgres to 2 Billion Users"* (`YLoYcwnqVzM`,
  Algoroq) — transcript not fetchable server-side.
- *Designing Data-Intensive Applications*, Martin Kleppmann — replication (Ch. 5),
  partitioning (Ch. 6), transactions (Ch. 7), consistency (Ch. 9), batch/stream &
  log-as-source-of-truth (Ch. 10–11), applied in §6.
- GeoID code verified first-hand this pass: `migrations/versions/0001_initial.py`
  (the `geoid_registry` schema resolving §3's known-unknown), and
  [`PERFORMANCE.md`](../PERFORMANCE.md) (baselines + deferred roadmap).
