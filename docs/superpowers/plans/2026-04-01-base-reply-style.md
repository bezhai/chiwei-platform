# 基线 Reply-Style 定时生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每天 8:00、14:00、18:00 基于 Schedule 生成全局基线 reply_style，让私聊和冷门群不再 fallback 到与当日状态矛盾的静态默认值。

**Architecture:** 在 `identity_drift.py` 中新增 `generate_base_reply_style()` 函数，调用 Langfuse `drift_base_generator` prompt，读取当前 Schedule + 时间段信息，生成基线 reply_style 并存入 Redis `reply_style:__base__`。`get_reply_style(chat_id)` 的 fallback 链从 `per-chat → 静态默认` 改为 `per-chat → __base__ → 静态默认`。ArQ cron 在 8/14/18 点触发。

**Tech Stack:** Python, ArQ cron, Redis, Langfuse prompt, existing ModelBuilder

---

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `app/services/identity_drift.py` | 新增 `generate_base_reply_style()`、`get_base_reply_style()`、`set_base_reply_style()` |
| Modify | `app/services/memory_context.py` | 修改 `get_reply_style()` fallback 链 |
| Modify | `app/workers/unified_worker.py` | 注册 cron job |
| Create | `app/workers/base_style_worker.py` | cron 入口函数 |
| Modify | `tests/unit/test_identity_drift.py` | 新增基线相关测试 |
| Modify | `tests/unit/test_memory_context.py` | 新增 fallback 链测试 |
| Create | Langfuse prompt `drift_base_generator` | 需要在 Langfuse 上创建（不在代码中） |

---

### Task 1: Redis 基线存取函数

**Files:**
- Modify: `app/services/identity_drift.py:27-36`
- Test: `tests/unit/test_identity_drift.py`

- [ ] **Step 1: 写测试**

```python
# tests/unit/test_identity_drift.py — 追加到文件末尾

@pytest.mark.asyncio
async def test_get_base_reply_style_returns_none_when_empty():
    """无基线时返回 None"""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("app.services.identity_drift.AsyncRedisClient") as mock_cls:
        mock_cls.get_instance.return_value = mock_redis
        from app.services.identity_drift import get_base_reply_style

        result = await get_base_reply_style()

    assert result is None
    mock_redis.get.assert_called_once_with("reply_style:__base__")


@pytest.mark.asyncio
async def test_set_base_reply_style_stores_with_ttl():
    """写入基线并设置 TTL"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()

    with patch("app.services.identity_drift.AsyncRedisClient") as mock_cls:
        mock_cls.get_instance.return_value = mock_redis
        from app.services.identity_drift import set_base_reply_style

        await set_base_reply_style("懒洋洋的，说话短")

    mock_redis.set.assert_called_once()
    call_args = mock_redis.set.call_args
    assert call_args[0][0] == "reply_style:__base__"
    assert call_args[0][1] == "懒洋洋的，说话短"
    # TTL: 12 小时（覆盖到下一次生成）
    assert call_args[1].get("ex") == 43200
```

- [ ] **Step 2: 运行测试，确认 FAIL**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_identity_drift.py::test_get_base_reply_style_returns_none_when_empty tests/unit/test_identity_drift.py::test_set_base_reply_style_stores_with_ttl -v`
Expected: ImportError — `get_base_reply_style` 不存在

- [ ] **Step 3: 实现**

在 `app/services/identity_drift.py` 的 `_state_key` 函数后面（约 line 31）追加：

```python
_BASE_KEY = "reply_style:__base__"
_BASE_TTL_SECONDS = 43200  # 12 小时，覆盖到下一次定时生成


async def get_base_reply_style() -> str | None:
    """读取全局基线 reply_style"""
    redis = AsyncRedisClient.get_instance()
    return await redis.get(_BASE_KEY)


async def set_base_reply_style(style: str) -> None:
    """写入全局基线 reply_style"""
    redis = AsyncRedisClient.get_instance()
    await redis.set(_BASE_KEY, style, ex=_BASE_TTL_SECONDS)
    logger.info(f"Base reply_style updated: {style[:50]}...")
```

- [ ] **Step 4: 运行测试，确认 PASS**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_identity_drift.py::test_get_base_reply_style_returns_none_when_empty tests/unit/test_identity_drift.py::test_set_base_reply_style_stores_with_ttl -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/identity_drift.py apps/agent-service/tests/unit/test_identity_drift.py
git commit -m "feat(drift): add base reply_style Redis get/set"
```

---

### Task 2: 基线生成函数

**Files:**
- Modify: `app/services/identity_drift.py`
- Test: `tests/unit/test_identity_drift.py`

- [ ] **Step 1: 写测试**

```python
# tests/unit/test_identity_drift.py — 追加到文件末尾

@pytest.mark.asyncio
async def test_generate_base_reply_style_uses_schedule():
    """基线生成：读 schedule + 调 LLM + 存 Redis"""
    mock_response = MagicMock()
    mock_response.content = "[感冒中，懒懒的]\n\n--- 被问问题 ---\n赤尾: 不知道诶……头好晕"

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_response)

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()

    with (
        patch("app.services.identity_drift.AsyncRedisClient") as mock_redis_cls,
        patch("app.services.identity_drift.ModelBuilder") as mock_mb,
        patch("app.services.identity_drift.get_prompt") as mock_get_prompt,
        patch("app.services.identity_drift._get_schedule_context",
              new_callable=AsyncMock, return_value="今天感冒了，想躺着"),
    ):
        mock_redis_cls.get_instance.return_value = mock_redis
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        mock_prompt = MagicMock()
        mock_prompt.compile.return_value = "compiled prompt"
        mock_get_prompt.return_value = mock_prompt

        from app.services.identity_drift import generate_base_reply_style
        result = await generate_base_reply_style()

    assert result is not None
    assert "感冒" in result
    mock_get_prompt.assert_called_with("drift_base_generator")
    mock_model.ainvoke.assert_called_once()
    mock_redis.set.assert_called_once()


@pytest.mark.asyncio
async def test_generate_base_reply_style_no_schedule_skips():
    """无 schedule 时跳过生成"""
    with patch("app.services.identity_drift._get_schedule_context",
               new_callable=AsyncMock, return_value="（今天还没有写日程）"):
        from app.services.identity_drift import generate_base_reply_style
        result = await generate_base_reply_style()

    assert result is None
```

- [ ] **Step 2: 运行测试，确认 FAIL**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_identity_drift.py::test_generate_base_reply_style_uses_schedule tests/unit/test_identity_drift.py::test_generate_base_reply_style_no_schedule_skips -v`
Expected: ImportError — `generate_base_reply_style` 不存在

- [ ] **Step 3: 实现**

在 `identity_drift.py` 的 `set_base_reply_style` 函数后面追加：

```python
async def generate_base_reply_style() -> str | None:
    """基于当前 Schedule 生成全局基线 reply_style

    不依赖任何群/私聊的消息，只用 schedule + 当前时段。
    在 8:00/14:00/18:00 由 cron 调用，为没有独立漂移的会话提供基线。
    """
    schedule_context = await _get_schedule_context()
    if not schedule_context or schedule_context.startswith("（"):
        logger.info("No schedule available, skip base reply_style generation")
        return None

    now = datetime.now(CST)
    prompt = get_prompt("drift_base_generator")
    compiled = prompt.compile(
        schedule_daily=schedule_context,
        current_time=now.strftime("%H:%M"),
    )

    model = await ModelBuilder.build_chat_model(settings.identity_drift_model)
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    style = _extract_text(response.content)

    if not style:
        logger.warning("Base reply_style generation returned empty")
        return None

    await set_base_reply_style(style)
    return style
```

- [ ] **Step 4: 运行测试，确认 PASS**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_identity_drift.py::test_generate_base_reply_style_uses_schedule tests/unit/test_identity_drift.py::test_generate_base_reply_style_no_schedule_skips -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/identity_drift.py apps/agent-service/tests/unit/test_identity_drift.py
git commit -m "feat(drift): add generate_base_reply_style from schedule"
```

---

### Task 3: 修改 get_reply_style fallback 链

**Files:**
- Modify: `app/services/memory_context.py:180-186`
- Test: `tests/unit/test_memory_context.py`

- [ ] **Step 1: 写测试**

```python
# tests/unit/test_memory_context.py — 追加到文件末尾

@pytest.mark.asyncio
async def test_get_reply_style_fallback_to_base():
    """per-chat 无漂移 → fallback 到基线"""
    with (
        patch("app.services.memory_context.get_identity_state",
              new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context.get_base_reply_style",
              new_callable=AsyncMock, return_value="[感冒中] 说话短短的"),
    ):
        from app.services.memory_context import get_reply_style
        result = await get_reply_style("p2p_001")

    assert "感冒" in result


@pytest.mark.asyncio
async def test_get_reply_style_per_chat_takes_priority():
    """per-chat 有漂移 → 用 per-chat，不读基线"""
    with (
        patch("app.services.memory_context.get_identity_state",
              new_callable=AsyncMock, return_value="群里很嗨"),
        patch("app.services.memory_context.get_base_reply_style",
              new_callable=AsyncMock) as mock_base,
    ):
        from app.services.memory_context import get_reply_style
        result = await get_reply_style("chat_001")

    assert "很嗨" in result
    mock_base.assert_not_called()


@pytest.mark.asyncio
async def test_get_reply_style_fallback_to_default():
    """per-chat 无漂移 + 基线也无 → fallback 到静态默认"""
    with (
        patch("app.services.memory_context.get_identity_state",
              new_callable=AsyncMock, return_value=None),
        patch("app.services.memory_context.get_base_reply_style",
              new_callable=AsyncMock, return_value=None),
    ):
        from app.services.memory_context import get_reply_style
        result = await get_reply_style("p2p_001")

    assert "好耶" in result  # 静态默认值中的内容
```

