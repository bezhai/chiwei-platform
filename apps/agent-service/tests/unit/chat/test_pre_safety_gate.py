"""Tests for chat/pre_safety_gate.py (Phase 2)."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.domain.safety import PreSafetyVerdict


@pytest.mark.asyncio
async def test_register_creates_future_and_resolve_sets_result():
    from app.chat import pre_safety_gate

    fut = pre_safety_gate.register("pr-1")
    assert isinstance(fut, asyncio.Future)
    assert not fut.done()

    verdict = PreSafetyVerdict(
        pre_request_id="pr-1", message_id="m-1", is_blocked=False
    )
    pre_safety_gate.resolve(verdict)
    assert fut.done()
    assert fut.result() is verdict
    pre_safety_gate.cleanup("pr-1")


@pytest.mark.asyncio
async def test_resolve_for_unknown_request_id_is_noop():
    """resolve 对未 register 的 id 不抛异常."""
    from app.chat import pre_safety_gate

    verdict = PreSafetyVerdict(
        pre_request_id="ghost", message_id="m-1", is_blocked=False
    )
    pre_safety_gate.resolve(verdict)  # 不抛


@pytest.mark.asyncio
async def test_resolve_for_already_done_future_is_noop():
    """resolve 对已 done 的 future 不抛 InvalidStateError."""
    from app.chat import pre_safety_gate

    fut = pre_safety_gate.register("pr-2")
    fut.cancel()
    await asyncio.sleep(0)

    verdict = PreSafetyVerdict(
        pre_request_id="pr-2", message_id="m-1", is_blocked=False
    )
    pre_safety_gate.resolve(verdict)  # 不抛
    pre_safety_gate.cleanup("pr-2")


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_returns_verdict_on_normal_completion():
    """正常路径：emit 触发节点链路 → verdict 出现 → 返回 verdict."""
    from app.chat import pre_safety_gate

    captured_pre_request_id: list[str] = []

    async def fake_emit(req):
        captured_pre_request_id.append(req.pre_request_id)
        # 模拟 graph 链路完成：直接 resolve
        verdict = PreSafetyVerdict(
            pre_request_id=req.pre_request_id,
            message_id=req.message_id,
            is_blocked=False,
        )
        pre_safety_gate.resolve(verdict)

    with patch("app.chat.pre_safety_gate.emit", fake_emit):
        v = await pre_safety_gate.run_pre_safety_via_graph(
            message_id="m-1", content="hi", persona_id="ayana"
        )

    assert isinstance(v, PreSafetyVerdict)
    assert v.is_blocked is False
    assert v.message_id == "m-1"
    assert captured_pre_request_id  # 用了一个 uuid4 pre_request_id


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_fails_open_on_timeout():
    """节点卡住 21s+ → fail-open + emit_task 被 cancel."""
    from app.chat import pre_safety_gate

    cancelled = asyncio.Event()

    async def fake_emit(req):
        try:
            await asyncio.sleep(60)  # 卡住
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with (
        patch("app.chat.pre_safety_gate.emit", fake_emit),
        patch("app.chat.pre_safety_gate._PRE_SAFETY_TIMEOUT_SECONDS", 0.05),
    ):
        v = await pre_safety_gate.run_pre_safety_via_graph(
            message_id="m-1", content="hi", persona_id="ayana"
        )

    assert v.is_blocked is False  # fail-open
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_fails_open_on_emit_error():
    """emit 自身抛异常 → fail-open 立即返回，不等满 timeout."""
    from app.chat import pre_safety_gate

    async def fake_emit(req):
        raise RuntimeError("mq not connected")

    with patch("app.chat.pre_safety_gate.emit", fake_emit):
        v = await pre_safety_gate.run_pre_safety_via_graph(
            message_id="m-1", content="hi", persona_id="ayana"
        )

    assert v.is_blocked is False


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_caller_cancel_cancels_emit():
    """外层调用方 cancel → emit_task 也被 cancel + waiter cleanup（reviewer round 6）."""
    from app.chat import pre_safety_gate

    emit_started = asyncio.Event()
    emit_cancelled = asyncio.Event()

    async def fake_emit(req):
        emit_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            emit_cancelled.set()
            raise

    async def caller():
        with patch("app.chat.pre_safety_gate.emit", fake_emit):
            await pre_safety_gate.run_pre_safety_via_graph(
                message_id="m-1", content="hi", persona_id="ayana"
            )

    task = asyncio.create_task(caller())
    await emit_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert emit_cancelled.is_set()


@pytest.mark.asyncio
async def test_run_pre_safety_via_graph_concurrent_uses_independent_ids():
    """并发多次调用每次独立 pre_request_id；不串."""
    from app.chat import pre_safety_gate

    seen_ids: list[str] = []

    async def fake_emit(req):
        seen_ids.append(req.pre_request_id)
        verdict = PreSafetyVerdict(
            pre_request_id=req.pre_request_id,
            message_id=req.message_id,
            is_blocked=False,
        )
        pre_safety_gate.resolve(verdict)

    with patch("app.chat.pre_safety_gate.emit", fake_emit):
        results = await asyncio.gather(*[
            pre_safety_gate.run_pre_safety_via_graph(
                message_id=f"m-{i}", content="hi", persona_id="ayana"
            )
            for i in range(5)
        ])

    assert len(results) == 5
    assert all(v.is_blocked is False for v in results)
    assert len(set(seen_ids)) == 5  # 都是不同 uuid
