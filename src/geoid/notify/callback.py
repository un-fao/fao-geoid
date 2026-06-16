"""OGC /conf/callback — server POSTs the result to the execute body's ``subscriber``.

This is the MACHINE-readable push (distinct from the human email): on completion the
server POSTs the IngestionReport to ``subscriber.successUri`` (or a status note to
``failedUri``). Best-effort — a callback failure is logged, never raised, because the
durable job row + polling remain the source of truth.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("geoid.notify.callback")

_TIMEOUT = 15.0


def _resolve_addresses(hostname: str) -> list[str]:
    """Every IP ``hostname`` resolves to — the SSRF range check inspects all of them."""
    return [info[4][0] for info in socket.getaddrinfo(hostname, None)]


def _is_public_address(ip_str: str) -> bool:
    """True only for a routable public address; unparseable input fails closed."""
    try:
        ip = ipaddress.ip_address(ip_str.split("%")[0])  # strip any IPv6 scope id
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _callback_target_allowed(uri: str) -> bool:
    """Gate a client-supplied subscriber URI before the server fetches it.

    The server POSTs from inside the VPC, so an unrestricted callback is an SSRF
    pivot (link-local metadata, internal-only services). Require https and reject
    any host that resolves to a private/loopback/link-local/reserved address.
    Best-effort: the resolved IP is not pinned into the connection, so this is not
    hardened against DNS rebinding — proportionate for a best-effort, admin-gated
    callback whose response body is never surfaced to the caller (blind).
    """
    parsed = urlparse(uri)
    if parsed.scheme != "https":
        logger.warning("callback URI %s rejected: only https is allowed", uri)
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        logger.warning("callback URI %s rejected: no host", uri)
        return False
    try:
        addresses = _resolve_addresses(host)
    except socket.gaierror:
        logger.warning("callback URI %s rejected: host did not resolve", uri)
        return False
    if not addresses or not all(_is_public_address(a) for a in addresses):
        logger.warning("callback URI %s rejected: resolves to a non-public address", uri)
        return False
    return True


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
    if not _callback_target_allowed(uri):
        return False
    payload = report if report is not None else {"status": status}
    try:
        # follow_redirects=False so a 302 can't bounce past the SSRF check to an
        # internal target (httpx default, pinned here for intent).
        async with _http(client) as c:
            resp = await c.post(uri, json=payload, follow_redirects=False)
            resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:  # best-effort: polling is the contract
        logger.warning("callback POST to %s failed: %s", uri, exc)
        return False
