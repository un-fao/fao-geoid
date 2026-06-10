"""Unit tests for geoid identifier minting + did:web/URI derivation."""

from __future__ import annotations

import itertools
import threading
import uuid

import pytest

from geoid.domain import identifiers

pytestmark = pytest.mark.unit

_FROZEN_NS = 1_780_000_000_000_000_000  # an arbitrary fixed instant, in nanoseconds


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


def test_same_ms_mints_are_strictly_increasing(monkeypatch):
    # Arrange: freeze the clock AT the last-seen ms so every mint takes the same-ms branch
    monkeypatch.setattr(identifiers, "_last_ms", _FROZEN_NS // 1_000_000)
    monkeypatch.setattr(identifiers, "_counter", 0)
    monkeypatch.setattr(identifiers.time, "time_ns", lambda: _FROZEN_NS)

    # Act
    minted = [identifiers.uuid7() for _ in range(100)]

    # Assert
    for earlier, later in itertools.pairwise(minted):
        assert earlier.bytes < later.bytes
    assert all(v.version == 7 for v in minted)
    assert all(v.variant == uuid.RFC_4122 for v in minted)


def test_clock_rollback_holds_timestamp_and_stays_monotonic(monkeypatch):
    # Arrange: a clock that ticks backwards between mints (NTP adjustment)
    monkeypatch.setattr(identifiers, "_last_ms", 0)
    monkeypatch.setattr(identifiers, "_counter", 0)
    clock = {"ns": _FROZEN_NS}
    monkeypatch.setattr(identifiers.time, "time_ns", lambda: clock["ns"])

    # Act
    minted = [identifiers.uuid7()]
    for step_back_ms in (10, 20, 30):
        clock["ns"] = _FROZEN_NS - step_back_ms * 1_000_000
        minted.append(identifiers.uuid7())

    # Assert: byte order strictly increases, embedded timestamp never decreases
    for earlier, later in itertools.pairwise(minted):
        assert earlier.bytes < later.bytes
        assert identifiers.timestamp_ms_of(later) >= identifiers.timestamp_ms_of(earlier)


def test_counter_overflow_advances_synthetic_ms_without_duplicates(monkeypatch):
    # Arrange: frozen clock so >4096 mints must overflow the 12-bit counter
    monkeypatch.setattr(identifiers, "_last_ms", 0)
    monkeypatch.setattr(identifiers, "_counter", 0)
    monkeypatch.setattr(identifiers.time, "time_ns", lambda: _FROZEN_NS)

    # Act
    minted = [identifiers.uuid7() for _ in range(5000)]

    # Assert: unique, strictly increasing, and the synthetic ms actually advanced
    assert len(set(minted)) == 5000
    for earlier, later in itertools.pairwise(minted):
        assert earlier.bytes < later.bytes
    assert identifiers.timestamp_ms_of(minted[-1]) > _FROZEN_NS // 1_000_000


def test_concurrent_minting_yields_unique_ids():
    # Arrange
    results: list[uuid.UUID] = []
    results_lock = threading.Lock()

    def mint_many() -> None:
        local = [identifiers.uuid7() for _ in range(500)]
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=mint_many) for _ in range(8)]

    # Act
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Assert
    assert len(set(results)) == 8 * 500


def test_explicit_ts_ms_bypasses_monotonic_state(monkeypatch):
    # Arrange: pre-load recognizable monotonic state
    monkeypatch.setattr(identifiers, "_last_ms", 123)
    monkeypatch.setattr(identifiers, "_counter", 7)

    # Act
    value = identifiers.uuid7(ts_ms=1_780_000_000_000)

    # Assert: the override is honored and the shared state is untouched
    assert identifiers.timestamp_ms_of(value) == 1_780_000_000_000
    assert identifiers._last_ms == 123
    assert identifiers._counter == 7
