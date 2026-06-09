"""Unit tests for geoid identifier minting + did:web/URI derivation."""

from __future__ import annotations

import uuid

import pytest

from geoid.domain import identifiers

pytestmark = pytest.mark.unit


def test_new_geoid_is_version_7_and_rfc4122_variant():
    value = identifiers.new_geoid()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert identifiers.is_uuid7(value)


def test_embedded_timestamp_round_trips():
    ts_ms = 1_780_000_000_000
    value = identifiers.uuid7(ts_ms=ts_ms)
    assert identifiers.timestamp_ms_of(value) == ts_ms


def test_uuid7_is_time_ordered_across_increasing_timestamps():
    earlier = identifiers.uuid7(ts_ms=1_000_000_000_000)
    later = identifiers.uuid7(ts_ms=2_000_000_000_000)
    # k-sortable: the millisecond prefix makes string + byte order match time order.
    assert earlier.bytes < later.bytes
    assert str(earlier) < str(later)


def test_minting_is_unique_in_bulk():
    minted = {identifiers.new_geoid() for _ in range(2000)}
    assert len(minted) == 2000


def test_did_web_derivation():
    value = uuid.UUID("019e9976-974c-7d01-b2b6-299f41d9d29c")
    assert (
        identifiers.did_for(value, "data.fao.org")
        == "did:web:data.fao.org:geoid:019e9976-974c-7d01-b2b6-299f41d9d29c"
    )


def test_uri_derivation_strips_trailing_slash():
    value = uuid.UUID("019e9976-974c-7d01-b2b6-299f41d9d29c")
    assert (
        identifiers.uri_for(value, "https://data.fao.org/")
        == "https://data.fao.org/geoid/019e9976-974c-7d01-b2b6-299f41d9d29c"
    )


def test_item_url_is_collection_scoped():
    value = uuid.UUID("019e9976-974c-7d01-b2b6-299f41d9d29c")
    url = identifiers.item_url_for(value, "public", "https://data.fao.org")
    assert url == "https://data.fao.org/collections/public/items/" + str(value)


def test_derive_identifiers_bundle_includes_item_url_when_collection_given():
    value = identifiers.new_geoid()
    bundle = identifiers.derive_identifiers(
        value, base_url="https://data.fao.org", did_host="data.fao.org", collection="public"
    )
    assert bundle["geoid"] == str(value)
    assert bundle["did"].startswith("did:web:data.fao.org:geoid:")
    assert bundle["uri"].endswith(str(value))
    assert bundle["item_url"].endswith(f"/collections/public/items/{value}")


def test_derive_identifiers_omits_item_url_without_collection():
    value = identifiers.new_geoid()
    bundle = identifiers.derive_identifiers(
        value, base_url="https://data.fao.org", did_host="data.fao.org"
    )
    assert "item_url" not in bundle
