"""The geoid identifier: a deterministic, content-addressed UUIDv8 derived from the
geometry, plus the read-time derivations.

The *one unrecoverable decision* in the system is that every geoid is an immutable,
**content-addressed** UUIDv8 (RFC 9562 §5.8): the first 16 bytes of the geometry's
canonical SHA-256 ``geom_hash`` with the version (8) and variant bits stamped
(:func:`geoid_from_geom_hash`). Identity is therefore a pure function of the canonical
geometry — the same geometry yields the same geoid on every deployment (federation
without coordination), and re-creating a deleted geometry recovers its geoid. The
authoritative value is computed DB-side in the arbiter CTE (migration 0004's SQL
``geoid_from_geom_hash``) from the very ``geom_hash`` used for dedup, so identity and
dedup can never drift; this Python mirror must stay byte-identical for tests/tooling.
RFC 9562 §6.5 mandates UUIDv8 (not the SHA-1 v5) for SHA-256-based name UUIDs.

On read we derive the resolvable form from the bare UUID:

* a URI            ``<base_url>/<uuid>``
"""

from __future__ import annotations

import uuid

_UUID_V8_VERSION = 0x80  # version 8 in the high nibble of byte 6
_VARIANT_RFC4122 = 0x80  # variant 10xx in the high bits of byte 8


def geoid_from_geom_hash(geom_hash: bytes) -> uuid.UUID:
    """Derive the deterministic geoid (UUIDv8) from a geometry's SHA-256 ``geom_hash``.

    Takes the first 16 bytes of the digest and stamps the version (8) and RFC 4122
    variant bits in place, so the geoid is a pure function of the canonical geometry:
    the same geometry → the same geoid on every deployment, and a re-created geometry
    recovers its geoid. Byte-identical to the SQL ``geoid_from_geom_hash`` (migration
    0004) — the database computes the authoritative value; this mirror serves tests
    and tooling.

    Args:
        geom_hash: the canonical geometry digest (a 32-byte SHA-256; only the first
            16 bytes are consumed). Inputs shorter than 16 bytes are rejected.
    """
    if len(geom_hash) < 16:
        raise ValueError(f"geom_hash must be at least 16 bytes, got {len(geom_hash)}")
    raw = bytearray(geom_hash[:16])
    raw[6] = (raw[6] & 0x0F) | _UUID_V8_VERSION
    raw[8] = (raw[8] & 0x3F) | _VARIANT_RFC4122
    return uuid.UUID(bytes=bytes(raw))


def uri_for(value: uuid.UUID, base_url: str) -> str:
    """Derive the durable resolver URI ``<base_url>/<uuid>``."""
    return f"{base_url.rstrip('/')}/{value}"


def derive_identifiers(value: uuid.UUID, *, base_url: str) -> dict[str, str]:
    """Bundle the resolvable forms returned by POST / GET: ``geoid`` and ``uri``."""
    return {"geoid": str(value), "uri": uri_for(value, base_url)}
