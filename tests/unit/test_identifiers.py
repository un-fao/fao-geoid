"""Unit tests for geoid derivation + URI derivation."""

from __future__ import annotations

import uuid

import pytest

from geoid.domain import identifiers

pytestmark = pytest.mark.unit


def test_uri_derivation_strips_trailing_slash():
    value = uuid.UUID("019e9976-974c-7d01-b2b6-299f41d9d29c")
    assert (
        identifiers.uri_for(value, "https://data.fao.org/")
        == "https://data.fao.org/019e9976-974c-7d01-b2b6-299f41d9d29c"
    )


def test_derive_identifiers_bundles_geoid_and_resolver_uri():
    value = uuid.UUID("169dc6c3-af8c-80d4-a0b3-436d1bbbde86")
    bundle = identifiers.derive_identifiers(value, base_url="https://data.fao.org")
    assert bundle == {"geoid": str(value), "uri": f"https://data.fao.org/{value}"}


# --- Deterministic geoid: UUIDv8 derived from the geometry's SHA-256 geom_hash ----
# geoid_from_geom_hash MUST stay byte-identical to the SQL function of the same name
# (migration 0004) — the Python and DB derivations are the same identity.

# SHA-256 of the canonical baseline_unit_square under recipe v2 (the golden-vector
# corpus, scripts/data/dedup_golden_vectors_v2.json).
_BASELINE_UNIT_SQUARE_SHA256 = "169dc6c3af8c30d4a0b3436d1bbbde865626ff1d7e48195b14fcf563dc4663c1"


def test_geoid_from_geom_hash_is_version_8_and_rfc4122_variant():
    geoid = identifiers.geoid_from_geom_hash(bytes.fromhex(_BASELINE_UNIT_SQUARE_SHA256))
    assert geoid.version == 8
    assert geoid.variant == uuid.RFC_4122


def test_geoid_from_geom_hash_is_deterministic():
    digest = bytes.fromhex(_BASELINE_UNIT_SQUARE_SHA256)
    assert identifiers.geoid_from_geom_hash(digest) == identifiers.geoid_from_geom_hash(digest)


def test_geoid_from_geom_hash_known_answer():
    # Pins the exact byte layout (first 16 bytes of the SHA-256, version=8 + variant
    # stamped) so any change to the stamping surfaces as identity drift.
    geoid = identifiers.geoid_from_geom_hash(bytes.fromhex(_BASELINE_UNIT_SQUARE_SHA256))
    assert str(geoid) == "169dc6c3-af8c-80d4-a0b3-436d1bbbde86"


def test_geoid_from_geom_hash_distinct_digests_give_distinct_geoids():
    a = identifiers.geoid_from_geom_hash(bytes.fromhex("00" * 32))
    b = identifiers.geoid_from_geom_hash(bytes.fromhex("ff" * 32))
    assert a != b


def test_geoid_from_geom_hash_uses_only_first_16_bytes():
    # The geoid is a 128-bit truncation: bytes past the 16th never affect it.
    prefix = "11" * 16
    geoid_a = identifiers.geoid_from_geom_hash(bytes.fromhex(prefix + "00" * 16))
    geoid_b = identifiers.geoid_from_geom_hash(bytes.fromhex(prefix + "ff" * 16))
    assert geoid_a == geoid_b


def test_geoid_from_geom_hash_rejects_short_input():
    with pytest.raises(ValueError):
        identifiers.geoid_from_geom_hash(b"\x00" * 15)
