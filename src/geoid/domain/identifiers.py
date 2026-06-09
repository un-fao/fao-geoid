"""The geoid identifier: a UUIDv7 minted app-side, plus the read-time derivations.

The *one unrecoverable decision* in the system is that every geoid is an immutable
UUIDv7 (RFC 9562). Postgres 16 has no native ``uuidv7()`` (that ships in PG18), and
a standalone country instance must be able to mint offline, so we generate it here
in the registry service rather than in the database.

On read we derive three resolvable forms from the bare UUID:

* a DID            ``did:web:<host>:geoid:<uuid>``
* a URI            ``<base_url>/geoid/<uuid>``
* an OGC item URL  ``<base_url>/collections/<collection>/items/<uuid>``

``did:web`` is a W3C-CCG community method (not a formal W3C standard); the full
custom ``did:geoid`` method is deferred and additive — the UUID never changes.
"""

from __future__ import annotations

import os
import time
import uuid

_UUID_V7_VERSION = 0x70  # version 7 in the high nibble of byte 6
_VARIANT_RFC4122 = 0x80  # variant 10xx in the high bits of byte 8


def uuid7(ts_ms: int | None = None) -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562 §5.7).

    Layout (128 bits): 48-bit big-endian Unix millisecond timestamp, 4-bit
    version (0b0111), 12-bit random ``rand_a``, 2-bit variant (0b10), 62-bit
    random ``rand_b``. The millisecond timestamp prefix makes ids k-sortable,
    which keeps the primary-key b-tree append-friendly.

    Args:
        ts_ms: Override the timestamp in Unix milliseconds (tests only).
    """
    if ts_ms is None:
        ts_ms = time.time_ns() // 1_000_000
    ts = ts_ms & ((1 << 48) - 1)

    rand = os.urandom(10)  # 80 random bits; we consume 12 + 62 = 74 of them
    b = bytearray(16)

    # 48-bit timestamp, big-endian, bytes 0..5
    b[0] = (ts >> 40) & 0xFF
    b[1] = (ts >> 32) & 0xFF
    b[2] = (ts >> 24) & 0xFF
    b[3] = (ts >> 16) & 0xFF
    b[4] = (ts >> 8) & 0xFF
    b[5] = ts & 0xFF

    # version (high nibble of byte 6) + 12-bit rand_a (low nibble of 6 + byte 7)
    rand_a = int.from_bytes(rand[0:2], "big") & 0x0FFF
    b[6] = _UUID_V7_VERSION | (rand_a >> 8)
    b[7] = rand_a & 0xFF

    # variant (high bits of byte 8) + 62-bit rand_b (rest of bytes 8..15)
    rand_b = int.from_bytes(rand[2:10], "big") & ((1 << 62) - 1)
    b[8] = _VARIANT_RFC4122 | ((rand_b >> 56) & 0x3F)
    b[9] = (rand_b >> 48) & 0xFF
    b[10] = (rand_b >> 40) & 0xFF
    b[11] = (rand_b >> 32) & 0xFF
    b[12] = (rand_b >> 24) & 0xFF
    b[13] = (rand_b >> 16) & 0xFF
    b[14] = (rand_b >> 8) & 0xFF
    b[15] = rand_b & 0xFF

    return uuid.UUID(bytes=bytes(b))


def new_geoid() -> uuid.UUID:
    """Mint a fresh geoid (a UUIDv7). The bare UUID is the canonical identifier."""
    return uuid7()


def timestamp_ms_of(value: uuid.UUID) -> int:
    """Extract the embedded Unix-millisecond timestamp from a UUIDv7."""
    return int.from_bytes(value.bytes[0:6], "big")


def is_uuid7(value: uuid.UUID) -> bool:
    """True iff ``value`` is a version-7 UUID."""
    return value.version == 7


def did_for(value: uuid.UUID, host: str) -> str:
    """Derive the ``did:web`` form. ``host`` is the did:web authority (e.g. ``data.fao.org``)."""
    return f"did:web:{host}:geoid:{value}"


def uri_for(value: uuid.UUID, base_url: str) -> str:
    """Derive the durable resolver URI ``<base_url>/geoid/<uuid>``."""
    return f"{base_url.rstrip('/')}/geoid/{value}"


def item_url_for(value: uuid.UUID, collection: str, base_url: str) -> str:
    """Derive the collection-scoped OGC API Features item URL."""
    return f"{base_url.rstrip('/')}/collections/{collection}/items/{value}"


def derive_identifiers(
    value: uuid.UUID,
    *,
    base_url: str,
    did_host: str,
    collection: str | None = None,
) -> dict[str, str]:
    """Bundle the three (or four) resolvable forms returned by POST / GET.

    Returns ``geoid``, ``did``, ``uri`` and, when ``collection`` is given, the
    collection-scoped ``item_url``.
    """
    out = {
        "geoid": str(value),
        "did": did_for(value, did_host),
        "uri": uri_for(value, base_url),
    }
    if collection is not None:
        out["item_url"] = item_url_for(value, collection, base_url)
    return out
