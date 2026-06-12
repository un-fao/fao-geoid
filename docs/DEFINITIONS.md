# GeoID — Definitions & Rules (Release 1)

A plain-language reference for the GeoID terms and rules the team settled for
Release 1. It is meant to be shared with stakeholders, not just developers — it
describes *what the system guarantees*, not how the code is written.

---

## Core terms

### geoid

The **identifier GeoID mints for a geospatial place** — one globally unique,
immutable UUID (UUIDv7) per place. It is the product's output. On read it is also
surfaced as a Decentralized Identifier (`did:web:…`) and as a resolvable URI, but
those are derived views of the *same* underlying geoid.

- **Immutable.** A geoid never changes and is never reused or deleted. A
  correction does not edit a place; it mints a *new* geoid that points back to the
  one it supersedes, and the original still resolves forever.
- **Identity, not geometry.** The geoid identifies a place; it is *not* computed
  from the shape — but the registry enforces a one-to-one mapping: an identical
  geometry is registered **once**, catalog-wide (see *deduplication* below), and
  one place keeps its geoid even though its geometry is fixed at mint time.

### external_id

An **optional caller-supplied reference** that lets a contributor tie a place back
to their own system (e.g. a national land-parcel number, an EUDR plot reference).
GeoID stores it but does not interpret it. It is a convenience handle, not the
identity — the geoid is the identity.

### collection

A **named bucket of places** within a catalog (the OGC API Features
"collection"). `external_id` uniqueness is evaluated *within a single
collection*, never across collections; geometry deduplication, by contrast, is
**global** — it applies across the whole catalog regardless of collection. One
reserved collection, `public`, accepts anonymous contributions.

### catalog

A **top-level grouping of collections** — the registry's outermost organizational
container (the OGC/STAC "catalog"). Release 1 runs with a single default catalog;
the concept exists so that multiple tenants or programmes can be separated later
without reshaping the data model.

How they nest:

```
catalog ─< collection ─< place (each place carries one geoid)
```

> **Terminology note.** The shipped default vocabulary is the OGC/STAC-aligned
> **catalog / collection / item** (`GEOID_VOCAB=stac`): the top-level container
> is a "catalog" (the STAC term — also the internal table name) and each place
> is surfaced as an "item" (the OGC API Features term; the table is `place`).
> The surface vocabulary is a configuration choice (`GEOID_VOCAB`, with `fao` =
> workspace/collection/item still available as a label-only alias), not a
> data-model difference.

---

## The three uniqueness rules

These are the guarantees Release 1 enforces. All three are hard, enforced
constraints.

### 1. geoid — globally unique across the whole registry

Every geoid is unique across the entire registry/catalog, not merely within one
collection. No two places anywhere share a geoid. (Internally this is backed by a
dedicated registry of every issued geoid, so the guarantee holds even as the
system is sharded for scale and across federated instances later.)

### 2. external_id — unique within a collection, enforced

Within one collection, an `external_id` may be used at most once. Reusing the same
`external_id` for a second place in the same collection is rejected. The *same*
`external_id` value may legitimately appear in *different* collections — the rule
is scoped to the collection, by design. This is a hard, enforced rule.

### 3. geometry uniqueness — identical-only, global, enforced

When a place is contributed with a geometry that is **identical** to one already
registered **anywhere in the catalog**, the submission **fails with an error
(HTTP 409)** that carries the **existing geoid** — the insert is rejected, no
second geoid is minted. This is the team's final ruling (June 2026): one
geometry maps to exactly one geoid across the whole catalog, regardless of
which collection either submission targeted.

Important scope of this rule, as the team agreed:

- It blocks **identical** geometries only. It is **not** a "near-duplicate" or
  "overlapping plot" detector, and it does not attempt fuzzy/spatial matching.
- The mechanism is **geohash-style**: the geometry is canonicalized (validity,
  ring/part/hole order, coordinate precision) and hashed; an equal hash means a
  duplicate, enforced by a database unique constraint on the hash.
- The error response names the incumbent: clients receive the existing geoid
  (plus its resolvable did/uri forms and collection), so "already registered"
  is actionable, not a dead end.
- Near-identical shapes that differ by more than float jitter mint distinct
  geoids by design (see the precision caveat below).

---

## Coordinate precision (the dedup grid)

Before hashing, every coordinate is snapped to a fixed grid so that
floating-point jitter doesn't make two copies of the same shape look different.
The grid size is the team's "coordinate precision" knob.

- **One global grid.** Geometry uniqueness is catalog-wide, so there is exactly
  one grid for the whole instance — there is no per-collection override. The
  value is pinned in the database (a migration event to change), with
  `GEOID_DEDUP_GRID_DEFAULT` mirroring it for the application's conflict lookup.
- **≈ 1 centimetre** — `1e-7°` per vertex. (At the equator, `1e-7°` of
  longitude is roughly 1 cm; it varies with latitude.) This is the team's final
  ruling: deduplication means **exact match** — the grid exists only to absorb
  floating-point jitter, *not* to merge shapes that are merely near each other.
- **Retuning is a migration event.** Changing the grid re-hashes every stored
  geometry and can surface previously-distinct shapes as duplicates, so it is
  done in a schema migration with an audited re-hash, never as a config flip.

The hashing recipe itself is **versioned** (currently `v1`), and every instance
records which geometry-engine stack (PostGIS/GEOS versions) its stored hashes were
computed under. Because part of the recipe runs inside the database's geometry
engine, an engine upgrade can change hash output; this never affects geoids
themselves (a geoid, once minted, is permanent), only the dedup comparison. A
pinned set of reference geometries ("golden vectors") detects such drift before
any data is loaded, and an audited re-hash procedure recomputes the stored hashes
on the new stack — any newly discovered duplicates are reported for human review,
never silently merged or deleted.

### The honest caveat (what "identical" means at the boundary)

The grid is an **absolute grid**, not a "merge anything within 1 cm" radius. It
quantizes coordinates into ~1 cm cells; it does **not** measure the distance
between two points. The practical consequence:

> Two points that are less than a centimetre apart but happen to fall on opposite
> sides of a cell boundary will land in **different** cells and therefore mint
> **different** geoids — they will *not* be treated as duplicates.

So the rule rejects float-jittered and exact re-submissions; it does **not**
guarantee that "visually the same" plots collapse to one geoid — and at ~1 cm
that is the *intended* behaviour, per the team's exact-match ruling: the
boundary case is a *different* geometry, and a different geometry is a
different place. Stakeholders should not expect the rule to act as a
near-duplicate detector.

---

## Authentication (Release-1 stretch goal)

Authentication is a **Release-1 stretch goal**, not a committed deliverable, and it
depends on **FAO's unified authentication service** (Eduardo's team), which is
expected to be OIDC — to be confirmed before it is wired in.

What exists today:

- A **temporary static admin token** (`GEOID_ADMIN_TOKEN`) gates the management
  endpoints (catalog/collection creation and listing). This is the team's
  agreed temporary stopgap and is a real, working deliverable.
- Anonymous contributions are allowed into the reserved `public` collection. The
  identity/dedup/`external_id` rules are identical for anonymous and admin
  callers — anonymity changes *who may write where*, not how a place is processed.
- The code has a single, currently inert **seam** where the unified auth service
  will plug in, so adopting it later does not change any endpoint.

Until that service is available, the static token is authoritative.
