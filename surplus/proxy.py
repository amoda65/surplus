"""Streaming passthrough to the upstream marketplace.

The proxy replaces the caller's Authorization header with the configured key, so
agents can send any placeholder. Nothing here logs headers or bodies.
"""

from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

# Hop-by-hop headers (RFC 9110) plus identity headers the proxy must own.
STRIP_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
    "accept-encoding",
    "authorization",
}

STRIP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "content-encoding",
}


def build_client(timeout_seconds: float) -> httpx.AsyncClient:
    """Long read timeout: streaming completions can run for minutes."""
    timeout = httpx.Timeout(
        connect=15.0,
        read=timeout_seconds,
        write=60.0,
        pool=30.0,
    )
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, http2=False)


def upstream_headers(request: Request, api_key: str) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in STRIP_REQUEST_HEADERS
    }
    headers["authorization"] = f"Bearer {api_key}"
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in STRIP_RESPONSE_HEADERS
    }


async def forward(
    client: httpx.AsyncClient, request: Request, url: str, api_key: str
) -> Response:
    """Forward one request upstream, streaming the body back unbuffered."""
    body = await request.body()
    headers = upstream_headers(request, api_key)

    upstream_request = client.build_request(
        request.method, url, headers=headers, content=body or None
    )
    upstream = await client.send(upstream_request, stream=True)

    async def body_iterator():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iterator(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )
