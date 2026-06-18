"""Unit contracts on ogc_service: paging links honor the offset cap, and the
advertised queryables set is pinned to the live CQL2 field mapping."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from geoid.config import Settings
from geoid.repositories.place_repo import queryable_field_mapping
from geoid.services import ogc_service

pytestmark = pytest.mark.unit

_GEOJSON_TEXT = '{"type":"Polygon","coordinates":[[[10,10],[11,10],[11,11],[10,11],[10,10]]]}'


def _item_row() -> dict:
    return {
        "geoid": uuid.UUID("019e0000-0000-7000-8000-000000000001"),
        "geometry": _GEOJSON_TEXT,
        "created_at": datetime(2026, 6, 17, tzinfo=UTC),
        "collection_slug": "public",
    }


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Same rationale as test_config.py: integration fixtures export GEOID_* into
    # os.environ; clear what these assertions depend on.
    for key in ("GEOID_MAX_OFFSET", "GEOID_ENVIRONMENT", "GEOID_ADMIN_TOKEN", "GEOID_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _links(settings, *, number_matched, limit, offset):
    fc = ogc_service.build_feature_collection(
        settings,
        rows=[],
        collection="c",
        number_matched=number_matched,
        limit=limit,
        offset=offset,
        query_suffix="",
    )
    return {link.rel for link in fc.links}


def test_next_link_emitted_below_the_offset_cap():
    settings = _settings(max_offset=150)
    assert "next" in _links(settings, number_matched=1000, limit=100, offset=0)


def test_next_link_suppressed_when_it_would_exceed_the_cap():
    # offset=100, limit=100 -> next would be offset 200 > max_offset 150: the
    # server must not advertise a link its own guard rejects with 400.
    settings = _settings(max_offset=150)
    assert "next" not in _links(settings, number_matched=1000, limit=100, offset=100)


def test_max_offset_zero_disables_paging_links_coherently():
    settings = _settings(max_offset=0)
    assert "next" not in _links(settings, number_matched=10, limit=5, offset=0)


def test_advertised_queryables_exactly_match_the_live_filter_mapping():
    # The queryables document is a CLOSED schema (additionalProperties: false)
    # and unknown names 400 — so advertised and working sets must be identical.
    assert set(ogc_service._QUERYABLE_SCHEMAS) == set(queryable_field_mapping())


# --- WKT vendor-extension advertising + encoding -----------------------------


def _wkt_alternate(links) -> object | None:
    return next(
        (link for link in links if link.rel == "alternate" and link.type == "text/plain"), None
    )


def test_build_feature_advertises_wkt_alternate():
    feature = ogc_service.build_feature(_settings(), _item_row())
    alt = _wkt_alternate(feature.links)
    assert alt is not None
    assert alt.href.endswith("/items/019e0000-0000-7000-8000-000000000001?f=wkt")
    assert alt.title == "WKT"


def test_build_feature_collection_advertises_wkt_alternate():
    fc = ogc_service.build_feature_collection(
        _settings(),
        rows=[],
        collection="public",
        number_matched=0,
        limit=10,
        offset=0,
        query_suffix="",
    )
    alt = _wkt_alternate(fc.links)
    assert alt is not None
    assert "f=wkt" in alt.href


def test_feature_to_wkt_returns_valid_wkt():
    feature = ogc_service.build_feature(_settings(), _item_row())
    assert ogc_service.feature_to_wkt(feature) == "POLYGON ((10 10, 11 10, 11 11, 10 11, 10 10))"


def test_feature_to_wkt_handles_null_geometry():
    feature = ogc_service.build_feature(_settings(), {**_item_row(), "geometry": None})
    assert ogc_service.feature_to_wkt(feature) == ""


def test_wkt_is_not_a_conformance_class():
    # WKT is a documented vendor extension — it adds NO conformance class and does
    # not touch /conformance. Pin the set so nobody silently advertises it.
    # The 8 classes are OGC API - Features Part 1/3 + CQL2 (the read surface).
    assert len(ogc_service.CONFORMANCE_CLASSES) == 8
    joined = " ".join(ogc_service.CONFORMANCE_CLASSES).lower()
    assert "wkt" not in joined
    assert "text/plain" not in joined
