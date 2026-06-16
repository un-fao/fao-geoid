"""Unit tests for the bulk-export service's pure helpers (no DB / no storage)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.config import Settings
from geoid.services import export_service

pytestmark = pytest.mark.unit


@pytest.fixture
def settings() -> Settings:
    return Settings(base_url="http://testserver")


def test_export_summary_is_async_only_reference_output(settings):
    summary = export_service.export_summary(settings)
    assert summary.id == "bulk-export"
    assert summary.jobControlOptions == ["async-execute"]
    assert summary.outputTransmission == ["reference"]


def test_export_description_unknown_id_is_none(settings):
    assert export_service.export_description(settings, "bulk-ingest") is None


def test_export_description_advertises_format_input(settings):
    desc = export_service.export_description(settings, "bulk-export")
    assert desc is not None
    assert set(desc.inputs) == {"collection", "format"}
    assert desc.inputs["format"]["schema"]["enum"] == ["geojson", "geoparquet", "flatgeobuf"]


def test_parse_export_inputs_defaults_to_geojson():
    parsed = export_service.parse_export_inputs({"collection": "public"})
    assert parsed.collection == "public"
    assert parsed.format == "geojson"


def test_parse_export_inputs_requires_collection():
    with pytest.raises(ValidationError):
        export_service.parse_export_inputs({"format": "geojson"})


def test_parse_export_inputs_rejects_unknown_format():
    with pytest.raises(ValidationError):
        export_service.parse_export_inputs({"collection": "public", "format": "shapefile"})
