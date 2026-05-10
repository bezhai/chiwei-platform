"""Tests for HTTPClient method-aware retry (Phase 7d Gap 16).

Uses ``httpx.MockTransport`` to stub responses/exceptions per attempt rather
than monkey-patching the client; this keeps the retry control flow under
real httpx semantics (status_code, exception types) without an extra dep.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.capabilities.http import HTTPClient


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    retries: int = 3,
    retry_post: int = 0,
) -> HTTPClient:
    """Build an HTTPClient whose internal AsyncClient uses MockTransport."""
    client = HTTPClient(retries=retries, retry_post=retry_post, retry_backoff=0.0)
    # Replace the internal AsyncClient so request flow goes through mock transport.
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_get_retries_on_5xx() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler)
    try:
        resp = await client.get("https://x/y")
        assert resp.status_code == 200
        assert calls["n"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_does_not_retry_on_5xx() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    client = _make_client(handler, retry_post=3)
    try:
        # No idempotency_key → retries forced to 0 for POST.
        resp = await client.post("https://x/y", json={})
        assert resp.status_code == 503
        assert calls["n"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_with_idempotency_key_still_skips_5xx_retry() -> None:
    """Even with idempotency_key, POST does not retry 5xx (only connect + 429)."""
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502)

    client = _make_client(handler, retry_post=3)
    try:
        resp = await client.post("https://x/y", idempotency_key="k1", json={})
        assert resp.status_code == 502
        assert calls["n"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_with_idempotency_key_retries_429() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Sanity: idempotency-key header is forwarded.
        assert req.headers.get("Idempotency-Key") == "k1"
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler, retry_post=3)
    try:
        resp = await client.post("https://x/y", idempotency_key="k1", json={})
        assert resp.status_code == 200
        assert calls["n"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_retries_on_read_timeout() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler)
    try:
        resp = await client.get("https://x/y")
        assert resp.status_code == 200
        assert calls["n"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_does_not_retry_read_timeout() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timeout")

    client = _make_client(handler, retry_post=3)
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.post("https://x/y", idempotency_key="k1", json={})
        assert calls["n"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_connect_error_retries_for_post() -> None:
    """ConnectError = request never reached server, safe to retry POST."""
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("dns")
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler, retry_post=3)
    try:
        resp = await client.post("https://x/y", idempotency_key="k1", json={})
        assert resp.status_code == 200
        assert calls["n"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_retries_on_429() -> None:
    """429 always retries — verifies the GET-side status retry set."""
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler)
    try:
        resp = await client.get("https://x/y")
        assert resp.status_code == 200
        assert calls["n"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_gives_up_after_max_retries() -> None:
    """After exhausting retries, the last response is returned (not raised)."""
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    client = _make_client(handler, retries=2)
    try:
        resp = await client.get("https://x/y")
        assert resp.status_code == 503
        # 1 initial + 2 retries = 3 attempts.
        assert calls["n"] == 3
    finally:
        await client.close()
