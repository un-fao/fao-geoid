# Changelog

All notable changes to GeoID are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
