"""Paging guard rails shared by the public read and management listing endpoints."""

from __future__ import annotations

from fastapi import HTTPException, status

from geoid.config import Settings


def enforce_max_offset(offset: int, settings: Settings) -> None:
    """Reject offsets beyond the configured cap with a 400.

    OFFSET makes the database scan and discard ``offset`` rows per request, so an
    unbounded value is a cheap way to pin a worker. The cap applies to the public
    items endpoint AND the admin item-ids listing (full enumeration of collections
    larger than max_offset+limit needs keyset paging — a tracked follow-up, raise
    GEOID_MAX_OFFSET in the interim). Statement timeouts bound the damage; this
    bounds the work. Call it before any I/O.
    """
    if offset > settings.max_offset:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"offset {offset} exceeds the maximum of {settings.max_offset}",
        )
