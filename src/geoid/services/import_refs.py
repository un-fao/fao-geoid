"""Import source-ref validation + sanitization (pure, network-free).

Run at submit time (422 on rejection) AND re-run by the worker (defense in
depth — the row could be edited between submit and run). Two controls:

* **https**: a vendor host allowlist (``GEOID_JOB_ALLOWED_URL_HOSTS``) is THE
  SSRF control — the worker only ever fetches storage vendors' hosts, so
  metadata endpoints / internal ranges are unreachable by construction (GCP
  metadata's ``Metadata-Flavor`` requirement is only a backstop). DNS-rebinding
  machinery is deliberately skipped while the hosts are vendor-fixed.
* **gs**: the bucket must be in ``GEOID_JOB_ALLOWED_BUCKETS`` (default empty =
  gs:// disabled) — kills the confused-deputy problem (any authenticated caller
  pointing our service account at any bucket it can read).

A signed URL's query string is a bearer secret: :func:`sanitized_source` is the
ONLY form that may appear in results, logs, or error text.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

from geoid.config import Settings


def sanitized_source(ref: str) -> str:
    """Scheme+host+path with the query/fragment stripped (signed-URL secret)."""
    parts = urlsplit(ref)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _host_allowed(host: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and host != pattern[1:].lstrip("."):
                return True
        elif host == pattern:
            return True
    return False


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def parse_gs(ref: str) -> tuple[str, str]:
    """Split ``gs://bucket/object`` into (bucket, object). Raises ValueError."""
    parts = urlsplit(ref)
    if parts.scheme != "gs" or not parts.netloc:
        raise ValueError("gs ref must look like gs://bucket/object")
    return parts.netloc, parts.path.lstrip("/")


def validate_href(href: str, settings: Settings) -> None:
    """One-object ref: https against the host allowlist, or an allowed gs:// object."""
    parts = urlsplit(href)
    if parts.scheme == "gs":
        _validate_gs(href, settings)
        return
    if parts.scheme != "https":
        raise ValueError("import href must be an https:// URL or a gs:// object")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("import href must not carry userinfo")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("import href has no host")
    if _is_ip_literal(host):
        raise ValueError("import href must use a hostname, not an IP literal")
    if parts.port is not None and parts.port != 443:
        raise ValueError("import href must use the default https port")
    if not _host_allowed(host, settings.job_allowed_url_hosts_list):
        raise ValueError(f"import href host {host!r} is not in the allowed storage hosts")


def validate_prefix(prefix: str, settings: Settings) -> None:
    """Prefix ref: gs:// only (https has no portable listing semantics)."""
    if not prefix.startswith("gs://"):
        raise ValueError("import prefix must be a gs://bucket/path/ reference")
    _validate_gs(prefix, settings)


def _validate_gs(ref: str, settings: Settings) -> None:
    bucket, _ = parse_gs(ref)
    if bucket not in settings.job_allowed_buckets_list:
        raise ValueError(
            f"bucket {bucket!r} is not in GEOID_JOB_ALLOWED_BUCKETS "
            "(gs:// refs are disabled until buckets are allowlisted)"
        )


def validate_ref(ref: str, *, is_prefix: bool, settings: Settings) -> None:
    """The one entry point both the submit route and the worker call."""
    if is_prefix:
        validate_prefix(ref, settings)
    else:
        validate_href(ref, settings)
