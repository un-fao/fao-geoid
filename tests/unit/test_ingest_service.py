"""Unit tests for the bulk-ingest service helpers and the OGC Processes surface."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from geoid.config import Settings
from geoid.services import ingest_service, ogc_service

pytestmark = pytest.mark.unit


@pytest.fixture
def settings() -> Settings:
    return Settings(base_url="http://testserver")


def test_process_list_offers_bulk_ingest_and_bulk_export(settings):
    listing = ingest_service.process_list(settings)
    assert [p.id for p in listing.processes] == ["bulk-ingest", "bulk-export"]


def test_bulk_export_process_is_async_only_with_reference_output(settings):
    listing = ingest_service.process_list(settings)
    export = next(p for p in listing.processes if p.id == "bulk-export")
    assert export.jobControlOptions == ["async-execute"]
    assert export.outputTransmission == ["reference"]


def test_process_description_bulk_export_describes_format_input(settings):
    desc = ingest_service.process_description(settings, "bulk-export")
    assert desc is not None
    assert set(desc.inputs) == {"collection", "format"}
    assert "download" in desc.outputs


def test_process_description_known_id_has_inputs_and_outputs(settings):
    desc = ingest_service.process_description(settings, "bulk-ingest")
    assert desc is not None
    assert set(desc.inputs) == {"collection", "items", "idempotencyKey"}
    assert "report" in desc.outputs


def test_process_description_unknown_id_is_none(settings):
    assert ingest_service.process_description(settings, "does-not-exist") is None


def test_parse_inputs_requires_collection_and_items():
    with pytest.raises(ValidationError):
        ingest_service.parse_inputs({"items": {"type": "FeatureCollection", "features": []}})


def test_parse_inputs_accepts_optional_idempotency_key():
    parsed = ingest_service.parse_inputs(
        {"collection": "public", "items": {"features": []}, "idempotencyKey": "k1"}
    )
    assert parsed.collection == "public"
    assert parsed.idempotencyKey == "k1"


def test_features_by_value_extracts_feature_list():
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature"}]}
    assert ingest_service._features_by_value(fc) == [{"type": "Feature"}]


def test_features_by_reference_returns_none():
    assert ingest_service._features_by_value({"href": "gs://bucket/blob.geojson"}) is None


def test_features_by_value_takes_precedence_over_href():
    fc = {"href": "gs://x", "features": [{"type": "Feature"}]}
    assert ingest_service._features_by_value(fc) == [{"type": "Feature"}]


def test_features_without_features_or_href_raises():
    with pytest.raises(ValueError, match="FeatureCollection"):
        ingest_service._features_by_value({"type": "FeatureCollection"})


def test_place_set_uri_uses_base_url_clean(settings):
    batch = uuid.UUID("019e0000-0000-7000-8000-000000000000")
    assert ingest_service.place_set_uri(settings, batch) == f"http://testserver/place-sets/{batch}"


async def test_ingest_features_cap_rejects_oversized_batch():
    # The cap fires before any DB work, so a dummy session is never touched.
    from types import SimpleNamespace

    from geoid.deps import Principal
    from geoid.domain.identifiers import new_geoid
    from geoid.services.exceptions import BulkLimitExceededError

    settings = Settings(base_url="http://testserver", bulk_max_features=2)
    collection = SimpleNamespace(id=new_geoid(), slug="public", writable_anon=True)
    with pytest.raises(BulkLimitExceededError):
        await ingest_service.ingest_features(
            None,
            settings=settings,
            principal=Principal.admin(),
            collection=collection,
            features=[{}, {}, {}],
            batch_id=new_geoid(),
        )


def test_conformance_advertises_processes_core():
    classes = ogc_service.conformance().conformsTo
    assert "http://www.opengis.net/spec/ogcapi-processes-1/1.0/conf/core" in classes


def test_landing_page_links_to_processes(settings):
    rels = {link.rel for link in ogc_service.landing_page(settings).links}
    assert "http://www.opengis.net/def/rel/ogc/1.0/processes" in rels
