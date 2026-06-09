"""Unit tests for bbox parsing, including antimeridian crossing (review fix #2)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from geoid.api.cql import parse_bbox

pytestmark = pytest.mark.unit


def test_none_returns_none():
    assert parse_bbox(None) is None
    assert parse_bbox("") is None


def test_normal_bbox():
    assert parse_bbox("1,2,3,4") == (1.0, 2.0, 3.0, 4.0)


def test_antimeridian_crossing_is_allowed():
    # west (170) > east (-170) is VALID per OGC API Features — must NOT raise.
    assert parse_bbox("170,-10,-170,10") == (170.0, -10.0, -170.0, 10.0)


def test_inverted_latitude_is_rejected():
    with pytest.raises(HTTPException) as exc:
        parse_bbox("1,5,3,2")  # miny=5 > maxy=2
    assert exc.value.status_code == 400


def test_six_element_bbox_drops_z():
    assert parse_bbox("1,2,0,3,4,100") == (1.0, 2.0, 3.0, 4.0)


def test_wrong_element_count_rejected():
    with pytest.raises(HTTPException):
        parse_bbox("1,2,3")


def test_non_numeric_rejected():
    with pytest.raises(HTTPException):
        parse_bbox("a,b,c,d")
