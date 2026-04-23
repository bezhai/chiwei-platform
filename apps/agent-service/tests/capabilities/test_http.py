from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.api.middleware import trace_id_var
from app.capabilities.http import HTTPClient


@pytest.mark.asyncio
async def test_absolute_url_bypasses_base_url():
    client = HTTPClient(service="sandbox-worker")
    fake_get = AsyncMock(return_value=MagicMock())
    with patch.object(client._client, "get", fake_get):
        with patch(
            "app.capabilities.http.lane_router"
        ) as mrouter:
            mrouter.get_headers.return_value = {}
            mrouter.base_url = MagicMock(
                side_effect=AssertionError("base_url must not be called for absolute URL")
            )
            await client.get("https://example.com/api/foo")

    assert fake_get.await_count == 1
    called_url = fake_get.await_args.args[0]
    assert called_url == "https://example.com/api/foo"


@pytest.mark.asyncio
async def test_service_path_prepends_base_url():
    client = HTTPClient(service="sandbox-worker")
    fake_get = AsyncMock(return_value=MagicMock())
    with patch.object(client._client, "get", fake_get):
        with patch("app.capabilities.http.lane_router") as mrouter:
            mrouter.get_headers.return_value = {}
            mrouter.base_url.return_value = "http://sandbox-worker-dev:8080"
            await client.get("/api/foo")

    mrouter.base_url.assert_called_once_with("sandbox-worker")
    called_url = fake_get.await_args.args[0]
    assert called_url == "http://sandbox-worker-dev:8080/api/foo"


@pytest.mark.asyncio
async def test_trace_id_header_from_contextvar():
    token = trace_id_var.set("trace-abc")
    try:
        client = HTTPClient()
        fake_post = AsyncMock(return_value=MagicMock())
        with patch.object(client._client, "post", fake_post):
            with patch("app.capabilities.http.lane_router") as mrouter:
                mrouter.get_headers.return_value = {}
                await client.post("https://example.com/x", json={"a": 1})
    finally:
        trace_id_var.reset(token)

    headers = fake_post.await_args.kwargs["headers"]
    assert headers.get("X-Trace-Id") == "trace-abc"


@pytest.mark.asyncio
async def test_framework_headers_win_over_caller_extra():
    # Even when caller passes X-Trace-Id in headers kwarg, the contextvar value wins.
    tok = trace_id_var.set("framework-tid")
    try:
        c = HTTPClient()
        sent: dict[str, Any] = {}

        async def fake_get(url, **kw):
            sent.update(kw)
            return httpx.Response(200)

        with patch.object(c._client, "get", new=fake_get):
            await c.get("https://example.com/", headers={"X-Trace-Id": "caller-attempt"})
        assert sent["headers"]["X-Trace-Id"] == "framework-tid"
    finally:
        trace_id_var.reset(tok)
        await c.close()


@pytest.mark.asyncio
async def test_lane_headers_merged_in():
    client = HTTPClient()
    fake_get = AsyncMock(return_value=MagicMock())
    with patch.object(client._client, "get", fake_get):
        with patch("app.capabilities.http.lane_router") as mrouter:
            mrouter.get_headers.return_value = {"x-ctx-lane": "dev"}
            await client.get(
                "https://example.com/x", headers={"X-Custom": "v"}
            )

    headers = fake_get.await_args.kwargs["headers"]
    assert headers["x-ctx-lane"] == "dev"
    assert headers["X-Custom"] == "v"
