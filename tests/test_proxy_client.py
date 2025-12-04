import json
import time

import httpx
import pytest

from app.services.proxy_client import ProxyConfig, ProxyHttpClient


def test_requires_base_url():
    with pytest.raises(ValueError):
        ProxyHttpClient(
            ProxyConfig(
                base_url="",
                token=None,
                timeout_seconds=5,
                max_retries=1,
                rps_limit=None,
                default_headers={},
            )
        )


def test_success_with_token_and_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token123"
        body = json.loads(request.content)
        assert body["url"] == "http://example.com/target"
        assert body["method"] == "GET"
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = ProxyHttpClient(
        ProxyConfig(
            base_url="https://proxy.test",
            token="token123",
            timeout_seconds=5,
            max_retries=1,
            rps_limit=None,
            default_headers={},
        ),
        transport=transport,
    )

    response = client.request("http://example.com/target")
    assert response.json() == {"ok": True}


def test_retries_on_failure_and_respects_rps_limit(monkeypatch):
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(502, json={"error": "bad gateway"})
        return httpx.Response(200, json={"ok": True})

    # speed up sleep to avoid slow tests
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    transport = httpx.MockTransport(handler)
    client = ProxyHttpClient(
        ProxyConfig(
            base_url="https://proxy.test",
            token=None,
            timeout_seconds=5,
            max_retries=2,
            rps_limit=1000.0,  # effectively no delay
            default_headers={},
        ),
        transport=transport,
    )

    response = client.request("http://example.com/target")
    assert attempts["count"] == 2
    assert response.status_code == 200


def test_passthrough_proxy_mode(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://example.com/target")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    client = ProxyHttpClient(
        ProxyConfig(
            base_url="http://user:pass@proxy:3128",
            token=None,
            timeout_seconds=5,
            max_retries=1,
            rps_limit=None,
            default_headers={"User-Agent": "UA"},
        ),
        transport=transport,
    )
    resp = client.request("http://example.com/target")
    assert resp.text == "ok"
