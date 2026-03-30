# Identity 漂移状态机 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现两阶段锁的 identity 漂移状态机，让赤尾的性格/情绪随对话内容异步漂移，替代 main prompt 中的静态 identity。

**Architecture:** 新增 `IdentityDriftManager` 单例，管理每个 chat 的两阶段锁（debounce 收集 + 不可中断 LLM 计算）。状态存 Redis，漂移结果注入 `build_inner_context()`。触发点是赤尾回复后的 `asyncio.create_task()`（复用现有 post-processing 模式）。

**Tech Stack:** Python 3.12, asyncio (timer/lock), Redis (状态存储+分布式锁), Langfuse (漂移 prompt), LangChain (LLM 调用)

---

## File Structure

| 文件 | 操作 | 职责 |
|------|------|------|
| `app/services/identity_drift.py` | 新增 | IdentityDriftManager：两阶段锁、缓冲区、LLM 漂移调用、Redis 读写 |
| `app/config/config.py` | 修改 | 新增漂移参数（debounce 秒数、buffer 阈值、漂移模型） |
| `app/services/memory_context.py` | 修改 | `build_inner_context()` 注入漂移状态 |
| `app/agents/domains/main/agent.py` | 修改 | 赤尾回复后触发 `drift_manager.on_event()` |
| `tests/unit/test_identity_drift.py` | 新增 | 漂移管理器单元测试 |
| `tests/unit/test_memory_context.py` | 修改 | 漂移注入测试 |

---

### Task 1: Config — 漂移参数

**Files:**
- Modify: `apps/agent-service/app/config/config.py:4-63`

- [ ] **Step 1: 添加漂移配置字段**

在 `Settings` class 中追加（在现有字段之后）：

```python
# Identity drift
identity_drift_model: str = "diary-model"  # 漂移 LLM 模型
identity_drift_debounce_seconds: int = 300  # 一阶段等待: 5 分钟
identity_drift_max_buffer: int = 20  # 强制 flush 阈值
identity_drift_session_gap_seconds: int = 600  # 会话边界: 10 分钟
identity_drift_ttl_seconds: int = 86400  # Redis TTL: 24 小时
```

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/config/config.py
git commit -m "feat(config): add identity drift parameters"
```

---

### Task 2: Redis 状态存储

**Files:**
- Create: `apps/agent-service/app/services/identity_drift.py`
- Test: `apps/agent-service/tests/unit/test_identity_drift.py`

- [ ] **Step 1: 写测试 — Redis 读写**

创建 `apps/agent-service/tests/unit/test_identity_drift.py`：

```python
"""Identity 漂移状态机测试"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


@pytest.mark.asyncio
async def test_get_identity_state_returns_none_when_empty():
    """无状态时返回 None"""
    mock_redis = AsyncMock()
    mock_redis.hget = AsyncMock(return_value=None)

    with patch("app.services.identity_drift.AsyncRedisClient") as mock_cls:
        mock_cls.get_instance.return_value = mock_redis
        from app.services.identity_drift import get_identity_state

        result = await get_identity_state("chat_001")

    assert result is None
    mock_redis.hget.assert_called_once_with("identity:chat_001", "state")


@pytest.mark.asyncio
async def test_set_and_get_identity_state():
    """写入后能读回"""
    store = {}

    async def fake_hset(key, mapping):
        store[key] = mapping

    async def fake_hget(key, field):
        return store.get(key, {}).get(field)

    async def fake_expire(key, ttl):
        pass

    mock_redis = AsyncMock()
    mock_redis.hset = fake_hset
    mock_redis.hget = fake_hget
    mock_redis.expire = fake_expire
    mock_pipe = MagicMock()
    mock_pipe.hset = MagicMock()
    mock_pipe.expire = MagicMock()
    mock_pipe.execute = AsyncMock(side_effect=lambda: [
        fake_hset("identity:chat_001", {"state": "有点困", "updated_at": "2026-03-28T15:00:00"}),
        None,
    ])
    mock_redis.pipeline.return_value = mock_pipe

    with patch("app.services.identity_drift.AsyncRedisClient") as mock_cls:
        mock_cls.get_instance.return_value = mock_redis
        from app.services.identity_drift import set_identity_state, get_identity_state

        await set_identity_state("chat_001", "有点困")

    mock_pipe.hset.assert_called_once()
    mock_pipe.expire.assert_called_once()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py -v -x 2>&1 | tail -20
```

预期：FAIL — `app.services.identity_drift` 不存在。

- [ ] **Step 3: 实现 Redis 读写函数**

创建 `apps/agent-service/app/services/identity_drift.py`：

```python
"""赤尾 Identity 漂移状态机

