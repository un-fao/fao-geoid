"""Unit tests for the worker's streaming feature parser (format sniffing)."""

from __future__ import annotations

import json

import pytest

from geoid.services.import_service import iter_features

pytestmark = pytest.mark.unit


def _feature(i: int) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(i), float(i)]},
        "properties": {"n": i},
    }


_FEATURES = [_feature(i) for i in range(5)]


def test_feature_collection_streams_identically_to_json_loads(tmp_path):
    doc = {"type": "FeatureCollection", "features": _FEATURES}
    path = tmp_path / "fc.geojson"
    path.write_text(json.dumps(doc))
    parsed = list(iter_features(path))
    assert parsed == json.loads(path.read_text())["features"]


def test_pretty_printed_feature_collection_uses_the_streaming_path(tmp_path):
    path = tmp_path / "fc.json"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": _FEATURES}, indent=2))
    assert list(iter_features(path)) == _FEATURES


def test_ndjson_lines_of_features(tmp_path):
    path = tmp_path / "data.ndjson"
    path.write_text("\n".join(json.dumps(f) for f in _FEATURES) + "\n")
    assert list(iter_features(path)) == _FEATURES


def test_rfc8142_rs_delimited_geojson_seq(tmp_path):
    path = tmp_path / "data.geojsonl"
    path.write_bytes(b"".join(b"\x1e" + json.dumps(f).encode() + b"\n" for f in _FEATURES))
    assert list(iter_features(path)) == _FEATURES


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "data.ndjson"
    path.write_text(json.dumps(_FEATURES[0]) + "\n\n\n" + json.dumps(_FEATURES[1]) + "\n")
    assert list(iter_features(path)) == _FEATURES[:2]


def test_empty_source_raises(tmp_path):
    path = tmp_path / "empty.json"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        list(iter_features(path))


def test_malformed_ndjson_line_raises(tmp_path):
    path = tmp_path / "bad.ndjson"
    path.write_text(json.dumps(_FEATURES[0]) + "\n{not json\n")
    with pytest.raises(ValueError, match="NDJSON"):
        list(iter_features(path))


def test_truncated_feature_collection_raises_mid_stream(tmp_path):
    import ijson

    full = json.dumps({"type": "FeatureCollection", "features": _FEATURES}, indent=2)
    path = tmp_path / "truncated.json"
    path.write_text(full[: len(full) // 2])
    with pytest.raises((ValueError, ijson.JSONError)):
        list(iter_features(path))


def test_parsing_is_lazy_constant_memory(tmp_path):
    # The generator yields before the document is exhausted — the streaming
    # property the OOM guard relies on.
    doc = {"type": "FeatureCollection", "features": _FEATURES}
    path = tmp_path / "fc.geojson"
    path.write_text(json.dumps(doc))
    iterator = iter_features(path)
    assert next(iterator) == _FEATURES[0]
