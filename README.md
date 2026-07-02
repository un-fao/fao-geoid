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

📖 **Full interactive technical walkthrough:** open [`local-scripts/docs/architecture.html`](local-scripts/docs/architecture.html)
in a browser — system architecture, ER + sequence + dedup diagrams, the write/read paths, and a
per-module reference for all 47 modules.

🎓 **Geospatial concepts & standards tutorial:** open [`local-scripts/docs/tutorial.html`](local-scripts/docs/tutorial.html)
in a browser — an interactive primer on every geospatial idea GeoID relies on (CRS & axis order,
GeoJSON↔WKB, polygon validity, the dedup hash, spatial indexing, OGC API Features,
the content-addressed UUIDv8 geoid), with live widgets and per-section self-tests grounded in the real code.

📋 **Stakeholder contract (shareable):** open [`local-scripts/docs/contract.html`](local-scripts/docs/contract.html) in a
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

## The identity recipe (load-bearing correctness)

Computed inside the single arbiter-CTE insert (`place_repo.insert_place`) that both the single and bulk
write paths share, via the one `geoid_geom_hash_default` SQL wrapper, so the hash can never drift
between paths (and, post-migration 0004, the geoid itself is derived from it). Since migration 0008
(**recipe v2**, [ADR-007](docs/adr/ADR-007-identity-recipe-v2.md)) the recipe is an integer-lattice
canonicalization GeoID fully owns — **engine-independent**, zero GEOS/PostGIS calls in the identity
bytes:

```
q            = round_half_even(coord × 10⁷)         one IEEE-754 multiply → int64 lattice index
canonical    = 0x02 ‖ type_tag ‖ counts + int64 pairs (big-endian), with rings CCW,
               min-vertex-rotated, coincident runs collapsed, holes/parts byte-sorted
geom_hash    = sha256(canonical)                     hash int64 indices, never floats
```

- **SHA-256** — a collision would assign the wrong geoid to a *different* place.
- **The 1e-7° lattice (~1 cm/vertex)** — coordinates that quantize to the same cell collapse to one
  hash (`GEOID_DEDUP_GRID_DEFAULT` documents the cell size; the SCALE literal is pinned in migration
  0008 and mirrored by `domain/geometry_identity.py`; retunes are identity-version migration events).
  Note: this snaps to a fixed lattice, so two points a hair apart but straddling a cell boundary can
  still land in *different* cells — it neutralizes float jitter, not all sub-cm differences.
- **Structure canonicalized by frozen spec constants** (not GEOS): ring rotation/winding, hole and
  multipart order, multipoint order — the same geometry serialized any way yields one hash.
- **Degeneracy is rejected, never merged**: a valid geometry that collapses on the lattice (a sliver
  thinner than a cell, a zero-area bowtie) answers **422** — an identity service must not silently
  merge or vanish distinct submissions.
- **Big-endian, collation-free serialization** — a country instance's hash matches central at
  federation sync, on any engine build.

The SQL functions (migration 0008) and the pure-Python reference (`geoid.domain.geometry_identity`)
are pinned byte-identical by the golden-vector corpus (`scripts/data/dedup_golden_vectors_v2.json`) on
every CI run and by the post-deploy canary (`scripts/dedup_vectors.py --check`).

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
in `local-scripts/` (git-ignored, not in the image); the full runbook is `local-scripts/docs/DEPLOYMENT.md §14`.

**1. geom_hash rehash / drift-recovery — HISTORICAL (v1-era).** Under recipe v1 the hash was
GEOS-bound, and `local-scripts/rehash_geom_hashes.py` existed to recompute stored hashes after an
engine upgrade. Under recipe v2 (migration 0008, ADR-007) the hash is **engine-independent**, so
engine-drift rehashing has no trigger anymore — and the script's own guard fails fast post-0004/0008
(the geoid is *derived from* `geom_hash`; recomputing would re-mint every identity). A failing
golden-vector check now means the deployed SQL diverged from the Python reference (a code bug or an
unapplied migration), never an engine artifact — do not load data on a failing check. A deliberate
recipe change is an **identity-version event** with its own migration and the re-mint runbook
(`local-scripts/docs/DEPLOYMENT.md §15`), never a rehash.

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

**Authenticated access is live**: hybrid Keycloak OIDC + a static admin token, with per-collection
owner/editor/viewer grants — see [`local-scripts/docs/auth.html`](local-scripts/docs/auth.html).
**Synchronous bulk write is live**: `POST /collections/{id}/items/bulk`. Planned next: an
open-source release, and standalone country instances with federation.