两阶段锁模型：
  一阶段（可中断）：收集消息，debounce N 秒，超过 M 条强制 flush
  二阶段（不可中断）：LLM 漂移计算，更新 identity 状态

每个群/私聊维护独立的漂移锁。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.clients.redis import AsyncRedisClient
from app.config.config import settings

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# Redis key 前缀
_KEY_PREFIX = "identity"


def _state_key(chat_id: str) -> str:
    return f"{_KEY_PREFIX}:{chat_id}"


async def get_identity_state(chat_id: str) -> str | None:
    """从 Redis 读取当前 identity 漂移状态"""
    redis = AsyncRedisClient.get_instance()
    return await redis.hget(_state_key(chat_id), "state")


async def get_identity_updated_at(chat_id: str) -> str | None:
    """读取上次漂移更新时间（ISO 格式）"""
    redis = AsyncRedisClient.get_instance()
    return await redis.hget(_state_key(chat_id), "updated_at")


async def set_identity_state(chat_id: str, state: str) -> None:
    """写入 identity 漂移状态到 Redis"""
    redis = AsyncRedisClient.get_instance()
    now = datetime.now(CST).isoformat()
    pipe = redis.pipeline()
    pipe.hset(_state_key(chat_id), mapping={"state": state, "updated_at": now})
    pipe.expire(_state_key(chat_id), settings.identity_drift_ttl_seconds)
    await pipe.execute()
    logger.info(f"Identity state updated for {chat_id}: {state[:50]}...")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py -v -x 2>&1 | tail -20
```

预期：PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/identity_drift.py apps/agent-service/tests/unit/test_identity_drift.py
git commit -m "feat(identity): add Redis state storage for identity drift"
```

---

### Task 3: IdentityDriftManager — 两阶段锁核心

**Files:**
- Modify: `apps/agent-service/app/services/identity_drift.py`
- Modify: `apps/agent-service/tests/unit/test_identity_drift.py`

- [ ] **Step 1: 写测试 — 单事件触发完整流程**

在 `tests/unit/test_identity_drift.py` 追加：

