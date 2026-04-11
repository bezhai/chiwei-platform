"""safety_race 模块测试

测试 buffer_until_pre 的 race 逻辑：
- pre 通过 → flush buffer + 透传后续 token
- pre 拦截 → 丢弃 buffer，yield guard_message
- pre 异常 → flush buffer
- stream 先结束再等 pre
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

from app.agents.domains.main.safety_race import buffer_until_pre


async def _fake_stream(*tokens: str) -> AsyncGenerator[str, None]:
    """生成一个假的 token 流"""
    for t in tokens:
        yield t


async def _delayed_stream(*tokens: str, delay: float = 0.05) -> AsyncGenerator[str, None]:
    """生成一个带延迟的假 token 流"""
    for t in tokens:
        await asyncio.sleep(delay)
        yield t


def _make_pre_task(is_blocked: bool, block_reason: str = "", delay: float = 0) -> asyncio.Task:
    """创建一个模拟的 pre_task"""
    async def _pre():
        if delay:
            await asyncio.sleep(delay)
        return {"is_blocked": is_blocked, "block_reason": block_reason}
    return asyncio.create_task(_pre())


@pytest.mark.asyncio
async def test_pre_passes_flush_buffer():
    """pre 通过时，缓冲的 token 应该被 flush 并透传后续 token"""
    stream = _delayed_stream("a", "b", "c", delay=0.02)
    pre_task = _make_pre_task(is_blocked=False, delay=0.05)

    result = []
    async for text in buffer_until_pre(stream, pre_task, "msg-1"):
        result.append(text)

    assert result == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_pre_blocks_yield_guard():
    """pre 拦截时，应该丢弃 buffer 并 yield guard_message"""
    stream = _delayed_stream("a", "b", "c", delay=0.02)
    pre_task = _make_pre_task(is_blocked=True, block_reason="nsfw", delay=0.05)

    result = []
    async for text in buffer_until_pre(stream, pre_task, "msg-2", guard_message="blocked!"):
        result.append(text)

    assert result == ["blocked!"]


@pytest.mark.asyncio
async def test_pre_passes_immediately():
    """pre 立即通过（比 stream 快），所有 token 应该直接透传"""
    stream = _delayed_stream("x", "y", delay=0.05)
    pre_task = _make_pre_task(is_blocked=False, delay=0)

    result = []
    async for text in buffer_until_pre(stream, pre_task, "msg-3"):
        result.append(text)

    assert result == ["x", "y"]


@pytest.mark.asyncio
async def test_stream_ends_before_pre_passes():
    """stream 先结束，然后 pre 通过 → flush buffer"""
    stream = _fake_stream("hello")
    pre_task = _make_pre_task(is_blocked=False, delay=0.1)

    result = []
    async for text in buffer_until_pre(stream, pre_task, "msg-4"):
        result.append(text)

    assert result == ["hello"]


@pytest.mark.asyncio
async def test_stream_ends_before_pre_blocks():
    """stream 先结束，然后 pre 拦截 → yield guard_message"""
    stream = _fake_stream("hello")
    pre_task = _make_pre_task(is_blocked=True, block_reason="harmful", delay=0.1)

    result = []
    async for text in buffer_until_pre(stream, pre_task, "msg-5", guard_message="nope"):
        result.append(text)

    assert result == ["nope"]


@pytest.mark.asyncio
async def test_empty_stream_pre_passes():
    """空 stream + pre 通过 → 无输出"""
    stream = _fake_stream()
    pre_task = _make_pre_task(is_blocked=False, delay=0.01)

    result = []
    async for text in buffer_until_pre(stream, pre_task, "msg-6"):
        result.append(text)

    assert result == []


@pytest.mark.asyncio
async def test_pre_exception_flush_buffer():
    """pre 任务异常时，应该 flush buffer（不崩溃）"""
    stream = _fake_stream("a", "b")

    async def _failing_pre():
        await asyncio.sleep(0.1)
        raise ValueError("pre failed")

    pre_task = asyncio.create_task(_failing_pre())

    result = []
    async for text in buffer_until_pre(stream, pre_task, "msg-7"):
        result.append(text)

    # stream 先结束，await pre_task 抛异常 → except 分支 flush buffer
    assert result == ["a", "b"]
