# tests/unit/test_afterthought.py
"""测试 AfterthoughtManager 两阶段锁行为"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager():
    """创建一个干净的 AfterthoughtManager 实例（不走 singleton）"""
    from app.services.afterthought import AfterthoughtManager

    mgr = AfterthoughtManager()
    return mgr


# ---------------------------------------------------------------------------
# Test 1: on_event 启动 phase1 timer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_event_starts_phase1_timer():
    """首次 on_event 应创建一个 phase1 timer task"""
    mgr = _make_manager()

    with patch.object(mgr, "_phase1_timer", new_callable=AsyncMock) as mock_timer:
        # 让 mock_timer 永远 sleep（模拟等待 debounce）
        mock_timer.return_value = None

        await mgr.on_event("chat_1", "akao")

    key = "chat_1:akao"
    assert key in mgr._buffers
    assert mgr._buffers[key] == 1
    assert key in mgr._timers  # timer task 已创建


# ---------------------------------------------------------------------------
# Test 2: 多次 on_event 重置 debounce timer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_events_reset_debounce_timer():
    """连续多次 on_event 应取消旧 timer，创建新 timer"""
    mgr = _make_manager()
    key = "chat_1:akao"

    # 用真正的 sleep 创建 timer（会被取消）
    await mgr.on_event("chat_1", "akao")
    first_timer = mgr._timers.get(key)
    assert first_timer is not None

    await mgr.on_event("chat_1", "akao")
    second_timer = mgr._timers.get(key)

    # 让取消操作完成
    await asyncio.sleep(0.01)

    # 第一个 timer 被取消，新 timer 替换
    assert first_timer.cancelled()
    assert second_timer is not first_timer
    assert mgr._buffers[key] == 2

    # 清理
    if second_timer and not second_timer.done():
        second_timer.cancel()


# ---------------------------------------------------------------------------
# Test 3: buffer 超过 MAX_BUFFER 强制进入 phase2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buffer_exceeding_max_forces_phase2():
    """buffer 达到 MAX_BUFFER 时应跳过 timer 直接触发 phase2"""
    mgr = _make_manager()
    mgr._max_buffer = 3  # 降低阈值方便测试
    key = "chat_1:akao"

    phase2_called = asyncio.Event()

    async def mock_enter_phase2(chat_id, persona_id):
        phase2_called.set()

    with patch.object(mgr, "_enter_phase2", side_effect=mock_enter_phase2):
        await mgr.on_event("chat_1", "akao")  # buffer=1
        await mgr.on_event("chat_1", "akao")  # buffer=2
        # 取消 timer 以免干扰
        if key in mgr._timers:
            mgr._timers[key].cancel()

        await mgr.on_event("chat_1", "akao")  # buffer=3 >= MAX_BUFFER

    # 让 create_task 有机会执行
    await asyncio.sleep(0.01)
    assert phase2_called.is_set()


# ---------------------------------------------------------------------------
# Test 4: phase2 运行中阻止新 phase2，事件仅缓冲
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_running_blocks_new_phase2():
    """phase2 运行期间新的 on_event 应只缓冲不启动新 timer 或 phase2"""
    mgr = _make_manager()
    key = "chat_1:akao"

    # 模拟 phase2 正在运行
    mgr._phase2_running.add(key)

    await mgr.on_event("chat_1", "akao")
    await mgr.on_event("chat_1", "akao")

    # 事件已缓冲
    assert mgr._buffers[key] == 2
    # 但没有 timer（因为 phase2 运行中直接 return）
    assert key not in mgr._timers


# ---------------------------------------------------------------------------
# Test 5: phase2 调用 _generate_conversation_fragment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_calls_generate_fragment():
    """_enter_phase2 应调用 _generate_conversation_fragment"""
    mgr = _make_manager()
    key = "chat_1:akao"
    mgr._buffers[key] = 5

    with patch(
        "app.services.afterthought._generate_conversation_fragment",
        new_callable=AsyncMock,
    ) as mock_gen:
        await mgr._enter_phase2("chat_1", "akao")

    mock_gen.assert_awaited_once_with("chat_1", "akao")
    # phase2 完成后，key 不在 running set 中
    assert key not in mgr._phase2_running


# ---------------------------------------------------------------------------
# Test 6: phase2 完成后若有新事件则启动下一轮
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_triggers_next_cycle_if_buffer_has_events():
    """phase2 完成后如果 buffer 中还有事件，应触发新的 on_event"""
    mgr = _make_manager()
    key = "chat_1:akao"
    mgr._buffers[key] = 3  # 触发 phase2 的事件

    on_event_called = asyncio.Event()

    async def track_on_event(chat_id, persona_id):
        on_event_called.set()

    with patch(
        "app.services.afterthought._generate_conversation_fragment",
        new_callable=AsyncMock,
    ):
        # 在 phase2 执行期间添加新事件
        async def inject_events_during_phase2(chat_id, persona_id):
            mgr._buffers[key] = 2  # 模拟 phase2 期间积累的事件

        with patch(
            "app.services.afterthought._generate_conversation_fragment",
            new_callable=AsyncMock,
            side_effect=inject_events_during_phase2,
        ):
            with patch.object(mgr, "on_event", side_effect=track_on_event):
                await mgr._enter_phase2("chat_1", "akao")
                # 让 create_task 有机会执行
                await asyncio.sleep(0.01)

    assert on_event_called.is_set()


# ---------------------------------------------------------------------------
# Test 7: phase2 异常不会阻塞后续事件
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_error_cleans_up():
    """_generate_conversation_fragment 异常时，phase2 应正确清理状态"""
    mgr = _make_manager()
    key = "chat_1:akao"
    mgr._buffers[key] = 3

    with patch(
        "app.services.afterthought._generate_conversation_fragment",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM down"),
    ):
        await mgr._enter_phase2("chat_1", "akao")

    # 状态已清理
    assert key not in mgr._phase2_running
    assert key not in mgr._buffers
