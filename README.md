# GeoID

> A global, federated **places registry** that mints a globally unique, secure, **immutable** identifier
> (a "geoid", framed as a Decentralized Identifier) for every geospatial location, tracks provenance and
> authority, permits **anonymous contributions**, and serves everything over an **OGC API Features**
> read surface. Built as a **Digital Public Good** (Apache-2.0 code, open base data).

GeoID is one FastAPI service over one PostGIS schema, packaged as one Docker image. The product's value
is the **write path** — *mint an immutable geoid → deduplicate by canonical geometry → attach provenance
→ accept anonymous contributions*. The read path is standard OGC API Features.

```
QGIS / ogr / Whisp / Ground ──►  GeoID FastAPI (one image)
                                  ├─ POST place → mint geoid + dedup + provenance   ◄── the product
                                  ├─ GET by geoid / by (external_id, collection)   ◄── the only reads
                                  ├─ OGC landing + conformance (public); collections list/describe (admin)
                                  └─ /docs (Swagger)
                                          │
                                          ▼
                          PostgreSQL 17 + PostGIS 3.6.0 (Cloud SQL)
                          catalog ─< collection ─< place  (+ geoid_registry, change_log)
```

## Documentation

📖 **Full interactive technical walkthrough:** open [`docs/architecture.html`](docs/architecture.html)
in a browser — system architecture, ER + sequence + dedup diagrams, the write/read paths, and a
per-module reference for all 47 modules.

🎓 **Geospatial concepts & standards tutorial:** open [`docs/tutorial.html`](docs/tutorial.html)
in a browser — an interactive primer on every geospatial idea GeoID relies on (CRS & axis order,
GeoJSON↔WKB, polygon validity, the dedup hash, spatial indexing, OGC API Features,
the content-addressed UUIDv8 geoid), with live widgets and per-section self-tests grounded in the real code.

📋 **Stakeholder contract (shareable):** open [`docs/contract.html`](docs/contract.html) in a
browser — definitions & rules, the team's decisions with rationale, the API contract, a fully
offline interactive playground that simulates mint → dedup → validation, and the release roadmap.

Prose: this file.

## The identifier

Every place is assigned a content-addressed **UUIDv8** (RFC 9562 §5.8) **derived from its canonical
geometry** (the same `geom_hash` used for dedup), stored bare as the item id. On read we derive:

- a URI — `https://data.fao.org/geoid/<uuid>`

`POST` returns the geoid and its URI, and a 201 `Location` header pointing at that resolver URI. The
geoid is **immutable**: `place` is INSERT-only (enforced by a DB trigger);
corrections mint a *new* geoid linked via `predecessor_id`, and the original resolves forever.

Identity and deduplication now share **one fingerprint**: the geoid is derived from the same **global**
canonical `geom_hash` (see below) that enforces uniqueness, so the two can never disagree and the same
geometry yields the same geoid on every deployment. One geometry → one geoid across the whole catalog —
POSTing an identical geometry fails with **409** and the body carries the **incumbent geoid** (plus its
uri and collection).

## The dedup recipe (load-bearing correctness)

Computed inside the single arbiter-CTE insert (`place_repo.insert_place`) that both the single and bulk
write paths share, via the one grid-pinned `geoid_geom_hash_default` SQL function, so the hash can never
drift between paths (and, post-migration 0004, the geoid itself is derived from it):

```
geom_hash = sha256( ST_AsBinary(
              ST_Normalize( ST_ReducePrecision( ST_MakeValid(geom), grid ) ),
              'NDR' ) )
```

- **SHA-256** — a collision would assign the wrong geoid to a *different* place.
- **`ST_ReducePrecision(grid)`** — coordinates that round to the same `grid` cell collapse to one hash
  (`grid` is `1e-7` ≈ 1 cm — ONE global value, pinned as the trigger literal in the schema migration;
  `GEOID_DEDUP_GRID_DEFAULT` mirrors it for the conflict lookup, and retunes are migration events).
  Note: this snaps to a fixed grid, so two points a hair apart but straddling a cell boundary can still
  round to *different* cells — it neutralizes float jitter, not all sub-cm differences.
- **`ST_Normalize`** — canonical ring / part / hole order.
- **`'NDR'` endianness pinned** — so a country instance's hash matches central at federation sync.

