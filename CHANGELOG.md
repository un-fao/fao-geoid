# Changelog

All notable changes to GeoID are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed — BREAKING
- **Identity recipe v2 (migration 0008, ADR-007): every geoid re-mints.** The
  geometry fingerprint the geoid is derived from is no longer computed by the
  database's GEOS library (whose builds provably disagree with each other); it is
  an engine-independent integer-lattice canonicalization owned by GeoID — the
  same geometry now yields the same geoid on *any* engine, build, or deployment.
  Geoids minted under the previous recipe are void (the registries were reset per
  the re-mint runbook). Dedup semantics are unchanged (same ~1cm exact-match
  precision, same 409-with-incumbent contract).
- **New:** a geometry that *degenerates at the identity precision* — e.g. a
  polygon sliver thinner than ~1cm, or a zero-area bowtie — is now rejected with
  `422 "geometry degenerates at the identity precision (1e-7°)"` (one
  `schema_invalid` reject on the bulk route) instead of being silently merged or
  dropped by the old repair step.
- The golden-vector corpus is regenerated as v2 and is now verifiable without a
  database; the post-deploy canary asserts the deployed SQL recipe is
  byte-identical to the pure-Python reference.

### Added
- **Hybrid authentication** — Keycloak OIDC (RS256 JWT) validated alongside the
  static admin token, with no flag day: the static token keeps working unchanged.
  OIDC is enabled at the review deployment; disabled by default elsewhere.
- **Per-collection RBAC grants** — `owner`/`editor` roles keyed by verified email
  (`viewer` reserved, not yet enforced), managed via
  `POST`/`GET`/`DELETE /collections/{id}/grants[/{email}]` (owner or sysadmin).
- Swagger single-sign-on button (Authorization Code + PKCE), gated behind
  `GEOID_SWAGGER_OAUTH2_ENABLED` (off by default).
- Post-deploy golden-vector canary: `dedup_vectors.py --check` runs as a Cloud Run
  job against the live database after every migration, failing the deploy on
  identity-recipe drift before the service ships.
- Last-owner guard: revoking or demoting a collection's only `owner` grant is
  blocked with 409 (grant another owner first).
- Database hardening (migration 0007): normalized-grant-email CHECK constraint;
  identity/dedup SQL functions marked `PARALLEL SAFE` with a pinned `search_path`
  (bodies unchanged — identity untouched); dead paging index dropped. (2D-only
  needs no new constraint: the geometry column type already rejects Z/M.)

### Changed
- A grant whose recorded Keycloak subject differs from the caller's is no longer
  honored (403 + server-side warning) — email reuse cannot silently inherit access.
- Unrecognized database integrity errors now answer `500 internal error` (logged
  loudly server-side) instead of a mislabeled `409`.
- Bulk ingest documents its atomicity contract: the batch is one transaction — a
  timeout/disconnect before the response discards all rows, including accepted ones.

### Fixed
- Non-ASCII bearer tokens now answer 401 instead of crashing with a 500.
- The dedup 409 now always names the *stored* incumbent geoid (self-correcting
  against any legacy pre-deterministic row).
- Database/programming failures during a write are no longer masked as
  `422 unparseable GeoJSON`; they surface loudly with server-side logging.
- Application logs (OIDC rejections, JWKS outages, bootstrap) are actually emitted
  in the deployed image; JWKS infrastructure failures log at ERROR.
- Concurrent cold-start bootstrap no longer crashes on the public-collection
  create race.

## [0.3.0] - 2026-06-24

### Added
- Accept Point and MultiPoint geometries (in addition to Polygon/MultiPolygon);
  lines and GeometryCollection remain unsupported. Identity reuses the frozen v1
  recipe at the same 1e-7 tolerance, so a point dedups exactly as a polygon vertex
  does (409 + incumbent on a match). See ADR-005.

### Changed
- GeoID is now explicitly 2D-only: a geometry carrying a Z (elevation) coordinate
  is rejected with a 422 instead of being silently accepted. See ADR-006.

## [0.2.1] - 2026-06-23

### Changed
- Scrubbed internal planning and team references from API descriptions, code
  comments, and OpenAPI docstrings. Documentation only — no behavioral change.

## [0.2.0] - 2026-06-23

### Removed
- **BREAKING — item listing/filtering/search endpoints removed.** Querying
  items by attribute or bbox is no longer supported via the API.
- **BREAKING — collection reads now require an admin principal.** Listing and
  reading collections is gated behind admin.
- OGC endpoints are hidden from the OpenAPI/Swagger schema.

## [0.1.2] - 2026-06-23

### Changed
- Bulk ingest cap raised to 2,500 features per request (deployed with 1 GiB
  memory and a 600 s request timeout).

### Fixed
- `/health` and OpenAPI now report the real package version (derived from
  package metadata) instead of a hardcoded string.

## [0.1.1] - 2026-06-23

### Security
- Patched `pydantic-settings` to 2.14.2 (GHSA-4xgf-cpjx-pc3j).

## [0.1.0] - 2026-06-22

Initial production release.

### Added
- **Deterministic, content-addressed GeoID** — each ID is a UUIDv8 derived from
  the geometry itself.
- **Global geometry uniqueness** — ingesting a duplicate geometry returns `409`
  with the incumbent geoid.
- **Synchronous multi-geometry bulk POST** for batch ingest.
- **GeoJSON + WKT** geometry output via `?format=`.
- **Durable resolver** served at the app root (`/{geoid}`).
- Collection management API.
- `/health` database-connectivity probe.
- Versioned dedup recipe with pinned golden vectors and a documented re-hash
  procedure.
- Proxy sub-path mount support (`GEOID_ROOT_PATH`).

### Security
- OGC callback hardened against SSRF to internal hosts.
- Patched `starlette` / `cryptography` / `aiohttp`.