```python
import asyncio


@pytest.mark.asyncio
async def test_on_event_single_triggers_drift_after_debounce():
    """单个事件 → 等待 debounce → 执行漂移"""
    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
    ):
        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis_cls.get_instance.return_value = mock_redis

        mock_settings.identity_drift_debounce_seconds = 0.1  # 100ms for test
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()
        await mgr.on_event("chat_001")

        # Wait for debounce + small margin
        await asyncio.sleep(0.3)

        mock_drift.assert_called_once_with("chat_001")


@pytest.mark.asyncio
async def test_on_event_debounce_resets_timer():
    """多个事件在 debounce 内 → 计时器重置 → 只触发一次漂移"""
    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
    ):
        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis_cls.get_instance.return_value = mock_redis

        mock_settings.identity_drift_debounce_seconds = 0.2
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # 3 events, each within debounce window
        await mgr.on_event("chat_001")
        await asyncio.sleep(0.05)
        await mgr.on_event("chat_001")
        await asyncio.sleep(0.05)
        await mgr.on_event("chat_001")

        # Wait for debounce from last event
        await asyncio.sleep(0.4)

        # Only one drift should fire
        mock_drift.assert_called_once_with("chat_001")


@pytest.mark.asyncio
async def test_on_event_forced_flush_at_threshold():
    """缓冲区超过 M 条 → 强制进入二阶段"""
    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", new_callable=AsyncMock) as mock_drift,
    ):
        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis_cls.get_instance.return_value = mock_redis

        mock_settings.identity_drift_debounce_seconds = 10  # long debounce
        mock_settings.identity_drift_max_buffer = 3  # low threshold for test

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # Send M events rapidly
        for _ in range(3):
            await mgr.on_event("chat_001")

        # Phase 2 should start immediately (no waiting for debounce)
        await asyncio.sleep(0.2)
        mock_drift.assert_called_once_with("chat_001")


@pytest.mark.asyncio
async def test_phase2_buffers_new_events():
    """二阶段执行中新事件 → 进入下一轮缓冲区"""
    drift_started = asyncio.Event()
    drift_release = asyncio.Event()

    async def slow_drift(chat_id: str):
        drift_started.set()
        await drift_release.wait()

    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.settings") as mock_settings,
        patch("app.services.identity_drift._run_drift", side_effect=slow_drift) as mock_drift,
    ):
        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis_cls.get_instance.return_value = mock_redis

        mock_settings.identity_drift_debounce_seconds = 0.05
        mock_settings.identity_drift_max_buffer = 20

        from app.services.identity_drift import IdentityDriftManager

        mgr = IdentityDriftManager()

        # Trigger first drift
        await mgr.on_event("chat_001")
        await asyncio.sleep(0.1)  # debounce fires

        await drift_started.wait()

        # New event during phase 2
        await mgr.on_event("chat_001")
        assert mgr._buffers.get("chat_001", 0) > 0  # buffered

        # Release phase 2
        drift_release.set()
        await asyncio.sleep(0.3)  # wait for next round

        # Should have been called twice (original + next round)
        assert mock_drift.call_count == 2
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py::test_on_event_single_triggers_drift_after_debounce -v -x 2>&1 | tail -20
```

预期：FAIL — `IdentityDriftManager` 不存在。

- [ ] **Step 3: 实现 IdentityDriftManager**

在 `apps/agent-service/app/services/identity_drift.py` 追加：

```python
import asyncio
from dataclasses import dataclass, field


@dataclass
class _DriftBuffer:
    """一阶段消息缓冲区"""
    count: int = 0
    first_event_ts: float = 0
    last_event_ts: float = 0


class IdentityDriftManager:
    """两阶段锁 identity 漂移管理器

    每个 chat_id 独立管理，不并行漂移。
    一阶段：收集消息（debounce N 秒 + 强制 flush M 条）
    二阶段：LLM 漂移计算（不可中断）
    """

    _instance: "IdentityDriftManager | None" = None

    def __init__(self):
        self._buffers: dict[str, int] = {}  # chat_id -> event count
        self._timers: dict[str, asyncio.Task] = {}  # chat_id -> phase1 timer
        self._phase2_running: set[str] = set()  # chat_ids in phase2

    @classmethod
    def get_instance(cls) -> "IdentityDriftManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def on_event(self, chat_id: str) -> None:
        """消息/回复事件 → 进入两阶段锁流程"""
        self._buffers[chat_id] = self._buffers.get(chat_id, 0) + 1

        # 二阶段运行中 → 只缓冲，不触发
        if chat_id in self._phase2_running:
            return

        # 取消已有计时器（重置 debounce）
        if chat_id in self._timers:
            self._timers[chat_id].cancel()
            del self._timers[chat_id]

        # 超过阈值 → 强制进入二阶段
        if self._buffers.get(chat_id, 0) >= settings.identity_drift_max_buffer:
            asyncio.create_task(self._enter_phase2(chat_id))
            return

        # 启动/重置 debounce 计时器
        self._timers[chat_id] = asyncio.create_task(
            self._phase1_timer(chat_id)
        )

    async def _phase1_timer(self, chat_id: str):
        """一阶段计时器：N 秒无新消息后进入二阶段"""
        try:
            await asyncio.sleep(settings.identity_drift_debounce_seconds)
            await self._enter_phase2(chat_id)
        except asyncio.CancelledError:
            pass  # timer reset by new event

    async def _enter_phase2(self, chat_id: str):
        """进入二阶段：清空缓冲区，执行 LLM 漂移"""
        event_count = self._buffers.pop(chat_id, 0)
        self._timers.pop(chat_id, None)

        if event_count == 0:
            return

        self._phase2_running.add(chat_id)
        try:
            logger.info(
                f"Identity drift phase2 for {chat_id}: "
                f"{event_count} events buffered"
            )
            await _run_drift(chat_id)
        except Exception as e:
            logger.error(f"Identity drift failed for {chat_id}: {e}")
        finally:
            self._phase2_running.discard(chat_id)
            # 二阶段期间有新事件 → 启动下一轮
            if self._buffers.get(chat_id, 0) > 0:
                asyncio.create_task(self.on_event(chat_id))


async def _run_drift(chat_id: str) -> None:
    """二阶段：LLM 漂移计算（占位，Task 4 实现）"""
    pass
```

- [ ] **Step 4: 运行全部漂移测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py -v -x 2>&1 | tail -30
```

预期：全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/identity_drift.py apps/agent-service/tests/unit/test_identity_drift.py
git commit -m "feat(identity): implement two-phase lock drift manager

Phase 1: debounce message collection with forced flush threshold.
Phase 2: non-interruptible LLM computation (placeholder).
New events during phase 2 buffer for next round."
```

---

### Task 4: 漂移 LLM 计算

**Files:**
- Modify: `apps/agent-service/app/services/identity_drift.py`
- Modify: `apps/agent-service/tests/unit/test_identity_drift.py`

- [ ] **Step 1: 写测试 — LLM 漂移调用**

在 `tests/unit/test_identity_drift.py` 追加：

```python
@pytest.mark.asyncio
async def test_run_drift_calls_llm_and_saves_state():
    """_run_drift 读取上下文 → 调用 LLM → 保存新状态"""
    mock_response = MagicMock()
    mock_response.content = "有点犯困但还不想睡。刚才群里闹腾了一阵，觉得好笑。"

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_response)

    mock_redis = AsyncMock()
    mock_redis.hget = AsyncMock(return_value="精力充沛，想找人聊天。")
    mock_pipe = MagicMock()
    mock_pipe.hset = MagicMock()
    mock_pipe.expire = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_redis.pipeline.return_value = mock_pipe

    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.ModelBuilder") as mock_mb,
        patch("app.services.identity_drift.get_prompt") as mock_get_prompt,
        patch("app.services.identity_drift._get_recent_messages",
              new_callable=AsyncMock,
              return_value="[15:30] A哥: 赤尾你觉得呢\n[15:31] 赤尾: 不觉得"),
        patch("app.services.identity_drift._get_schedule_context",
              new_callable=AsyncMock,
              return_value="下午有点犯困，想窝着看番"),
    ):
        mock_redis_cls.get_instance.return_value = mock_redis
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        mock_prompt = MagicMock()
        mock_prompt.compile.return_value = "compiled prompt"
        mock_get_prompt.return_value = mock_prompt

        from app.services.identity_drift import _run_drift
        await _run_drift("chat_001")

    # LLM was called
    mock_model.ainvoke.assert_called_once()
    # State was saved
    mock_pipe.hset.assert_called_once()
    call_args = mock_pipe.hset.call_args
    assert "identity:chat_001" in call_args.args or call_args.args[0] == "identity:chat_001"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py::test_run_drift_calls_llm_and_saves_state -v -x 2>&1 | tail -20
```

