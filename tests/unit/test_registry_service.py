"""Unit tests for registry_service error-handling branches (no DB, no Docker).

Covers the two "should not happen" paths hardened in the review:
- a dedup loser whose incumbent collection never materialises → RegistryConsistencyError
- an unrecognised integrity constraint in bulk → an honest ``internal_error`` reject
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from geoid.config import Settings
from geoid.deps import Principal
from geoid.models import Collection
from geoid.repositories import place_repo
from geoid.repositories.place_repo import InsertResult
from geoid.schemas.place import PlaceCreate
from geoid.services import registry_service
from geoid.services.exceptions import GeometryInvalidError, RegistryConsistencyError

pytestmark = pytest.mark.unit

_SQUARE = {
    "type": "Feature",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
    },
    "properties": {},
}


async def test_create_place_raises_registry_consistency_when_incumbent_missing(monkeypatch):
    # Force the drift: insert_place reports a dedup loser (created=False) whose
    # incumbent collection slug never resolved (None). create_place must raise the
    # structured RegistryConsistencyError carrying the geoid — not crash on a None slug.
    drift_geoid = uuid.uuid4()

    async def _drift(*args, **kwargs):
        return InsertResult(geoid=drift_geoid, created=False, collection_slug=None)

    monkeypatch.setattr(place_repo, "insert_place", _drift)

    feature = PlaceCreate.model_validate(_SQUARE)
    collection = Collection(id=uuid.uuid4(), catalog_id=uuid.uuid4(), slug="x", writable_anon=True)

    with pytest.raises(RegistryConsistencyError) as exc_info:
        await registry_service.create_place(
            None,  # session is unused once insert_place is faked
            settings=Settings(),
            principal=Principal.admin(),
            collection=collection,
            feature=feature,
        )
    assert exc_info.value.geoid == drift_geoid


# --- DBAPIError sqlstate discrimination (H2): only known ST_GeomFromGeoJSON ----
# parse states become the client-facing 422; anything else stays loud.


class _FakeDriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("boom")
        self.sqlstate = sqlstate


class _FakeSession:
    """Just enough session for the error paths: rollback + SAVEPOINT context."""

    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True

    def begin_nested(self):
        class _Nested:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Nested()


def _collection() -> Collection:
    return Collection(id=uuid.uuid4(), catalog_id=uuid.uuid4(), slug="x", writable_anon=True)


def _raise_dbapi(sqlstate: str):
    async def _raise(*args, **kwargs):
        raise DBAPIError("INSERT ...", {}, _FakeDriverError(sqlstate))

    return _raise


async def test_unknown_dbapi_sqlstate_reraises_instead_of_422(monkeypatch):
    # 42883 (UndefinedFunction — the June-15 schema-drift outage signature) must NOT
    # be masked as the client's "unparseable GeoJSON": re-raise for a loud 500.
    monkeypatch.setattr(place_repo, "insert_place", _raise_dbapi("42883"))
    with pytest.raises(DBAPIError):
        await registry_service.create_place(
            _FakeSession(),
            settings=Settings(),
            principal=Principal.admin(),
            collection=_collection(),
            feature=PlaceCreate.model_validate(_SQUARE),
        )


async def test_geojson_parse_sqlstate_maps_to_geometry_invalid(monkeypatch):
    # XX000 (PostGIS lwgeom parse error) IS the malformed-GeoJSON class -> 422.
    monkeypatch.setattr(place_repo, "insert_place", _raise_dbapi("XX000"))
    session = _FakeSession()
    with pytest.raises(GeometryInvalidError):
        await registry_service.create_place(
            session,
            settings=Settings(),
            principal=Principal.admin(),
            collection=_collection(),
            feature=PlaceCreate.model_validate(_SQUARE),
        )
    assert session.rolled_back is True


async def test_bulk_unknown_dbapi_sqlstate_aborts_the_batch(monkeypatch):
    # In bulk, a programming/schema failure must abort the whole batch loudly — a
    # mislabelled per-feature invalid_geometry would hide a server-side outage.
    monkeypatch.setattr(place_repo, "insert_place", _raise_dbapi("42883"))
    with pytest.raises(DBAPIError):
        await registry_service.create_places_bulk(
            _FakeSession(),
            settings=Settings(),
            principal=Principal.admin(),
            collection=_collection(),
            features=[_SQUARE],
        )


async def test_bulk_geojson_parse_sqlstate_rejects_the_row(monkeypatch):
    monkeypatch.setattr(place_repo, "insert_place", _raise_dbapi("22023"))
    report = await registry_service.create_places_bulk(
        _FakeSession(),
        settings=Settings(),
        principal=Principal.admin(),
        collection=_collection(),
        features=[_SQUARE],
    )
    assert report.summary.rejected == 1
    assert report.rejected[0].reason == "invalid_geometry"


async def test_classify_integrity_unknown_constraint_is_internal_error(monkeypatch):
    # An unrecognised constraint must surface honestly as internal_error (and be
    # logged), NOT be mislabelled invalid_geometry. The fallthrough never touches the
    # session, so None is safe here.
    monkeypatch.setattr(
        registry_service, "pg_fields", lambda exc: ("some_unexpected_constraint", "99999")
    )
    exc = IntegrityError("INSERT ...", {}, Exception("boom"))

    rejected = await registry_service._classify_integrity(
        None, index=3, external_id="ext-9", geojson="{}", exc=exc
    )

    assert rejected.reason == "internal_error"
    assert rejected.index == 3
    assert rejected.external_id == "ext-9"
    assert "some_unexpected_constraint" in (rejected.detail or "")
