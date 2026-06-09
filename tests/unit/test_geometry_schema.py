"""Unit tests for ingest validation (RFC 7946 + lon/lat bounds + polygon-only)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.schemas.place import PlaceCreate, iter_positions

pytestmark = pytest.mark.unit

_VALID_POLYGON = {
    "type": "Feature",
    "geometry": {"type": "Polygon", "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]]},
    "properties": {},
}


def test_iter_positions_walks_polygon():
    positions = list(iter_positions([[[0, 0], [1, 0], [1, 1], [0, 0]]]))
    assert positions == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]


def test_iter_positions_walks_multipolygon():
    mp = [[[[0, 0], [1, 0], [1, 1], [0, 0]]], [[[5, 5], [6, 5], [6, 6], [5, 5]]]]
    positions = list(iter_positions(mp))
    assert (5.0, 5.0) in positions and (1.0, 1.0) in positions
    assert len(positions) == 8


def test_accepts_valid_polygon_and_extracts_external_id_from_feature_id():
    feature = PlaceCreate.model_validate({**_VALID_POLYGON, "id": "plot-1"})
    assert feature.geometry.type == "Polygon"
    assert feature.external_id == "plot-1"


def test_external_id_is_none_when_feature_has_no_id():
    feature = PlaceCreate.model_validate(_VALID_POLYGON)
    assert feature.external_id is None


def test_accepts_multipolygon():
    feature = PlaceCreate.model_validate(
        {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]],
            },
            "properties": {},
        }
    )
    assert feature.geometry.type == "MultiPolygon"


def test_rejects_point_geometry():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {}}
        )


def test_rejects_linestring_geometry():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "properties": {},
            }
        )


def test_rejects_unclosed_ring():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
                "properties": {},
            }
        )


def test_rejects_out_of_bounds_longitude():
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[200, 0], [201, 0], [201, 1], [200, 1], [200, 0]]],
                },
                "properties": {},
            }
        )


def test_rejects_out_of_bounds_latitude_likely_swapped_lonlat():
    # lat=200 is a classic lon/lat swap; must be rejected (fail fast, before DB).
    with pytest.raises(ValidationError):
        PlaceCreate.model_validate(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 200], [1, 200], [1, 201], [0, 201], [0, 200]]],
                },
                "properties": {},
            }
        )
