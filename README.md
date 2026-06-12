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
                                  ├─ GET by geoid / by (external_id, collection)
                                  ├─ OGC API Features read (landing, conformance, collections, items, queryables)
                                  └─ /docs (Swagger)
                                          │
                                          ▼
                          PostgreSQL 17 + PostGIS 3.5.2
                          catalog ─< collection ─< place  (+ geoid_registry, change_log)
```

## Documentation

📖 **Full interactive technical walkthrough:** open [`docs/architecture.html`](docs/architecture.html)
in a browser — system architecture, ER + sequence + dedup diagrams, the write/read paths, and a
per-module reference for all 47 modules.

🎓 **Geospatial concepts & standards tutorial:** open [`docs/tutorial.html`](docs/tutorial.html)
in a browser — an interactive primer on every geospatial idea GeoID relies on (CRS & axis order,
GeoJSON↔WKB, polygon validity, the dedup hash, spatial indexing, the antimeridian, OGC API Features
& CQL2, UUIDv7), with live widgets and per-section self-tests grounded in the real code.

Prose: this file + [`docs/DEFINITIONS.md`](docs/DEFINITIONS.md).

## The identifier

Every place is minted a **UUIDv7** (RFC 9562), stored bare as the item id. On read we derive:

- a DID — `did:web:data.fao.org:geoid:<uuid>`
- a URI — `https://data.fao.org/geoid/<uuid>`
- a collection-scoped OGC item URL — `…/collections/{coll}/items/<uuid>`

`POST` returns all three. The geoid is **immutable**: `place` is INSERT-only (enforced by a DB trigger);
corrections mint a *new* geoid linked via `predecessor_id`, and the original resolves forever.

Deduplication is a **separate** concern from identity: a **global** canonical `geom_hash` (see below),
never the id. One geometry → one geoid across the whole catalog — POSTing an identical geometry fails
with **409** and the body carries the **incumbent geoid** (plus its did/uri and collection).

## The dedup recipe (load-bearing correctness)

Computed in a `BEFORE INSERT` trigger so the API, `COPY`, and `ogr2ogr` paths can never drift:

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

A single SQL function `geoid_geom_hash(geom, grid)` is the *one* definition, used by both the insert trigger
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
mint → dedup → validation → OGC read → bulk export in one command:

```bash
uv run python scripts/seed_samples.py
```

Expected: 5 plots minted, a reversed-winding duplicate rejected with 409 + the incumbent geoid, a
self-intersecting polygon rejected (422), bbox/CQL2 queries, and a 5-feature bulk export.
See `samples/README.md` for details.

## Tests

```bash
uv run pytest -m unit                  # pure unit (no Docker)
uv run pytest -m integration           # ephemeral PostGIS via testcontainers (needs Docker)
uv run ruff check .
```

## Configuration

All configuration is environment-driven (see `.env.example`). The core image imports **no GCP SDK**:
object storage is behind a `BlobStore` Protocol (`LocalFSStore` default, `GCSStore` via the `gcs` extra),
so the *same image* is the on-prem artifact via `docker compose`.

## Licensing

- **Code:** Apache-2.0 (`LICENSE`)
- **Base data:** ODbL (`DATA-LICENSE`) — share-alike keeps the federation open

## Status

Phase 1 / Release 1. Authenticated RBAC is a Release-1 **stretch goal**, gated on
FAO's unified authentication service — until then a temporary static admin token gates
the management surface. See the implementation plan for the full roadmap (1.2 authenticated RBAC, 1.3
bulk, 2 open-source release, 3 standalone country instances + federation).
