"""Unit contracts on ogc_service: the single feature's links + WKT shaping, and
the trimmed conformance set (the listing/filtering/queryables surface is gone)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from geoid.config import Settings
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
    # Integration fixtures export GEOID_* into os.environ; clear what these
    # assertions depend on so unit tests are order-independent.
    for key in ("GEOID_ENVIRONMENT", "GEOID_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# --- the single feature's self link is the durable resolver ------------------


def test_build_feature_self_link_is_the_resolver():
    feature = ogc_service.build_feature(_settings(), _item_row())
    self_link = next(link for link in feature.links if link.rel == "self")
    assert self_link.href.endswith("/019e0000-0000-7000-8000-000000000001")
    assert "/items/" not in self_link.href


def _wkt_alternate(links) -> object | None:
    return next(
        (link for link in links if link.rel == "alternate" and link.type == "text/plain"), None
    )


def test_build_feature_advertises_wkt_alternate():
    feature = ogc_service.build_feature(_settings(), _item_row())
    alt = _wkt_alternate(feature.links)
    assert alt is not None
    assert alt.href.endswith("/019e0000-0000-7000-8000-000000000001?f=wkt")
    assert alt.title == "WKT"


def test_feature_to_wkt_returns_valid_wkt():
    feature = ogc_service.build_feature(_settings(), _item_row())
    assert ogc_service.feature_to_wkt(feature) == "POLYGON ((10 10, 11 10, 11 11, 10 11, 10 10))"


def test_feature_to_wkt_handles_null_geometry():
    feature = ogc_service.build_feature(_settings(), {**_item_row(), "geometry": None})
    assert ogc_service.feature_to_wkt(feature) == ""


def test_conformance_keeps_core_drops_filter_and_cql2():
    # The item read surface (listing/filtering/queryables) was removed, so Part 3
    # (filter/queryables) and CQL2 are no longer advertised. Core/OAS30/GeoJSON stay.
    classes = ogc_service.CONFORMANCE_CLASSES
    assert len(classes) == 3
    joined = " ".join(classes).lower()
    assert "conf/core" in joined
    assert "oas30" in joined
    assert "geojson" in joined
    assert "cql2" not in joined
    assert "filter" not in joined
    assert "queryables" not in joined
    assert "wkt" not in joined
