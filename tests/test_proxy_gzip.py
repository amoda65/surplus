"""Regression tests for the streaming passthrough in surplus.proxy.

Pins the gzip bug: httpx offers ``accept-encoding: gzip`` upstream, so the
forwarded stream must arrive DECOMPRESSED (the response headers deliberately
drop ``content-encoding``), never as raw compressed bytes.
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest
from starlette.requests import Request

from surplus.proxy import build_client, forward


def make_request(body: bytes, method: str = "POST") -> Request:
    """Minimal starlette Request around a fully-buffered body."""

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": method,
        "path": "/v1/chat/completions",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer placeholder"),
            (b"accept-encoding", b"gzip"),
        ],
        "query_string": b"",
    }
    return Request(scope, receive)


async def collect(response) -> bytes:
    parts: list[bytes] = []
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        parts.append(chunk)
    return b"".join(parts)


async def test_gzip_upstream_arrives_decompressed() -> None:
    payload = {"choices": [{"message": {"content": "hello"}}]}
    compressed = gzip.compress(json.dumps(payload).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=compressed,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await forward(
            client,
            make_request(b'{"model": "m"}'),
            "https://upstream.example/v1/chat/completions",
            "real-key",
        )
        assert response.status_code == 200
        # content-encoding must not leak downstream (the router always sends
        # plain bytes), and the body must not be the compressed blob
        headers = {k.lower(): v for k, v in response.headers.items()}
        assert "content-encoding" not in headers
        body = await collect(response)
        assert body == json.dumps(payload).encode()
        assert body != compressed
        assert json.loads(body)["choices"][0]["message"]["content"] == "hello"
    finally:
        await client.aclose()


async def test_plain_upstream_passes_through() -> None:
    payload = b'{"ok": true}'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer real-key"
        return httpx.Response(200, content=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await forward(
            client, make_request(b"{}", method="POST"), "https://upstream.example/v1/x", "real-key"
        )
        assert await collect(response) == payload
    finally:
        await client.aclose()


def test_build_client_sets_proxy_and_ignores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://evil.example:1")
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    build_client(60.0, "http://127.0.0.1:15432")
    assert captured["proxy"] == "http://127.0.0.1:15432"
    assert captured["trust_env"] is False  # ambient env proxies never hijack the tunnel
    captured.clear()
    build_client(60.0)
    assert "proxy" not in captured
    assert "trust_env" not in captured  # legacy behavior preserved when direct