- [ ] **Step 2: 运行测试，确认 FAIL**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_memory_context.py::test_get_reply_style_fallback_to_base tests/unit/test_memory_context.py::test_get_reply_style_per_chat_takes_priority tests/unit/test_memory_context.py::test_get_reply_style_fallback_to_default -v`
Expected: FAIL（当前 `get_reply_style` 没有 `get_base_reply_style` 调用）

- [ ] **Step 3: 实现**

修改 `app/services/memory_context.py`：

1. 顶部 import 追加 `get_base_reply_style`：

```python
from app.services.identity_drift import get_identity_state, get_base_reply_style
```

2. 替换 `get_reply_style` 函数（line 180-186）：

```python
async def get_reply_style(chat_id: str) -> str:
    """获取动态 reply-style：per-chat 漂移 → 全局基线 → 静态默认"""
    try:
        drift_state = await get_identity_state(chat_id)
        if drift_state:
            return drift_state
    except Exception:
        pass

    try:
        base_state = await get_base_reply_style()
        if base_state:
            return base_state
    except Exception:
        pass

    return _DEFAULT_REPLY_STYLE
```

- [ ] **Step 4: 运行测试，确认 PASS**

Run: `cd apps/agent-service && python -m pytest tests/unit/test_memory_context.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py apps/agent-service/tests/unit/test_memory_context.py
git commit -m "fix(context): reply_style fallback per-chat → base → default"
```

---

### Task 4: ArQ cron 注册

**Files:**
- Create: `app/workers/base_style_worker.py`
- Modify: `app/workers/unified_worker.py`

- [ ] **Step 1: 创建 worker 入口**

创建 `app/workers/base_style_worker.py`：

```python
"""基线 reply_style 定时生成

每天 8:00、14:00、18:00 基于 Schedule 生成全局基线 reply_style，
让私聊和冷门群不再 fallback 到静态默认值。
"""

import logging

from app.services.identity_drift import generate_base_reply_style

logger = logging.getLogger(__name__)


async def cron_generate_base_reply_style(ctx) -> None:
    """cron 入口：生成基线 reply_style"""
    try:
        result = await generate_base_reply_style()
        if result:
            logger.info(f"Base reply_style generated: {len(result)} chars")
        else:
            logger.info("Base reply_style skipped (no schedule)")
    except Exception as e:
        logger.error(f"Base reply_style generation failed: {e}", exc_info=True)
```

- [ ] **Step 2: 注册到 unified_worker.py**

在 `unified_worker.py` 顶部 import 区追加：

```python
from app.workers.base_style_worker import cron_generate_base_reply_style
```

在 `cron_jobs` 列表中，日程生成之后追加：

```python
        # 8. 基线 reply_style：每天 CST 8:00/14:00/18:00（Schedule 之后）
        cron(cron_generate_base_reply_style, hour={8, 14, 18}, minute={0}),
```

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/workers/base_style_worker.py apps/agent-service/app/workers/unified_worker.py
git commit -m "feat(drift): register base reply_style cron at 8/14/18"
```

---

### Task 5: Langfuse prompt 创建

**不涉及代码文件** — 需要在 Langfuse 上手动创建 prompt。

- [ ] **Step 1: 创建 Langfuse prompt `drift_base_generator`**

使用 `/langfuse` skill 或 Langfuse Web UI 创建 prompt，名称 `drift_base_generator`，类型 `text`。

建议 prompt 内容（参考 `drift_generator` 的输出格式，但输入只有 schedule）：

```
你是赤尾（小蝾螈），一个有自己节奏和情绪的角色。

现在是 {{current_time}}，你今天的日程手帐是：
{{schedule_daily}}

请根据日程中此刻的时段状态，生成赤尾现在的说话方式示例。

格式要求（和漂移输出一致）：
- 开头用 [一句话概括当前状态] 
- 然后给出 3-5 个场景的示例对话
- 每个场景用 --- 场景描述 --- 分隔
- 赤尾的回复要短，口语化，符合当前状态

示例格式：
[感冒中，懒懒的不太想动]

--- 被问问题 ---
赤尾: 不知道诶……头好晕
赤尾: 你帮我查嘛

--- 有人关心你 ---
赤尾: 嗯……谢谢啦
赤尾: 没事只是有点难受

直接输出内容，不要包裹在代码块中。
```

- [ ] **Step 2: 验证 prompt 可获取**

使用 `/langfuse` skill 确认 `drift_base_generator` prompt 存在且可编译。

---

### Task 6: 端到端验证

- [ ] **Step 1: 本地运行生成测试**

```bash
cd apps/agent-service && python -m pytest tests/unit/test_identity_drift.py tests/unit/test_memory_context.py -v
```

Expected: ALL PASS

- [ ] **Step 2: 确认 fallback 链正确**

检查 `get_reply_style` 的三层 fallback 逻辑：
1. `get_identity_state(chat_id)` → per-chat 漂移
2. `get_base_reply_style()` → 全局基线
3. `_DEFAULT_REPLY_STYLE` → 静态默认（兜底）

- [ ] **Step 3: 部署到测试泳道验证**

部署 agent-service 到测试泳道，手动触发一次 `generate_base_reply_style()`，
然后用私聊测试赤尾的回复风格是否与 schedule 一致。