A single SQL function `geoid_geom_hash(geom, grid)` — pinned to the global grid by the
`geoid_geom_hash_default(geom)` wrapper — is the *one* definition, used by both the arbiter-CTE insert
and the incumbent-lookup query.

## Quickstart (uv)

```bash
uv sync                       # create venv + install (editable) from the lockfile
cp .env.example .env          # set DATABASE_URL, GEOID_ADMIN_TOKEN, BASE_URL

# local stack (FastAPI + postgis/postgis:17)
docker compose up -d db
uv run alembic upgrade head   # apply schema + triggers
uv run uvicorn geoid.main:app --reload

# or the whole stack
docker compose up --build
```

Open `http://localhost:8000/docs` for Swagger, `http://localhost:8000/` for the OGC landing page.

> If host port 5432 is already in use, bring the stack up with `GEOID_DB_PORT=5433 docker compose up -d`.

## Try it with sample data

With the stack running, seed the dummy EUDR/Whisp-style plots in `samples/` and watch
mint → dedup → validation → resolve in one command:

```bash
uv run python scripts/seed_samples.py
```

Expected: 5 plots minted, a reversed-winding duplicate rejected with 409 + the incumbent geoid, a
self-intersecting polygon rejected (422), and the first plot resolved by its geoid.
See `samples/README.md` for details.

## Tests

```bash
uv run pytest -m unit                  # pure unit (no Docker)
uv run pytest -m integration           # ephemeral PostGIS via testcontainers (needs Docker)
uv run ruff check .
```

## Configuration

All configuration is environment-driven (see `.env.example`). The core image imports **no GCP SDK** —
every deployment difference is expressed as configuration — so the *same image* is the on-prem artifact
via `docker compose`.

## Destructive operations (operator-only, run manually)

These are **not shipped as runnable scripts** — they permanently alter the append-only registry and
must only be run by a DB admin (`cloudsqlsuperuser`) over the Cloud SQL Auth Proxy. The tooling lives
in `local-scripts/` (git-ignored, not in the image); the full runbook is `local-docs/DEPLOYMENT.md §14`.

**1. geom_hash rehash / drift-recovery.** If `bootstrap_db.py`'s golden-vector parity gate (or
`scripts/dedup_vectors.py --check`) reports drift, the stored `geoid_registry.geom_hash` values were
computed on a PostGIS/GEOS stack that no longer matches. Recovery = `local-scripts/rehash_geom_hashes.py`,
which recomputes them from `place.geom` in one audited transaction.

> ⚠️ **Guard:** since migration `0004` the geoid is *derived from* `geom_hash`, so the recipe is
> **identity-load-bearing and frozen**. Recomputing `geom_hash` on a DB that already holds real geoids
> would **re-mint every identity**. The script refuses on a deterministic-geoid DB unless forced; the
> only safe time to run it is to repair drift detected **before any data is loaded** on a new stack.
> A genuine recipe/GEOS change is an *identity-version event*, not a rehash — do not load data on a
> failing parity gate.

**2. DB teardown wipe** (reset data while keeping schema + the seeded `public` collection). The
`place`/`geoid_registry`/`change_log` triggers block ordinary `DELETE`/`TRUNCATE`. On managed
Postgres (Cloud SQL) **no login role is a true superuser**, so `SET session_replication_role` is
refused — instead disable the guard triggers by **table ownership**, connected as the role that
owns the tables (the app role, e.g. `geoid`):

```sql
ALTER TABLE place          DISABLE TRIGGER USER;
ALTER TABLE geoid_registry DISABLE TRIGGER USER;
ALTER TABLE change_log     DISABLE TRIGGER USER;
TRUNCATE place, geoid_registry, change_log;   -- no CASCADE: the two hinges carry no inbound FKs
ALTER TABLE place          ENABLE TRIGGER USER;
ALTER TABLE geoid_registry ENABLE TRIGGER USER;
ALTER TABLE change_log     ENABLE TRIGGER USER;  -- keeps catalog/collection seed + schema
```

## Licensing

- **Code:** Apache-2.0 (`LICENSE`)
- **Base data:** ODbL (`DATA-LICENSE`) — share-alike keeps the federation open

## Status

Phase 1 / Release 1. Authenticated RBAC is a Release-1 **stretch goal**, gated on
FAO's unified authentication service — until then a temporary static admin token gates
the management surface. See the implementation plan for the full roadmap (1.2 authenticated RBAC, 1.3
bulk, 2 open-source release, 3 standalone country instances + federation).
