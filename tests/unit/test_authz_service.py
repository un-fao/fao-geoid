"""Unit truth-table for the pure authz predicates (can_write / can_read / can_manage).

``load_caller_grant`` touches the DB and is exercised in the integration suite; here
the predicates are pure functions over (principal, collection-view / public_read
flag, grant).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from geoid.deps import Principal
from geoid.services import authz_service

pytestmark = pytest.mark.unit


def _collection(*, writable_anon: bool):
    return SimpleNamespace(writable_anon=writable_anon)


def _grant(role: str):
    return SimpleNamespace(role=role)


ANON = Principal.anonymous()
ADMIN = Principal.admin()
USER = Principal(subject="kc-1", email="u@x.org", email_verified=True)

CLOSED = _collection(writable_anon=False)
OPEN = _collection(writable_anon=True)

# can_read takes the public_read flag as a scalar (read paths hold it on the row).
PUBLIC_READ = True
PRIVATE_READ = False


# --- can_write ----------------------------------------------------------------


def test_sysadmin_writes_anything():
    assert authz_service.can_write(ADMIN, CLOSED, None) is True


def test_writable_anon_allows_anonymous_write():
    assert authz_service.can_write(ANON, OPEN, None) is True


def test_viewer_cannot_write():
    assert authz_service.can_write(USER, CLOSED, _grant("viewer")) is False


def test_editor_can_write():
    assert authz_service.can_write(USER, CLOSED, _grant("editor")) is True


def test_owner_can_write():
    assert authz_service.can_write(USER, CLOSED, _grant("owner")) is True


def test_authenticated_non_grantee_cannot_write_closed_collection():
    assert authz_service.can_write(USER, CLOSED, None) is False


def test_authenticated_non_grantee_can_write_open_collection():
    # writable_anon means open-to-all writes (anonymous AND authenticated).
    assert authz_service.can_write(USER, OPEN, None) is True


# --- can_read -----------------------------------------------------------------


def test_sysadmin_reads_anything():
    assert authz_service.can_read(ADMIN, PRIVATE_READ, None) is True


def test_public_read_allows_anonymous_read():
    assert authz_service.can_read(ANON, PUBLIC_READ, None) is True


def test_private_collection_blocks_anonymous_read():
    assert authz_service.can_read(ANON, PRIVATE_READ, None) is False


def test_any_grant_reads_private_collection():
    for role in ("viewer", "editor", "owner"):
        assert authz_service.can_read(USER, PRIVATE_READ, _grant(role)) is True


def test_authenticated_non_grantee_cannot_read_private_collection():
    assert authz_service.can_read(USER, PRIVATE_READ, None) is False


def test_authenticated_non_grantee_reads_public_collection():
    assert authz_service.can_read(USER, PUBLIC_READ, None) is True


# --- can_manage ---------------------------------------------------------------


def test_sysadmin_manages_anything():
    assert authz_service.can_manage(ADMIN, CLOSED, None) is True


def test_only_owner_grant_manages():
    assert authz_service.can_manage(USER, CLOSED, _grant("owner")) is True
    assert authz_service.can_manage(USER, CLOSED, _grant("editor")) is False
    assert authz_service.can_manage(USER, CLOSED, _grant("viewer")) is False
    assert authz_service.can_manage(USER, CLOSED, None) is False
