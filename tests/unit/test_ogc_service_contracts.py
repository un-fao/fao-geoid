"""Unit contracts on ogc_service: paging links honor the offset cap, and the
advertised queryables set is pinned to the live CQL2 field mapping."""

from __future__ import annotations

import pytest

from geoid.config import Settings
from geoid.repositories.place_repo import queryable_field_mapping
from geoid.services import ogc_service

pytestmark = pytest.mark.unit


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
