"""Unit tests for the BlobStore seam (LocalFSStore + factory)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.config import Settings
from geoid.storage import LocalFSStore, _build
from geoid.storage.local import LocalFSStore as LocalDirect

pytestmark = pytest.mark.unit


async def test_local_put_get_exists_roundtrip(tmp_path):
    store = LocalDirect(str(tmp_path))
    url = await store.put("sub/dir/blob.txt", b"hello geoid")
    assert url.startswith("file://")
    assert await store.exists("sub/dir/blob.txt") is True
    assert await store.get("sub/dir/blob.txt") == b"hello geoid"


async def test_local_exists_false_for_missing(tmp_path):
    store = LocalDirect(str(tmp_path))
    assert await store.exists("nope.txt") is False


async def test_local_path_traversal_is_neutralized(tmp_path):
    store = LocalDirect(str(tmp_path))
    await store.put("../../../etc/evil.txt", b"x")
    escaped = (tmp_path / ".." / ".." / ".." / "etc" / "evil.txt").resolve()
    assert not escaped.exists()
    assert list(tmp_path.rglob("evil.txt"))  # landed under the root instead


def test_url_for_is_file_uri(tmp_path):
    store = LocalDirect(str(tmp_path))
    assert store.url_for("a.txt").startswith("file://")


def test_factory_builds_local(tmp_path):
    settings = Settings(_env_file=None, storage_backend="local", storage_local_root=str(tmp_path))
    assert isinstance(_build(settings), LocalFSStore)


def test_unknown_backend_is_rejected_at_construction():
    # The Literal rejects an unknown backend at construction, before the factory.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, storage_backend="weird")


def test_factory_gcs_requires_bucket():
    settings = Settings(_env_file=None, storage_backend="gcs", storage_gcs_bucket=None)
    with pytest.raises(ValueError, match="GEOID_STORAGE_GCS_BUCKET"):
        _build(settings)
