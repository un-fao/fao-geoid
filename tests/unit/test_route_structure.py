"""Structural guards over the app's route table.

M11: the root ``/{geoid}`` resolver is a single-segment catch-all whose safety
depends on registration ORDER (literal routes must come first). That was
convention held by a comment; these tests make it structural.

M12: ``require_principal`` deliberately resolves to an anonymous principal, so a
future mutating route that forgets its authorization check would be silently
anonymous-writable. The inventory test forces every new mutating route through a
review: it fails until the route is added here (with its authz mechanism named).
"""

from __future__ import annotations

import json

import pytest
from fastapi.routing import APIRoute

from geoid.main import create_app
from geoid.schemas.place import BulkFeatureCollection, PlaceCreate

pytestmark = pytest.mark.unit


def _paths(app) -> list[str]:
    return [route.path for route in app.routes]


def test_no_literal_single_segment_route_registers_after_the_geoid_catchall():
    paths = _paths(create_app())
    catchall = paths.index("/{geoid}")
    offenders = [
        path
        for index, path in enumerate(paths)
        if index > catchall and "{" not in path and path.count("/") == 1
    ]
    assert offenders == [], (
        f"literal single-segment routes registered AFTER the /{{geoid}} catch-all "
        f"(they would be shadowed): {offenders}"
    )


def test_all_known_literal_single_segment_routes_precede_the_catchall():
    paths = _paths(create_app())
    catchall = paths.index("/{geoid}")
    before = set(paths[:catchall])
    assert {"/", "/conformance", "/collections", "/health", "/docs", "/redoc", "/openapi.json"} <= (
        before
    )


@pytest.mark.parametrize(
    "path",
    ["/collections/{collection_id}/items", "/collections/{collection_id}/items/bulk"],
)
def test_registry_post_prefills_collection_id_with_the_public_collection(path):
    # Swagger seeds the Try-it-out box from `schema.example` — a FastAPI upgrade that
    # moved or dropped the key would silently empty the box. The absent `examples` map
    # is the no-dropdown requirement: it seeds the box too, but via a one-option select.
    parameters = create_app().openapi()["paths"][path]["post"]["parameters"]
    collection_id = next(p for p in parameters if p["name"] == "collection_id")
    assert collection_id["schema"]["example"] == "public"
    assert "examples" not in collection_id


def test_registry_post_body_schemas_carry_executable_examples():
    # The body examples must stay EXECUTABLE (first click mints) and properties-free
    # (an omitted properties member is the point of the leniency). Validating them
    # through the models themselves pins executability without duplicating literals;
    # distinct geometries pin that single-then-bulk first clicks never cross-409.
    schemas = create_app().openapi()["components"]["schemas"]

    place = schemas["PlaceCreate"]
    assert place["required"] == ["type", "geometry"]
    assert "properties" not in place["example"]
    PlaceCreate.model_validate(place["example"])

    bulk = schemas["BulkFeatureCollection"]
    BulkFeatureCollection.model_validate(bulk["example"])
    for feature in bulk["example"]["features"]:
        assert "properties" not in feature
        PlaceCreate.model_validate(feature)

    geometries = [place["example"]["geometry"]] + [
        f["geometry"] for f in bulk["example"]["features"]
    ]
    assert len({json.dumps(g, sort_keys=True) for g in geometries}) == len(geometries)


@pytest.mark.parametrize(
    "path",
    ["/collections/{collection_id}/items", "/collections/{collection_id}/items/bulk"],
)
def test_registry_post_body_has_no_examples_dropdown(path):
    # Mirror of the path-param pin: the body must seed from `schema.example` only —
    # an `examples` map would render as a Swagger dropdown.
    content = create_app().openapi()["paths"][path]["post"]["requestBody"]["content"]
    assert "examples" not in content["application/json"]


def test_every_mutating_route_is_in_the_authorized_inventory():
    app = create_app()
    mutating = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    # Every entry's authorization mechanism, verified at review time:
    #   items / items/bulk  -> registry_service._authorize_write (service layer)
    #   grants routes       -> api.grants._require_manageable (anon→401, owner|sysadmin)
    #   /manage/*           -> require_admin router dependency
    # Adding a mutating route? Wire its authz, then extend this set.
    assert mutating == {
        ("POST", "/collections/{collection_id}/items"),
        ("POST", "/collections/{collection_id}/items/bulk"),
        ("POST", "/collections/{collection_id}/grants"),
        ("DELETE", "/collections/{collection_id}/grants/{email}"),
        ("POST", "/manage/collections"),
    }
