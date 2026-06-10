"""Unit tests for provenance shaping and vocabulary aliasing."""

from __future__ import annotations

import pytest

from geoid.domain import provenance, vocab

pytestmark = pytest.mark.unit


# --- provenance -------------------------------------------------------------

def test_extract_client_from_whisp_block():
    client = provenance.extract_client({"_whisp": {"version": "2.1.0", "run": "abc"}})
    assert client is not None
    assert client["name"] == "whisp"
    assert client["version"] == "2.1.0"
    assert client["raw"] == {"version": "2.1.0", "run": "abc"}


def test_extract_client_from_scalar_value():
    client = provenance.extract_client({"client": "qgis-3.34"})
    assert client == {"name": "client", "version": "qgis-3.34", "raw": "qgis-3.34"}


def test_extract_client_returns_none_when_absent():
    assert provenance.extract_client({"some": "attribute"}) is None
    assert provenance.extract_client(None) is None


def test_build_provenance_anonymous_has_null_created_by():
    prov = provenance.build_provenance(created_by=None, originating_instance="fao-central")
    assert prov["schema"] == "geoid-prov/0.1"
    assert prov["created_by"] is None
    assert prov["originating_instance"] == "fao-central"
    assert prov["client"] is None


def test_build_provenance_merges_extra():
    prov = provenance.build_provenance(
        created_by="admin",
        originating_instance="fao-central",
        client={"name": "whisp", "version": "1.0"},
        extra={"submitted_properties": {"area_ha": 3.2}},
    )
    assert prov["created_by"] == "admin"
    assert prov["submitted_properties"] == {"area_ha": 3.2}


# --- vocab ------------------------------------------------------------------

def test_fao_vocab_is_the_default_and_matches_the_ruling():
    # the reviewer's final terminology ruling: workspace / collection / item.
    assert vocab.DEFAULT_VOCAB == "fao"
    v = vocab.get_vocab("fao")
    assert v.label("workspace") == "workspace"
    assert v.label("collection") == "collection"
    assert v.label("place") == "item"
    assert v.item_type == "item"
    assert v.plural("place") == "items"


def test_stac_vocab_labels():
    v = vocab.get_vocab("stac")
    assert v.label("workspace") == "catalog"
    assert v.label("place") == "item"
    assert v.item_type == "item"
    assert v.plural("place") == "items"


def test_neutral_vocab_labels():
    v = vocab.get_vocab("neutral")
    assert v.label("workspace") == "workspace"
    assert v.label("place") == "place"
    assert v.plural("place") == "places"


def test_unknown_vocab_falls_back_to_default():
    v = vocab.get_vocab("klingon")
    assert v.name == vocab.DEFAULT_VOCAB
