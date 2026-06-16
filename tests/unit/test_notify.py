"""Unit tests for the completion-notification seam (Graph sendMail + OGC callback)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from geoid.config import Settings
from geoid.notify import NullNotifier, get_notifier
from geoid.notify.callback import post_callback
from geoid.notify.graph import GraphNotifier
from geoid.schemas.ingest import IngestionReport

pytestmark = pytest.mark.unit


def _settings(**overrides) -> Settings:
    base = {
        "base_url": "http://testserver",
        "notify_backend": "graph",
        "graph_tenant_id": "tenant",
        "graph_client_id": "client",
        "graph_client_secret": "secret",
        "graph_sender": "geoid@fao.org",
    }
    base.update(overrides)
    return Settings(**base)


def _job(notify_email: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID("019e0000-0000-7000-8000-000000000001"),
        notify_email=notify_email,
        status="successful",
        message=None,
    )


def _report() -> IngestionReport:
    return IngestionReport(
        collection="public",
        batch_id="b",
        place_set_uri="http://testserver/place-sets/b",
        total=3,
        accepted_count=2,
        rejected_count=1,
    )


def test_get_notifier_defaults_to_null():
    get_notifier.cache_clear()
    try:
        assert isinstance(get_notifier(), NullNotifier)
    finally:
        get_notifier.cache_clear()


async def test_graph_notifier_suppress_send_skips_http():
    # graph_suppress_send builds the message but makes no HTTP call (no client needed).
    notifier = GraphNotifier(_settings(graph_suppress_send=True))
    await notifier.send_completion(
        _job("to@fao.org"), _report()
    )  # no transport -> would raise if it tried


async def test_graph_notifier_no_recipient_is_noop():
    notifier = GraphNotifier(_settings())
    await notifier.send_completion(_job(None), _report())  # no recipient -> no HTTP


async def test_graph_notifier_posts_sendmail_with_token():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2/v2.0/token" in url:
            calls.append("token")
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        if "sendMail" in url:
            calls.append("send")
            assert request.headers["Authorization"] == "Bearer tok-123"
            body = request.read().decode()
            assert "geoid bulk ingest successful" in body.lower()
            assert "to@fao.org" in body
            return httpx.Response(202)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = GraphNotifier(_settings(), client=client)
    await notifier.send_completion(_job("to@fao.org"), _report())
    await client.aclose()
    assert calls == ["token", "send"]


async def test_graph_notifier_retries_then_gives_up_without_raising(monkeypatch):
    import geoid.notify.graph as graph_mod

    monkeypatch.setattr(graph_mod.asyncio, "sleep", lambda *_: _noop())
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        attempts["n"] += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = GraphNotifier(_settings(), client=client)
    # A persistent 5xx must NOT raise into the worker (at-most-once already claimed).
    await notifier.send_completion(_job("to@fao.org"), _report())
    await client.aclose()
    assert attempts["n"] == 3  # 3 tries then give up


async def test_post_callback_posts_report_to_success_uri():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sent = await post_callback(
        {"successUri": "https://cb.example/ok"},
        report={"accepted_count": 2},
        status="successful",
        client=client,
    )
    await client.aclose()
    assert sent is True
    assert seen["url"] == "https://cb.example/ok"
    assert "accepted_count" in seen["body"]


async def test_post_callback_no_subscriber_is_noop():
    assert await post_callback(None, report={}, status="successful") is False


async def _noop():
    return None
