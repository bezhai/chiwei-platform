"""Tests for capabilities.sandbox (Phase 7d Gap 16).

Stubs the underlying ``HTTPClient._client`` with an ``httpx.MockTransport``
to verify both happy-path payload shape and the no-retry contract for the
non-idempotent ``/exec`` endpoint.

出量上限也钉在这一层：沙箱那侧一个字都不截（``stdout_bytes.decode(...)`` 原样回），
而这个 capability 是两条路（她自己跑一条命令、说明里那条预处理指令的结果被替换进正
文）唯一都要经过的地方。裁在这里，谁也漏不掉。
"""

from __future__ import annotations

import httpx
import pytest

from app.capabilities import sandbox
from app.capabilities.sandbox import (
    OUTPUT_CUT_MARK,
    OUTPUT_MAX_CHARS,
    SandboxResult,
    run,
)


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


def _wire(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", stderr: str = "") -> None:
    """把这一跳接到一个只会回这份输出的假 sandbox-worker 上。"""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"exit_code": 0, "stdout": stdout, "stderr": stderr}
        )

    monkeypatch.setattr(
        "app.capabilities.http.lane_router.base_url",
        lambda _svc: "http://sandbox-worker:8080",
    )
    monkeypatch.setattr("app.capabilities.http.lane_router.get_headers", lambda: {})
    _patch_transport(handler)


@pytest.mark.asyncio
async def test_a_flood_of_output_is_cut_before_it_can_reach_her(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一条 ``print('x'*10**8)`` 就能把她那一轮冲掉 —— 沙箱那侧不截，这一层必须截。"""
    _wire(monkeypatch, stdout="x" * 100_000)

    result = await run(command="python3 -c \"print('x'*100000)\"")

    assert len(result.stdout) < 100_000, "整坨原样交出去了"
    assert result.stdout.startswith("x" * OUTPUT_MAX_CHARS)
    assert result.dropped == 100_000 - OUTPUT_MAX_CHARS


@pytest.mark.asyncio
async def test_what_was_cut_is_said_out_loud_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """静默截断更糟：她会把戛然而止的那一段当成全部，拿一个不完整的结果往下做事。"""
    _wire(monkeypatch, stdout="x" * 100_000)

    result = await run(command="whatever")

    assert OUTPUT_CUT_MARK in result.stdout
    assert str(100_000 - OUTPUT_MAX_CHARS) in result.stdout, "没告诉她截掉了多少"


@pytest.mark.asyncio
async def test_a_flood_on_the_error_side_is_cut_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报错那一路同样没有上限 —— 一个刷屏的 traceback 效果一模一样。"""
    _wire(monkeypatch, stdout="", stderr="e" * 50_000)

    result = await run(command="whatever")

    assert len(result.stderr) < 50_000
    assert OUTPUT_CUT_MARK in result.stderr
    assert result.dropped == 50_000 - OUTPUT_MAX_CHARS


@pytest.mark.asyncio
async def test_output_that_fits_comes_back_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """裁的是刷屏那种，不是每一条都加一句废话。"""
    _wire(monkeypatch, stdout="hello\n", stderr="")

    result = await run(command="echo hello")

    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.dropped == 0


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
