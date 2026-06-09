"""CQL2 + bbox parsing for the items endpoint.

CQL2 (text or JSON) is parsed by pygeofilter and translated to a SQLAlchemy
clause over the place columns / geometry. Invalid filters fail fast with 400.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from lark.exceptions import LarkError
from pygeofilter.backends.sqlalchemy.evaluate import to_filter
from pygeofilter.parsers.cql2_json import parse as parse_cql2_json
from pygeofilter.parsers.cql2_text import parse as parse_cql2_text

FILTER_LANG_TEXT = "cql2-text"
FILTER_LANG_JSON = "cql2-json"
SUPPORTED_FILTER_LANGS = (FILTER_LANG_TEXT, FILTER_LANG_JSON)


def parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """Parse an OGC ``bbox`` query value (minx,miny,maxx,maxy[,minz,maxz])."""
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip() != ""]
    try:
        nums = [float(p) for p in parts]
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"invalid bbox (non-numeric): {raw!r}"
        ) from exc
    if len(nums) == 4:
        minx, miny, maxx, maxy = nums
    elif len(nums) == 6:  # 3D bbox: drop z
        minx, miny, _, maxx, maxy, _ = nums
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "bbox must have 4 (or 6) comma-separated numbers",
        )
    # Longitude is cyclic: a bbox that crosses the antimeridian has minx (west)
    # GREATER than maxx (east), which is valid per OGC API Features (17-069r4)
    # — e.g. bbox=170,-10,-170,10. Latitude is not cyclic, so miny>maxy is an error.
    if miny > maxy:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bbox miny must be <= maxy")
    return (minx, miny, maxx, maxy)


def build_cql_clause(
    filter_expr: str | None,
    filter_lang: str | None,
    field_mapping: dict[str, Any],
) -> Any | None:
    """Translate a CQL2 filter into a SQLAlchemy clause, or None when absent."""
    if not filter_expr:
        return None
    lang = (filter_lang or FILTER_LANG_TEXT).lower()
    if lang not in SUPPORTED_FILTER_LANGS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unsupported filter-lang {filter_lang!r}; use one of {SUPPORTED_FILTER_LANGS}",
        )
    # Only user-error families map to 400; unexpected types propagate to a 500 instead
    # of masquerading as a bad filter. LarkError = pygeofilter parse failures.
    _USER_FILTER_ERRORS = (LarkError, ValueError, KeyError, TypeError, NotImplementedError)
    try:
        ast = parse_cql2_json(filter_expr) if lang == FILTER_LANG_JSON else parse_cql2_text(
            filter_expr
        )
    except _USER_FILTER_ERRORS as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"invalid CQL2 filter: {exc}"
        ) from exc
    try:
        return to_filter(ast, field_mapping)
    except _USER_FILTER_ERRORS as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unsupported CQL2 filter: {exc}"
        ) from exc
