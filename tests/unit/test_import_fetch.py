"""Unit tests for the worker's HTTPS spool (httpx MockTransport, no network).

The cap is counted while streaming — a declared Content-Length is a fast-fail
optimisation, never trusted. Exception text must never carry the URL: a
presigned URL's query string is a bearer secret.
"""

from __future__ import annotations

import httpx
import pytest

from geoid.services.import_service import ImportSourceError, _spool_https

pytestmark = pytest.mark.unit

_SECRET_URL = "https://storage.googleapis.com/b/data.json?X-Goog-Signature=SUPERSECRET"
_LABEL = "https://storage.googleapis.com/b/data.json"


class _Chunks(httpx.AsyncByteStream):
    """Hand-built body stream = full control over Content-Length presence."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _transport(response_builder):
    return httpx.MockTransport(response_builder)


async def test_spool_writes_body_to_path(tmp_path):
    body = b'{"type":"FeatureCollection","features":[]}'
    transport = _transport(lambda request: httpx.Response(200, content=body))
    path = tmp_path / "out"
    await _spool_https(_SECRET_URL, path, 1024, transport, _LABEL)
    assert path.read_bytes() == body


async def test_declared_content_length_over_cap_fast_fails(tmp_path):
    transport = _transport(
        lambda request: httpx.Response(
            200, headers={"content-length": "2048"}, stream=_Chunks([b"x" * 10])
        )
    )
    with pytest.raises(ImportSourceError, match="GEOID_JOB_MAX_BYTES"):
        await _spool_https(_SECRET_URL, tmp_path / "out", 1024, transport, _LABEL)


async def test_lying_or_absent_content_length_is_caught_by_the_counted_cap(tmp_path):
    # No content-length header at all; the body alone exceeds the cap.
    transport = _transport(
        lambda request: httpx.Response(200, stream=_Chunks([b"x" * 600, b"y" * 600]))
    )
    with pytest.raises(ImportSourceError, match="GEOID_JOB_MAX_BYTES"):
        await _spool_https(_SECRET_URL, tmp_path / "out", 1024, transport, _LABEL)


@pytest.mark.parametrize("code", [301, 302, 307, 403, 404, 500])
async def test_non_200_fails_with_the_status_code(tmp_path, code):
    transport = _transport(lambda request: httpx.Response(code, content=b""))
    with pytest.raises(ImportSourceError, match=str(code)):
        await _spool_https(_SECRET_URL, tmp_path / "out", 1024, transport, _LABEL)


async def test_exception_text_never_carries_the_url_or_its_query(tmp_path):
    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    transport = _transport(_refuse)
    with pytest.raises(ImportSourceError) as excinfo:
        await _spool_https(_SECRET_URL, tmp_path / "out", 1024, transport, _LABEL)
    text = str(excinfo.value)
    assert "SUPERSECRET" not in text
    assert "X-Goog-Signature" not in text
    assert "ConnectError" in text  # only the exception class is surfaced


async def test_upstream_error_text_never_leaks_the_secret(tmp_path):
    transport = _transport(lambda request: httpx.Response(403, content=b"denied"))
    with pytest.raises(ImportSourceError) as excinfo:
        await _spool_https(_SECRET_URL, tmp_path / "out", 1024, transport, _LABEL)
    assert "SUPERSECRET" not in str(excinfo.value)
    assert "denied" not in str(excinfo.value)  # upstream bodies stay out of messages
