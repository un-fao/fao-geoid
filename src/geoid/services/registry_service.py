"""The registry service — the product: mint + dedup + provenance.

Anonymous and managed contributions share this ONE code path; the only branch is
the data-layer policy check (``writable_anon``). The geoid/dedup/external-id rules
are identical for both — anonymity is not a special case.
"""

from __future__ import annotations

import json

from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from geoid.config import Settings
from geoid.deps import Principal
from geoid.domain.identifiers import derive_identifiers, new_geoid
from geoid.domain.provenance import build_provenance, extract_client
from geoid.models import Collection
from geoid.repositories import place_repo
from geoid.schemas.place import MintResponse, PlaceCreate
from geoid.services.exceptions import (
    AnonymousWriteForbiddenError,
    GeometryConflictError,
    GeometryInvalidError,
)

# Geometry validity (ST_IsValid) and polygon-only are enforced by CHECK constraints
# on the INSERT (SQLSTATE 23514). Polygon-only + lon/lat bounds + RFC 7946 structure
# are ALSO enforced earlier by the PlaceCreate pydantic schema (422 before the DB).
_SQLSTATE_CHECK_VIOLATION = "23514"


def _geometry_to_geojson(feature: PlaceCreate) -> str:
    """Serialise the feature's geometry to a GeoJSON geometry string for PostGIS."""
    geom = feature.geometry
    return json.dumps({"type": geom.type, "coordinates": geom.coordinates})


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


async def create_place(
    session: AsyncSession,
    *,
    settings: Settings,
    principal: Principal,
    collection: Collection,
    feature: PlaceCreate,
) -> MintResponse:
    """Mint a geoid for ``feature`` in ``collection``.

    Raises:
        AnonymousWriteForbiddenError: anon POST to a non-anonymous collection.
        GeometryInvalidError: geometry unparseable / invalid (422 with reason).
        GeometryConflictError: identical geometry already registered anywhere in
            the catalog (409 carrying the incumbent geoid).
    """
    if principal.is_anonymous and not collection.writable_anon:
        raise AnonymousWriteForbiddenError(collection.slug)

    geojson = _geometry_to_geojson(feature)
    external_id = feature.external_id
    client = extract_client(feature.properties)
    provenance = build_provenance(
        created_by=principal.subject,
        originating_instance=settings.instance_id,
        client=client,
        extra={"submitted_properties": feature.properties or {}},
    )

    geoid = new_geoid()
    # Happy path is one INSERT round-trip: the DB CHECK constraints reject invalid
    # geometry (23514) and we recover ST_IsValidReason for the 422 ONLY on that
    # error path — so a valid POST never pays a separate pre-validation query.
    try:
        result = await place_repo.insert_place(
            session,
            geoid=geoid,
            collection_id=collection.id,
            geojson=geojson,
            external_id=external_id,
            provenance=provenance,
            originating_instance=settings.instance_id,
            dedup_grid_default=settings.dedup_grid_default,
        )
    except IntegrityError as exc:
        if _sqlstate(exc) == _SQLSTATE_CHECK_VIOLATION:
            await session.rollback()
            reason = await place_repo.geometry_invalid_reason(session, geojson)
            raise GeometryInvalidError(reason) from exc
        raise  # 23505 (external_id / geoid duplicate) → mapped to 409 by api/errors.py
    except (OperationalError, InterfaceError):
        raise  # genuine infra failure — never mask as a 422
    except DBAPIError as exc:
        # Malformed GeoJSON makes ST_GeomFromGeoJSON raise and aborts the tx.
        await session.rollback()
        raise GeometryInvalidError("unparseable GeoJSON geometry") from exc

    if not result.created:
        # Identical geometry already registered (anywhere in the catalog): the
        # insert failed, and the 409 must carry the incumbent geoid + collection.
        # The repo's incumbent lookup guarantees collection_slug when created=False.
        raise GeometryConflictError(geoid=result.geoid, collection=result.collection_slug)

    ids = derive_identifiers(
        result.geoid,
        base_url=settings.base_url_clean,
        did_host=settings.did_host or "",
        collection=collection.slug,
    )
    return MintResponse(
        geoid=ids["geoid"],
        did=ids["did"],
        uri=ids["uri"],
        item_url=ids["item_url"],
        collection=collection.slug,
        external_id=external_id,
    )
