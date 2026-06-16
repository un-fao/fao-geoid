"""OGC /conf/callback — server POSTs the result to the execute body's ``subscriber``.

This is the MACHINE-readable push (distinct from the human email): on completion the
server POSTs the IngestionReport to ``subscriber.successUri`` (or a status note to
``failedUri``). Best-effort — a callback failure is logged, never raised, because the
durable job row + polling remain the source of truth.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("geoid.notify.callback")

_TIMEOUT = 15.0


@asynccontextmanager
async def _http(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """Yield the injected client (tests) or a fresh one closed on exit (prod)."""
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as fresh:
            yield fresh


async def post_callback(
    subscriber: dict[str, Any] | None,
    *,
    report: dict[str, Any] | None,
    status: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """POST the result to the matching subscriber URI. Returns True iff one was sent."""
    if not subscriber:
        return False
    uri = subscriber.get("successUri") if status == "successful" else subscriber.get("failedUri")
    if not uri:
        return False
    # Subscriber URIs are client-supplied; require https so a callback can't be
    # aimed at an internal http service (confused-deputy / SSRF guard).
    if urlparse(uri).scheme != "https":
        logger.warning("callback URI %s rejected: only https is allowed", uri)
        return False
    payload = report if report is not None else {"status": status}
    try:
        async with _http(client) as c:
            resp = await c.post(uri, json=payload)
            resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:  # best-effort: polling is the contract
        logger.warning("callback POST to %s failed: %s", uri, exc)
        return False
