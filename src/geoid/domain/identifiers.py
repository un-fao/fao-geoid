"""The geoid identifier: a UUIDv7 minted app-side, plus the read-time derivations.

The *one unrecoverable decision* in the system is that every geoid is an immutable
UUIDv7 (RFC 9562). Postgres 17 has no native ``uuidv7()`` (that ships in PG18), and
a standalone country instance must be able to mint offline, so we generate it here
in the registry service rather than in the database.

On read we derive resolvable forms from the bare UUID:

* a URI            ``<base_url>/geoid/<uuid>``
* an OGC item URL  ``<base_url>/collections/<collection>/items/<uuid>``
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_UUID_V7_VERSION = 0x70  # version 7 in the high nibble of byte 6
_VARIANT_RFC4122 = 0x80  # variant 10xx in the high bits of byte 8

# Monotonic state (RFC 9562 §6.2 Method 1): the 12-bit rand_a field is a counter,
# randomly re-seeded on each new millisecond (random seed = cross-process spread, per
# the RFC; the trade-off is that per-ms capacity before overflow is 4096 minus the
# seed, not a fixed 4095). The lock guards the (ms, counter) snapshot; the per-mint
# rand_b urandom() runs outside it — only the once-per-ms 2-byte seed runs inside.
_COUNTER_MAX = 0xFFF  # 12 bits
_lock = threading.Lock()
_last_ms: int = 0
_counter: int = 0


def _assemble(ts_ms: int, rand_a: int, rand_b: int) -> uuid.UUID:
    """Pack the three UUIDv7 fields into the RFC 9562 §5.7 byte layout.

    Layout (128 bits): 48-bit big-endian Unix millisecond timestamp, 4-bit
    version (0b0111), 12-bit ``rand_a``, 2-bit variant (0b10), 62-bit ``rand_b``.
    The millisecond timestamp prefix makes ids k-sortable, which keeps the
    primary-key b-tree append-friendly. Inputs wider than their field are masked
    (``rand_a`` to 12 bits, ``rand_b`` to 62), so callers may pass raw urandom ints.
    """
    ts = ts_ms & ((1 << 48) - 1)
    rand_a &= 0x0FFF
    rand_b &= (1 << 62) - 1
    b = bytearray(16)

    # 48-bit timestamp, big-endian, bytes 0..5
    b[0] = (ts >> 40) & 0xFF
    b[1] = (ts >> 32) & 0xFF
    b[2] = (ts >> 24) & 0xFF
    b[3] = (ts >> 16) & 0xFF
    b[4] = (ts >> 8) & 0xFF
    b[5] = ts & 0xFF

    # version (high nibble of byte 6) + 12-bit rand_a (low nibble of 6 + byte 7)
    b[6] = _UUID_V7_VERSION | (rand_a >> 8)
    b[7] = rand_a & 0xFF

    # variant (high bits of byte 8) + 62-bit rand_b (rest of bytes 8..15)
    b[8] = _VARIANT_RFC4122 | ((rand_b >> 56) & 0x3F)
    b[9] = (rand_b >> 48) & 0xFF
    b[10] = (rand_b >> 40) & 0xFF
    b[11] = (rand_b >> 32) & 0xFF
    b[12] = (rand_b >> 24) & 0xFF
    b[13] = (rand_b >> 16) & 0xFF
    b[14] = (rand_b >> 8) & 0xFF
    b[15] = rand_b & 0xFF

    return uuid.UUID(bytes=bytes(b))


def uuid7(ts_ms: int | None = None) -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562 §5.7) with a monotonic counter (§6.2 Method 1).

    Without ``ts_ms``, ids are strictly monotonic within the process: rand_a is a
    counter incremented on same-ms mints, the timestamp is held at the last known
    ms on clock rollback (NTP, suspend/resume), and counter overflow (>4095/ms)
    advances a synthetic millisecond before re-seeding.

    Args:
        ts_ms: Override the timestamp in Unix milliseconds (tests only). Bypasses
            the monotonic state entirely and uses a random rand_a.
    """
    if ts_ms is not None:
        rand = os.urandom(10)  # 80 random bits; we consume 12 + 62 = 74 of them
        rand_a = int.from_bytes(rand[0:2], "big")
        rand_b = int.from_bytes(rand[2:10], "big")
        return _assemble(ts_ms, rand_a, rand_b)

    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            # New millisecond: advance the clock, seed the counter randomly.
            _last_ms = ms
            _counter = int.from_bytes(os.urandom(2), "big") & _COUNTER_MAX
        else:
            # Same ms OR clock rollback: hold the timestamp, increment the counter.
            _counter += 1
            if _counter > _COUNTER_MAX:
                # Overflow: advance a synthetic millisecond, re-seed the counter.
                _last_ms += 1
                _counter = int.from_bytes(os.urandom(2), "big") & _COUNTER_MAX
        ms_snapshot = _last_ms
        counter_snapshot = _counter

    rand_b = int.from_bytes(os.urandom(8), "big")
    return _assemble(ms_snapshot, counter_snapshot, rand_b)


def new_geoid() -> uuid.UUID:
    """Mint a fresh geoid (a UUIDv7). The bare UUID is the canonical identifier."""
    return uuid7()


def timestamp_ms_of(value: uuid.UUID) -> int:
    """Extract the embedded Unix-millisecond timestamp from a UUIDv7."""
    return int.from_bytes(value.bytes[0:6], "big")


def is_uuid7(value: uuid.UUID) -> bool:
    """True iff ``value`` is a version-7 UUID."""
    return value.version == 7


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
    collection: str | None = None,
) -> dict[str, str]:
    """Bundle the two (or three) resolvable forms returned by POST / GET.

    Returns ``geoid``, ``uri`` and, when ``collection`` is given, the
    collection-scoped ``item_url``.
    """
    out = {
        "geoid": str(value),
        "uri": uri_for(value, base_url),
    }
    if collection is not None:
        out["item_url"] = item_url_for(value, collection, base_url)
    return out
