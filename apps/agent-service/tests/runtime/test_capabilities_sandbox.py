"""Tests for capabilities.sandbox (Phase 7d Gap 16).

Stubs the underlying ``HTTPClient._client`` with an ``httpx.MockTransport``
to verify both happy-path payload shape and the no-retry contract for the
non-idempotent ``/exec`` endpoint.
"""

from __future__ import annotations

import httpx
import pytest

from app.capabilities import sandbox
from app.capabilities.sandbox import SandboxResult, run


def _patch_transport(handler):
    """Swap the module-level sandbox client to a MockTransport-backed AsyncClient."""
    sandbox._CLIENT._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_run_returns_structured_result(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["body"] = req.content.decode()
        return httpx.Response(
            200, json={"exit_code": 0, "stdout": "hello\n", "stderr": ""}
        )

    # lane_router.base_url("sandbox-worker") is hit through HTTPClient._url.
    # Patch it to a stable host so the assertion below is deterministic.
    monkeypatch.setattr(
        "app.capabilities.http.lane_router.base_url",
        lambda _svc: "http://sandbox-worker:8080",
    )
    monkeypatch.setattr(
        "app.capabilities.http.lane_router.get_headers", lambda: {}
    )
    _patch_transport(handler)

    result = await run(command="echo hello")
    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert captured["method"] == "POST"
    assert captured["url"] == "http://sandbox-worker:8080/exec"
    assert "echo hello" in str(captured["body"])
    assert "timeout_sec" in str(captured["body"])


@pytest.mark.asyncio
async def test_run_does_not_retry_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    monkeypatch.setattr(
        "app.capabilities.http.lane_router.base_url",
        lambda _svc: "http://sandbox-worker:8080",
    )
    monkeypatch.setattr(
        "app.capabilities.http.lane_router.get_headers", lambda: {}
    )
    _patch_transport(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await run(command="anything")
    # Sandbox is non-idempotent; retries=0 → exactly one attempt.
    assert calls["n"] == 1
