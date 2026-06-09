"""Pydantic request/response schemas — the API's typed boundary.

Input geometry is validated by ``geojson-pydantic`` (RFC 7946 structure +
polygon-only) plus our own lon/lat bounds check; ``ST_IsValid`` is the final
gate in the database (reject, don't repair).
"""
