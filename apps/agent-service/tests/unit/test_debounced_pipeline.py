"""DebouncedPipeline 基类测试

用一个最小化的 ConcreteTestPipeline 验证 buffer/timer/phase2 状态机行为。
"""

import asyncio

import pytest

from app.services.debounced_pipeline import DebouncedPipeline


# ---------------------------------------------------------------------------
# Concrete test implementation
# ---------------------------------------------------------------------------


class ConcreteTestPipeline(DebouncedPipeline):
    """测试用具体实现：记录 process() 被调用的参数"""

    def __init__(self, debounce_seconds: float, max_buffer: int):
        super().__init__(debounce_seconds=debounce_seconds, max_buffer=max_buffer)
        self.process_calls: list[tuple[str, str, int]] = []
        self._process_started = asyncio.Event()
        self._process_gate = asyncio.Event()
        self._process_gate.set()  # 默认不阻塞

    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        self._process_started.set()
        await self._process_gate.wait()
        self.process_calls.append((chat_id, persona_id, event_count))


# ---------------------------------------------------------------------------
# Test 1: debounce 超时后触发 process
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_triggers_after_timeout():
    """2 个事件，等 debounce 超时，verify process 只被调用一次，count=2"""
    pipe = ConcreteTestPipeline(debounce_seconds=0.05, max_buffer=100)

    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_1", "akao")

    # 等 debounce 触发 + 执行完成
    await asyncio.sleep(0.15)

    assert len(pipe.process_calls) == 1
    chat_id, persona_id, count = pipe.process_calls[0]
    assert chat_id == "chat_1"
    assert persona_id == "akao"
    assert count == 2


# ---------------------------------------------------------------------------
# Test 2: max_buffer 达到阈值立即 flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_buffer_forces_flush():
    """3 个事件，max_buffer=3，不等 debounce 直接 flush"""
    pipe = ConcreteTestPipeline(debounce_seconds=10, max_buffer=3)

    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_1", "akao")

    # flush 是 create_task，给它调度时间
    await asyncio.sleep(0.05)

    assert len(pipe.process_calls) == 1
    _, _, count = pipe.process_calls[0]
    assert count == 3


# ---------------------------------------------------------------------------
# Test 3: phase2 执行期间新事件被缓冲，完成后自动触发下一轮
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_during_phase2_buffered():
    """phase2 运行期间收到的事件被缓冲，phase2 完成后触发新一轮"""
    pipe = ConcreteTestPipeline(debounce_seconds=0.05, max_buffer=100)
    pipe._process_gate.clear()  # 阻塞 process，模拟慢处理

    await pipe.on_event("chat_1", "akao")

    # 等 debounce 超时，phase2 启动（但被 gate 阻塞）
    await asyncio.sleep(0.1)
    assert pipe._process_started.is_set()

    # phase2 运行中，发送新事件
    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_1", "akao")

    # 事件已缓冲
    key = "chat_1:akao"
    assert pipe._buffers.get(key, 0) == 2

    # 释放 process，让 phase2 完成
    pipe._process_started.clear()
    pipe._process_gate.set()

    # 等下一轮 debounce + 执行
    await asyncio.sleep(0.3)

    # 应该被调用 2 次：第一轮 count=1
    # 第二轮：buffer 中有 2 条，re-trigger 调 on_event 又 +1 = 3
    assert len(pipe.process_calls) == 2
    assert pipe.process_calls[0][2] == 1
    assert pipe.process_calls[1][2] == 3


# ---------------------------------------------------------------------------
# Test 4: 不同 key 完全独立
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_separate_keys_independent():
    """不同 (chat_id, persona_id) 组合独立管理"""
    pipe = ConcreteTestPipeline(debounce_seconds=0.05, max_buffer=100)

    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_2", "bkao")

    # 等 debounce 触发
    await asyncio.sleep(0.15)

    assert len(pipe.process_calls) == 2
    keys = {(c, p) for c, p, _ in pipe.process_calls}
    assert ("chat_1", "akao") in keys
    assert ("chat_2", "bkao") in keys
    # 每个 key 的 count 都是 1
    for _, _, count in pipe.process_calls:
        assert count == 1


# ---------------------------------------------------------------------------
# Test 5: debounce 重置 — 连续事件只触发一次
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_resets_on_new_event():
    """在 debounce 窗口内持续发事件，只在最后一个事件的 debounce 到期后触发一次"""
    pipe = ConcreteTestPipeline(debounce_seconds=0.1, max_buffer=100)

    await pipe.on_event("chat_1", "akao")
    await asyncio.sleep(0.03)
    await pipe.on_event("chat_1", "akao")
    await asyncio.sleep(0.03)
    await pipe.on_event("chat_1", "akao")

    # 还没到最后一个事件的 debounce
    await asyncio.sleep(0.05)
    assert len(pipe.process_calls) == 0

    # 等最后一个事件的 debounce 到期
    await asyncio.sleep(0.1)
    assert len(pipe.process_calls) == 1
    assert pipe.process_calls[0][2] == 3


# ---------------------------------------------------------------------------
# Test 6: process 异常后状态正确清理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_error_cleans_up():
    """process() 抛异常后 phase2_running 应被清理"""

    class FailingPipeline(DebouncedPipeline):
        async def process(self, chat_id, persona_id, event_count):
            raise RuntimeError("boom")

    pipe = FailingPipeline(debounce_seconds=0.05, max_buffer=100)
    await pipe.on_event("chat_1", "akao")

    await asyncio.sleep(0.15)

    key = "chat_1:akao"
    assert key not in pipe._phase2_running