预期：FAIL — `_run_drift` 是空占位。

- [ ] **Step 3: 实现 _run_drift + 辅助函数**

在 `apps/agent-service/app/services/identity_drift.py` 中，替换占位的 `_run_drift` 并添加辅助函数。

在文件顶部追加 import：

```python
from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.orm.crud import get_chat_messages_in_range, get_plan_for_period, get_username
```

替换 `_run_drift` 函数：

```python
async def _run_drift(chat_id: str) -> None:
    """二阶段：LLM 漂移计算

    读取最近消息 + 当前 identity + Schedule 上下文 → 调用 LLM → 保存新状态
    """
    # 1. 收集上下文
    current_state = await get_identity_state(chat_id)
    recent_messages = await _get_recent_messages(chat_id)
    schedule_context = await _get_schedule_context()

    if not recent_messages:
        logger.info(f"No recent messages for {chat_id}, skip drift")
        return

    # 2. 编译 prompt
    prompt_template = get_prompt("identity_drift")
    now = datetime.now(CST)
    compiled = prompt_template.compile(
        schedule_daily_current_period=schedule_context,
        current_identity_state=current_state or "（刚醒来，还没有形成今天的状态）",
        message_buffer=recent_messages,
        current_time=now.strftime("%H:%M"),
    )

    # 3. 调用 LLM
    model = await ModelBuilder.build_chat_model(settings.identity_drift_model)
    response = await model.ainvoke(
        [{"role": "user", "content": compiled}],
    )

    new_state = response.content
    if isinstance(new_state, list):
        new_state = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in new_state
        )

    if not new_state or not new_state.strip():
        logger.warning(f"Drift LLM returned empty for {chat_id}")
        return

    # 4. 保存新状态
    await set_identity_state(chat_id, new_state.strip())


async def _get_recent_messages(chat_id: str, max_messages: int = 50) -> str:
    """获取上次漂移以来的消息，格式化为时间线"""
    # 确定起始时间：上次漂移时间 or 1小时前
    updated_at_str = await get_identity_updated_at(chat_id)
    if updated_at_str:
        try:
            start_dt = datetime.fromisoformat(updated_at_str)
        except ValueError:
            start_dt = datetime.now(CST) - timedelta(hours=1)
    else:
        start_dt = datetime.now(CST) - timedelta(hours=1)

    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(datetime.now(CST).timestamp() * 1000)

    messages = await get_chat_messages_in_range(chat_id, start_ts, end_ts)
    if not messages:
        return ""

    # 取最近 max_messages 条
    messages = messages[-max_messages:]

    # 格式化
    lines = []
    for msg in messages:
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = msg_time.strftime("%H:%M")
        if msg.role == "assistant":
            speaker = "赤尾"
        else:
            name = await get_username(msg.user_id)
            speaker = name or msg.user_id[:6]

        from app.utils.content_parser import parse_content
        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"[{time_str}] {speaker}: {rendered[:200]}")

    return "\n".join(lines)


async def _get_schedule_context() -> str:
    """获取当前时段的 Schedule daily"""
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    schedule = await get_plan_for_period("daily", today, today)
    if schedule and schedule.content:
        return schedule.content
    return "（今天还没有写日程）"
```

- [ ] **Step 4: 运行全部漂移测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_identity_drift.py -v -x 2>&1 | tail -30
```

预期：全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/identity_drift.py apps/agent-service/tests/unit/test_identity_drift.py
git commit -m "feat(identity): implement LLM drift computation

Read recent messages + current state + schedule context,
call drift LLM, save new identity state to Redis."
```

---

