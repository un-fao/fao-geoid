"""Provenance shaping — the ``geoid-prov/0.1`` contract.

Provenance is an append-only JSON object stored on the place. It records *who*
(``created_by``; ``null`` for anonymous contributions), *which instance* minted
it (``originating_instance``; the federation seam), and *which client tool* sent
it (``client``; mirrors the ``_whisp.version`` we already see on Whisp features).

Authority is deliberately NOT here: a claim of authority *over* a place lives in
the append-only ``authority_assertion`` side table, never as a mutable column.
This is what answers "anonymous yet authority-tracked" architecturally.
"""

from __future__ import annotations

from typing import Any

PROV_SCHEMA = "geoid-prov/0.1"

# Property keys on an incoming GeoJSON Feature that we recognise as client
# provenance (Whisp stamps ``_whisp`` with a ``version``; DynaStore features carry
# the same shape). Anything else stays untouched in the feature's properties.
_CLIENT_PROPERTY_KEYS = ("_whisp", "_client", "client")


def extract_client(properties: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull a client-provenance descriptor out of incoming feature properties.

    Recognises the ``_whisp`` provenance block FAO's EUDR tool already emits and
    normalises it to ``{"name": ..., "version": ...}`` plus the raw block. Returns
    ``None`` when no client provenance is present.
    """
    if not properties:
        return None
    for key in _CLIENT_PROPERTY_KEYS:
        block = properties.get(key)
        if block is None:
            continue
        if isinstance(block, dict):
            name = "whisp" if key == "_whisp" else block.get("name", key.lstrip("_"))
            return {"name": name, "version": block.get("version"), "raw": block}
        # A bare scalar (e.g. a version string) still counts as client provenance.
        return {"name": key.lstrip("_"), "version": block, "raw": block}
    return None


def build_provenance(
    *,
    created_by: str | None,
    originating_instance: str,
    client: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical provenance object stored on ``place.provenance``.

    Args:
        created_by: Subject of the authenticated principal, or ``None`` for anon.
        originating_instance: The minting instance id (federation seam).
        client: Normalised client descriptor (see :func:`extract_client`).
        extra: Additional provenance facts to merge (e.g. ingest batch metadata).
    """
    prov: dict[str, Any] = {
        "schema": PROV_SCHEMA,
        "created_by": created_by,
        "originating_instance": originating_instance,
        "client": client,
    }
    if extra:
        prov.update(extra)
    return prov
