"""Unit tests for import source-ref validation (the SSRF / confused-deputy gates)."""

from __future__ import annotations

import pytest

from geoid.config import Settings
from geoid.services.import_refs import (
    parse_gs,
    sanitized_source,
    validate_href,
    validate_prefix,
    validate_ref,
)

pytestmark = pytest.mark.unit


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    "href",
    [
        "https://storage.googleapis.com/bucket/data.json?X-Goog-Signature=abc",
        "https://mybucket.storage.googleapis.com/data.json",
        "https://s3.amazonaws.com/bucket/key.geojson",
        "https://bucket.s3.amazonaws.com/key.geojson",
        "https://bucket.s3.eu-west-1.amazonaws.com/key.geojson",
    ],
)
def test_default_vendor_hosts_are_allowed(href):
    validate_href(href, _settings())  # must not raise


@pytest.mark.parametrize(
    "href",
    [
        "http://storage.googleapis.com/bucket/data.json",  # scheme
        "https://evil.example.com/data.json",  # host not allowlisted
        "https://169.254.169.254/computeMetadata/v1/",  # metadata IP
        "https://metadata.google.internal/computeMetadata/v1/",  # metadata host
        "https://[::1]/data.json",  # IPv6 literal
        "https://user@storage.googleapis.com/data.json",  # userinfo
        "https://storage.googleapis.com:8443/data.json",  # non-443 port
        "https://amazonaws.com/data.json",  # bare suffix must not match *.amazonaws.com
        "ftp://storage.googleapis.com/data.json",
        "https:///data.json",  # no host
    ],
)
def test_hostile_or_off_allowlist_hrefs_are_rejected(href):
    with pytest.raises(ValueError):
        validate_href(href, _settings())


def test_gs_href_rejected_while_bucket_allowlist_is_empty():
    with pytest.raises(ValueError, match="GEOID_JOB_ALLOWED_BUCKETS"):
        validate_href("gs://any-bucket/data.json", _settings())


def test_gs_href_allowed_when_bucket_is_allowlisted():
    settings = _settings(job_allowed_buckets="fao-geoid-imports")
    validate_href("gs://fao-geoid-imports/data.json", settings)
    with pytest.raises(ValueError):
        validate_href("gs://other-bucket/data.json", settings)


def test_prefix_must_be_gs():
    settings = _settings(job_allowed_buckets="fao-geoid-imports")
    validate_prefix("gs://fao-geoid-imports/batch/", settings)
    with pytest.raises(ValueError, match="gs://"):
        validate_prefix("https://storage.googleapis.com/bucket/batch/", settings)


def test_validate_ref_dispatches_on_is_prefix():
    settings = _settings(job_allowed_buckets="b")
    validate_ref("gs://b/p/", is_prefix=True, settings=settings)
    validate_ref("https://storage.googleapis.com/b/o.json", is_prefix=False, settings=settings)
    with pytest.raises(ValueError):
        validate_ref("https://storage.googleapis.com/b/", is_prefix=True, settings=settings)


def test_parse_gs_splits_bucket_and_object():
    assert parse_gs("gs://bucket/path/to/obj.json") == ("bucket", "path/to/obj.json")
    with pytest.raises(ValueError):
        parse_gs("s3://bucket/key")


def test_sanitized_source_strips_query_and_fragment():
    # The query string of a presigned URL is a bearer secret.
    signed = "https://storage.googleapis.com/b/o.json?X-Goog-Signature=SECRET#frag"
    assert sanitized_source(signed) == "https://storage.googleapis.com/b/o.json"
    assert "SECRET" not in sanitized_source(signed)


def test_sanitized_source_leaves_gs_refs_intact():
    assert sanitized_source("gs://bucket/obj.json") == "gs://bucket/obj.json"


def test_custom_host_allowlist_replaces_defaults():
    settings = _settings(job_allowed_url_hosts="files.example.org")
    validate_href("https://files.example.org/data.json", settings)
    with pytest.raises(ValueError):
        validate_href("https://storage.googleapis.com/bucket/data.json", settings)
