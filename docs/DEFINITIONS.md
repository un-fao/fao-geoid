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
  from the shape. Two records of the same shape can have two geoids (see
  *deduplication* below), and one place keeps its geoid even though its geometry is
  fixed at mint time.

### external_id

An **optional caller-supplied reference** that lets a contributor tie a place back
to their own system (e.g. a national land-parcel number, an EUDR plot reference).
GeoID stores it but does not interpret it. It is a convenience handle, not the
identity — the geoid is the identity.

### collection

A **named bucket of places** within a workspace (the OGC API Features
"collection"). It is the unit that scoping rules apply to: deduplication and
`external_id` uniqueness are evaluated *within a single collection*, never across
collections. One reserved collection, `public`, accepts anonymous contributions.

### workspace

A **top-level grouping of collections** — the registry's outermost organizational
container. Release 1 runs with a single default workspace; the concept exists so
that multiple tenants or programmes can be separated later without reshaping the
data model.

How they nest:

```
workspace ─< collection ─< place (each place carries one geoid)
```

> **Terminology note.** The team's final API terminology ruling — **workspace /
> collection / item** — is the shipped default: the API surfaces each place as an
> "item" (the OGC API Features term). Internally and in this document "place"
> names the same concept; the surface vocabulary is a configuration choice
> (`GEOID_VOCAB`), not a data-model difference.

---

## The three uniqueness rules

These are the guarantees Release 1 enforces. The first two are hard, enforced
constraints; the third is a best-effort convenience that the team has chosen to
de-prioritize.

### 1. geoid — globally unique across the whole registry

Every geoid is unique across the entire registry/workspace, not merely within one
collection. No two places anywhere share a geoid. (Internally this is backed by a
dedicated registry of every issued geoid, so the guarantee holds even as the
system is sharded for scale and across federated instances later.)

### 2. external_id — unique within a collection, enforced

Within one collection, an `external_id` may be used at most once. Reusing the same
`external_id` for a second place in the same collection is rejected. The *same*
`external_id` value may legitimately appear in *different* collections — the rule
is scoped to the collection, by design. This is a hard, enforced rule.

### 3. geometry deduplication — identical-only, per collection, de-prioritized

When a place is contributed with a geometry that is **identical** to one already in
the same collection, GeoID returns the existing geoid instead of minting a new one
(deduplication). This is scoped per collection: the same shape in two different
collections produces two geoids.

Important scope of this rule, as the team agreed:

- It blocks **identical** geometries only. It is **not** a "near-duplicate" or
  "overlapping plot" detector, and it does not attempt fuzzy/spatial matching.
- The mechanism is **geohash-style**: the geometry is canonicalized (validity,
  ring/part/hole order, coordinate precision) and hashed; an equal hash means a
  duplicate.
- It is **error-prone by nature** (see the precision caveat below) and is
  therefore **de-prioritized** for Release 1. The mechanism is kept and working,
  but it is not something stakeholders should rely on for data-quality guarantees.

---

## Coordinate precision (the dedup grid)

Before hashing, every coordinate is snapped to a fixed grid so that
floating-point jitter doesn't make two copies of the same shape look different.
The grid size is the team's "coordinate precision" knob.

- **Configurable.** Set globally via `GEOID_DEDUP_GRID_DEFAULT`, and overridable
  per collection (`collection.metadata.dedup_grid`).
- **Default ≈ 1 centimetre** — `1e-7°` per vertex. (At the equator, `1e-7°` of
  longitude is roughly 1 cm; it varies with latitude.) This is the team's final
  ruling: deduplication means **exact match** — the grid exists only to absorb
  floating-point jitter, *not* to merge shapes that are merely near each other.
  Every collection is stamped with this default at creation unless an explicit
  value is supplied.
- **Frozen once a collection has data.** Changing the grid after places exist
  would re-hash existing geometries and could silently mint a second geoid for an
  already-registered place, so it is locked once a collection is non-empty.

### The honest caveat (why dedup is de-prioritized)

The grid is an **absolute grid**, not a "merge anything within 1 cm" radius. It
quantizes coordinates into ~1 cm cells; it does **not** measure the distance
between two points. The practical consequence:

> Two points that are less than a centimetre apart but happen to fall on opposite
> sides of a cell boundary will land in **different** cells and therefore produce
> **different** geoids — they will *not* be deduplicated.

So the rule neutralizes float jitter and exact re-submissions; it does **not**
guarantee that "visually the same" plots collapse to one geoid — and at ~1 cm
that is the *intended* behaviour, per the team's exact-match ruling. The boundary
behaviour is exactly the error-proneness the team flagged, and the reason
geometry deduplication is de-prioritized for Release 1.

---

## Authentication (Release-1 stretch goal)

Authentication is a **Release-1 stretch goal**, not a committed deliverable, and it
depends on **FAO's unified authentication service** (Eduardo's team), which is
expected to be OIDC — to be confirmed before it is wired in.

What exists today:

- A **temporary static admin token** (`GEOID_ADMIN_TOKEN`) gates the management
  endpoints (workspace/collection creation and listing). This is the team's
  agreed temporary stopgap and is a real, working deliverable.
- Anonymous contributions are allowed into the reserved `public` collection. The
  identity/dedup/`external_id` rules are identical for anonymous and admin
  callers — anonymity changes *who may write where*, not how a place is processed.
- The code has a single, currently inert **seam** where the unified auth service
  will plug in, so adopting it later does not change any endpoint.

Until that service is available, the static token is authoritative.