### Task 5: 注入漂移状态到 build_inner_context

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py:34-53,56-116`
- Modify: `apps/agent-service/tests/unit/test_memory_context.py`

- [ ] **Step 1: 写测试 — 漂移状态注入**

在 `apps/agent-service/tests/unit/test_memory_context.py` 追加：

```python
@pytest.mark.asyncio
async def test_build_inner_context_injects_drift_state():
    """有漂移状态时，注入到 inner_context 中"""
    with (
        patch("app.services.memory_context._build_today_state",
              new_callable=AsyncMock, return_value="今天有点累"),
        patch("app.services.memory_context.get_group_culture_gestalt",
              new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context._build_people_gestalt",
              new_callable=AsyncMock, return_value=[]),
        patch("app.services.memory_context.get_identity_state",
              new_callable=AsyncMock,
              return_value="有点犯困但还不想睡，说话偏短偏懒"),
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
            chat_name="测试群",
        )

    assert "有点犯困但还不想睡" in result
    # 漂移状态应该在今日状态之前
    drift_pos = result.index("有点犯困")
    today_pos = result.index("今天有点累")
    assert drift_pos < today_pos


@pytest.mark.asyncio
async def test_build_inner_context_no_drift_state():
    """无漂移状态时，正常 fallback 到 today_state"""
    with (
        patch("app.services.memory_context._build_today_state",
              new_callable=AsyncMock, return_value="今天精力充沛"),
        patch("app.services.memory_context.get_group_culture_gestalt",
              new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context._build_people_gestalt",
              new_callable=AsyncMock, return_value=[]),
        patch("app.services.memory_context.get_identity_state",
              new_callable=AsyncMock, return_value=None),
    ):
        from app.services.memory_context import build_inner_context

        result = await build_inner_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
            chat_name="测试群",
        )

    assert "今天精力充沛" in result
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_memory_context.py::test_build_inner_context_injects_drift_state -v -x 2>&1 | tail -20
```

预期：FAIL — `get_identity_state` 没有被 import/使用。

- [ ] **Step 3: 修改 build_inner_context 注入漂移状态**

在 `apps/agent-service/app/services/memory_context.py` 中：

顶部 import 追加：

```python
from app.services.identity_drift import get_identity_state
```

修改 `build_inner_context()` 函数，在 "今日状态" 区块之前插入漂移状态：

将：

```python
    # === 今日状态（Journal / Schedule） ===
    today_state = await _build_today_state()
    if today_state:
        sections.append(f"你今天的状态：\n{today_state}")
```

改为：

```python
    # === 此刻状态（Identity 漂移） ===
    try:
        drift_state = await get_identity_state(chat_id)
    except Exception:
        drift_state = None
    if drift_state:
        sections.append(f"你此刻的状态：\n{drift_state}")

    # === 今日基调（Journal / Schedule） ===
    today_state = await _build_today_state()
    if today_state:
        sections.append(f"你今天的基调：\n{today_state}")
```

- [ ] **Step 4: 运行全部 memory_context 测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_memory_context.py -v -x 2>&1 | tail -30
```

预期：全部 PASS。注意：已有测试 mock 了 `_build_today_state` 但没有 mock `get_identity_state`，需要确认不影响。如果已有测试因为新 import 失败，在对应测试中添加 `patch("app.services.memory_context.get_identity_state", new_callable=AsyncMock, return_value=None)`。

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py apps/agent-service/tests/unit/test_memory_context.py
git commit -m "feat(identity): inject drift state into inner_context

Add '此刻状态' section before '今日基调' in build_inner_context.
Drift state from Redis takes priority; today_state remains as baseline."
```

---

### Task 6: 触发漂移 — agent.py post-processing

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/agent.py:344-348`

- [ ] **Step 1: 在赤尾回复后触发漂移**

在 `apps/agent-service/app/agents/domains/main/agent.py` 中，找到 post-processing 的 fire-and-forget 块（约 line 344-348）：

```python
if full_content and session_id:
    asyncio.create_task(
        _publish_post_check(session_id, full_content, chat_id, message_id)
    )
```

在其后追加：

```python
    # Identity 漂移触发
    try:
        from app.services.identity_drift import IdentityDriftManager
        asyncio.create_task(
            IdentityDriftManager.get_instance().on_event(chat_id)
        )
    except Exception as e:
        logger.warning(f"Identity drift trigger failed: {e}")
```

- [ ] **Step 2: 运行已有测试确认无回归**

```bash
cd apps/agent-service && uv run pytest tests/ -v --timeout=30 -x 2>&1 | tail -30
```

预期：全部 PASS。

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/agent.py
git commit -m "feat(identity): trigger drift after reply via asyncio.create_task

Fire-and-forget pattern matching existing post-safety-check.
DriftManager handles debounce and phase locking internally."
```

---

### Task 7: Langfuse prompt — identity_drift

**Files:**
- Langfuse prompt `identity_drift`（通过 langfuse skill 创建）

- [ ] **Step 1: 创建 identity_drift prompt**

使用 `langfuse` skill 创建新 prompt `identity_drift`，内容：

```
你是赤尾的"内心状态"。你的任务是感受赤尾现在的情绪和能量状态。

赤尾今天的日程安排：
{schedule_daily_current_period}

赤尾上一刻的状态：
{current_identity_state}

刚才发生了这些事：
{message_buffer}

---

现在是 {current_time}。

请描述赤尾此刻的内心状态。包括：
- 精力和心情（不要用数值，用感觉描述）
- 刚才的对话对她的影响（如果有的话）
- 她现在说话大概会是什么样的（语气、长度、态度）

用赤尾自己的口吻写，像她的内心独白。3-5 句话。不要用任何格式标记。
```

模板变量：`schedule_daily_current_period`, `current_identity_state`, `message_buffer`, `current_time`

Label: `context-v3`（泳道测试用）

- [ ] **Step 2: Commit（空提交记录 prompt 版本）**

```bash
git commit --allow-empty -m "feat(prompt): create identity_drift Langfuse prompt

Template variables: schedule_daily_current_period, current_identity_state,
message_buffer, current_time.
Output: 3-5 sentence inner monologue describing 赤尾's current emotional state."
```

---

### Task 8: 全量测试 + push

**Files:** 无新改动

- [ ] **Step 1: 运行全量测试**

```bash
cd apps/agent-service && uv run pytest tests/ -v --timeout=30 2>&1 | tail -30
```

预期：全部 PASS。

- [ ] **Step 2: 检查所有提交**

```bash
git log --oneline -10
```

预期：6-7 个 commit 覆盖 Task 1-7。

- [ ] **Step 3: Push 分支**

```bash
git push -u origin docs/review-context-system
```

---

### Task 9: 泳道部署 + 实测验证

**Files:** 无代码改动（运维操作）

- [ ] **Step 1: 部署到 context-v3 泳道**

```bash
make deploy APP=agent-service LANE=context-v3 GIT_REF=docs/review-context-system
```

- [ ] **Step 2: 验证 Redis 状态**

在 dev bot 群发消息触发赤尾回复，等待 5 分钟（debounce），然后检查 Redis 中是否有 identity 状态：

使用 `/ops-db` 查赤尾最近回复，确认有新的回复。然后通过日志确认漂移是否触发：

```bash
make logs APP=agent-service KEYWORD="Identity drift" SINCE=10m
```

- [ ] **Step 3: 检查 inner_context 中的漂移注入**

在 Langfuse trace 中找到最新的对话 trace，检查 `inner_context` 变量是否包含 "你此刻的状态" 区块。

- [ ] **Step 4: 观察效果**

在群里和赤尾持续对话 20+ 条，观察：
1. 赤尾的语气/态度是否有变化
2. 被连续追问时是否能感受到情绪波动
3. 不同时段的表现是否不同

记录观察结果供后续调优。
