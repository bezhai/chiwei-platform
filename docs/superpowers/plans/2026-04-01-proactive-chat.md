# 赤尾群聊窥屏 MVP 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让赤尾能在群聊中主动插话——基于"顺手刷"和"cron 兜底"两层触发，用小模型判断是否想说话，复用现有 chat_request 链路发送。

**Architecture:** 新增 `proactive_scanner.py` 作为核心模块，负责扫描 → 小模型判断 → 构造合成消息 → 投递 chat_request。两个触发入口：chat_consumer 处理完后的 piggyback、unified_worker 的 cron job。context_builder 和 memory_context 加 proactive 分支，chat-response-worker 调整发送方式。

**Tech Stack:** Python (agent-service)、TypeScript (lark-server)、ARQ cron、RabbitMQ、Redis、PostgreSQL、gemini-2.0-flash (小模型判断)

**Spec:** `docs/superpowers/specs/2026-04-01-proactive-chat-design.md`

---

### Task 1: 创建 proactive_scanner.py — 核心扫描与判断模块

**Files:**
- Create: `apps/agent-service/app/workers/proactive_scanner.py`
- Test: `apps/agent-service/tests/workers/test_proactive_scanner.py`

这是整个功能的核心新文件。包含：冷却检查、拉取未读消息、小模型判断、合成消息构造、chat_request 投递。

- [ ] **Step 1: 写测试 — should_scan 冷却逻辑**

```python
# tests/workers/test_proactive_scanner.py
import time
import pytest
from unittest.mock import AsyncMock, patch

from app.workers.proactive_scanner import should_scan, COOLDOWN_KEY


@pytest.mark.asyncio
async def test_should_scan_returns_true_when_no_recent_scan():
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    with patch("app.workers.proactive_scanner.AsyncRedisClient.get_instance", return_value=mock_redis):
        assert await should_scan() is True


@pytest.mark.asyncio
async def test_should_scan_returns_false_within_cooldown():
    mock_redis = AsyncMock()
    mock_redis.get.return_value = str(int(time.time() * 1000))  # just now
    with patch("app.workers.proactive_scanner.AsyncRedisClient.get_instance", return_value=mock_redis):
        assert await should_scan() is False


@pytest.mark.asyncio
async def test_should_scan_returns_true_after_cooldown():
    mock_redis = AsyncMock()
    mock_redis.get.return_value = str(int(time.time() * 1000) - 20 * 60 * 1000)  # 20min ago
    with patch("app.workers.proactive_scanner.AsyncRedisClient.get_instance", return_value=mock_redis):
        assert await should_scan() is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && python -m pytest tests/workers/test_proactive_scanner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.workers.proactive_scanner'`

- [ ] **Step 3: 实现 proactive_scanner.py 骨架 + should_scan**

```python
# apps/agent-service/app/workers/proactive_scanner.py
"""赤尾群聊窥屏 — 主动搭话扫描器

两层触发（piggyback / cron）共用同一套扫描 → 判断 → 投递流程。
MVP 硬编码目标群 oc_a44255e98af05f1359aeb29eeb503536。
"""

import json
import logging
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.clients.redis import AsyncRedisClient
from app.config import settings

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
TARGET_CHAT_ID = "oc_a44255e98af05f1359aeb29eeb503536"
COOLDOWN_KEY = "proactive:last_scan_time"
COOLDOWN_MS = 15 * 60 * 1000  # 15 分钟
PROACTIVE_USER_ID = "__proactive__"
QUIET_HOURS = (23, 9)  # 23:00-09:00 不触发


async def should_scan() -> bool:
    """检查冷却：15 分钟内已扫描过则跳过"""
    redis = AsyncRedisClient.get_instance()
    last_scan = await redis.get(COOLDOWN_KEY)
    if last_scan:
        elapsed_ms = int(time.time() * 1000) - int(last_scan)
        if elapsed_ms < COOLDOWN_MS:
            return False
    return True


def _is_quiet_hours() -> bool:
    """23:00-09:00 CST 不触发"""
    hour = datetime.now(CST).hour
    return hour >= QUIET_HOURS[0] or hour < QUIET_HOURS[1]


async def _mark_scanned() -> None:
    """记录扫描时间戳到 Redis"""
    redis = AsyncRedisClient.get_instance()
    await redis.set(COOLDOWN_KEY, str(int(time.time() * 1000)), ex=COOLDOWN_MS // 1000 + 60)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/workers/test_proactive_scanner.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: 写测试 — get_unseen_messages**

```python
# 追加到 tests/workers/test_proactive_scanner.py
from datetime import datetime, timezone
from app.workers.proactive_scanner import get_unseen_messages, TARGET_CHAT_ID


@pytest.mark.asyncio
async def test_get_unseen_messages_returns_messages_after_last_presence():
    mock_session = AsyncMock()
    mock_result = AsyncMock()
    # 模拟有 2 条新消息
    mock_result.scalars.return_value.all.return_value = [
        AsyncMock(message_id="m1", content='{"text":"hello"}', user_id="u1",
                  create_time=1000, role="user", chat_id=TARGET_CHAT_ID),
        AsyncMock(message_id="m2", content='{"text":"world"}', user_id="u2",
                  create_time=2000, role="user", chat_id=TARGET_CHAT_ID),
    ]
    mock_session.execute.return_value = mock_result

    # 模拟 last_presence_time
    mock_presence_result = AsyncMock()
    mock_presence_result.scalar.return_value = 500  # 赤尾上次在 t=500 说话

    mock_session.execute.side_effect = [mock_presence_result, mock_result]

    with patch("app.workers.proactive_scanner.AsyncSessionLocal") as mock_session_cls:
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        messages = await get_unseen_messages()

    assert len(messages) == 2


@pytest.mark.asyncio
async def test_get_unseen_messages_returns_empty_when_no_new_messages():
    mock_session = AsyncMock()
    mock_presence_result = AsyncMock()
    mock_presence_result.scalar.return_value = 5000

    mock_msg_result = AsyncMock()
    mock_msg_result.scalars.return_value.all.return_value = []

    mock_session.execute.side_effect = [mock_presence_result, mock_msg_result]

    with patch("app.workers.proactive_scanner.AsyncSessionLocal") as mock_session_cls:
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        messages = await get_unseen_messages()

    assert messages == []
```

- [ ] **Step 6: 实现 get_unseen_messages**

```python
# 追加到 proactive_scanner.py
from sqlalchemy import select, func, and_, desc
from app.orm.base import AsyncSessionLocal
from app.orm.models import ConversationMessage


async def get_unseen_messages(limit: int = 30) -> list[ConversationMessage]:
    """拉取目标群中赤尾'不在场'期间的新消息"""
    async with AsyncSessionLocal() as session:
        # 赤尾在该群最后一次说话的时间
        last_presence = await session.execute(
            select(func.max(ConversationMessage.create_time)).where(
                and_(
                    ConversationMessage.chat_id == TARGET_CHAT_ID,
                    ConversationMessage.role == "assistant",
                )
            )
        )
        last_presence_time = last_presence.scalar() or 0

        # 拉取之后的用户消息
        result = await session.execute(
            select(ConversationMessage)
            .where(
                and_(
                    ConversationMessage.chat_id == TARGET_CHAT_ID,
                    ConversationMessage.role == "user",
                    ConversationMessage.create_time > last_presence_time,
                    ConversationMessage.user_id != PROACTIVE_USER_ID,
                )
            )
            .order_by(ConversationMessage.create_time.asc())
            .limit(limit)
        )
        return list(result.scalars().all())
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/workers/test_proactive_scanner.py -v`
Expected: 5 tests PASS

- [ ] **Step 8: 写测试 — judge_response 小模型判断**

```python
# 追加到 tests/workers/test_proactive_scanner.py
from app.workers.proactive_scanner import judge_response


@pytest.mark.asyncio
async def test_judge_response_returns_respond_true():
    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = AsyncMock(
        content='{"respond": true, "target_message_id": "m1", "stimulus": "他们在聊番剧"}'
    )
    with patch("app.workers.proactive_scanner.ModelBuilder.build_chat_model",
               new_callable=AsyncMock, return_value=mock_model):
        result = await judge_response(
            messages_text="[10:00] 小明: 最近有啥好看的番吗",
            reply_style="元气活泼",
            group_culture="二次元浓度拉满的群",
            recent_proactive=[],
        )
    assert result["respond"] is True
    assert result["stimulus"] == "他们在聊番剧"


@pytest.mark.asyncio
async def test_judge_response_returns_respond_false():
    mock_model = AsyncMock()
    mock_model.ainvoke.return_value = AsyncMock(
        content='{"respond": false}'
    )
    with patch("app.workers.proactive_scanner.ModelBuilder.build_chat_model",
               new_callable=AsyncMock, return_value=mock_model):
        result = await judge_response(
            messages_text="[10:00] 小明: 我去吃饭了",
            reply_style="懒洋洋的",
            group_culture="日常闲聊群",
            recent_proactive=[],
        )
    assert result["respond"] is False
```

- [ ] **Step 9: 实现 judge_response**

```python
# 追加到 proactive_scanner.py
from app.agents.infra.model_builder import ModelBuilder
from app.agents.infra.langfuse_client import get_prompt
from app.utils.content_parser import parse_content

JUDGE_MODEL_ID = "proactive-judge-model"  # DB 中配置的小模型 ID


async def judge_response(
    messages_text: str,
    reply_style: str,
    group_culture: str,
    recent_proactive: list[dict],
) -> dict:
    """用小模型判断赤尾想不想在群里说话

    Returns:
        {"respond": bool, "target_message_id": str?, "stimulus": str?}
    """
    recent_str = ""
    if recent_proactive:
        lines = [f"- {r['time']}: {r['summary']}" for r in recent_proactive]
        recent_str = "你今天已经主动在这个群说过的话：\n" + "\n".join(lines)

    prompt_template = get_prompt("proactive_judge")
    prompt_text = prompt_template.compile(
        messages=messages_text,
        reply_style=reply_style,
        group_culture=group_culture,
        recent_proactive=recent_str,
    )

    model = await ModelBuilder.build_chat_model(JUDGE_MODEL_ID)
    response = await model.ainvoke([{"role": "user", "content": prompt_text}])

    raw = response.content
    if isinstance(raw, list):
        raw = "".join(str(c) for c in raw)

    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        logger.warning(f"judge_response JSON 解析失败: {raw[:200]}")
        return {"respond": False}
```

- [ ] **Step 10: 运行测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/workers/test_proactive_scanner.py -v`
Expected: 7 tests PASS

- [ ] **Step 11: 实现 submit_proactive_request — 合成消息 + 投递**

```python
# 追加到 proactive_scanner.py
from app.clients.rabbitmq import CHAT_REQUEST, RabbitMQClient


async def submit_proactive_request(
    target_message_id: str | None,
    stimulus: str,
) -> None:
    """构造合成消息并投递 proactive chat_request"""
    now_ms = int(time.time() * 1000)
    synthetic_id = f"proactive_{uuid.uuid4().hex[:16]}"

    # 插入合成消息到 conversation_messages
    async with AsyncSessionLocal() as session:
        session.add(ConversationMessage(
            message_id=synthetic_id,
            chat_id=TARGET_CHAT_ID,
            chat_type="group",
            role="user",
            user_id=PROACTIVE_USER_ID,
            content=json.dumps({"text": stimulus}),
            root_message_id=target_message_id or synthetic_id,
            reply_message_id=target_message_id,
            create_time=now_ms,
            message_type="proactive_trigger",
            vector_status="skipped",
            bot_name="chiwei",
        ))
        await session.commit()

    # 投递 chat_request
    client = RabbitMQClient.get_instance()
    await client.publish(
        CHAT_REQUEST,
        {
            "session_id": str(uuid.uuid4()),
            "message_id": synthetic_id,
            "chat_id": TARGET_CHAT_ID,
            "is_p2p": False,
            "root_id": target_message_id or "",
            "user_id": PROACTIVE_USER_ID,
            "bot_name": "chiwei",
            "lane": "prod",
            "is_proactive": True,
            "enqueued_at": now_ms,
        },
        lane=None,  # 强制 prod
    )
    logger.info(
        "proactive_request_submitted",
        extra={
            "synthetic_id": synthetic_id,
            "target_message_id": target_message_id,
            "stimulus": stimulus[:100],
        },
    )
```

- [ ] **Step 12: 实现主入口 run_proactive_scan — 完整流程编排**

```python
# 追加到 proactive_scanner.py
from app.orm.crud import get_group_culture_gestalt
from app.services.memory_context import get_reply_style


async def _get_recent_proactive_records() -> list[dict]:
    """获取今天赤尾在目标群的主动发言触发记录（查询合成触发消息）"""
    today_start = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(
                and_(
                    ConversationMessage.chat_id == TARGET_CHAT_ID,
                    ConversationMessage.user_id == PROACTIVE_USER_ID,
                    ConversationMessage.create_time > today_start_ms,
                )
            )
            .order_by(ConversationMessage.create_time.desc())
            .limit(10)
        )
        rows = result.scalars().all()

    records = []
    for r in rows:
        t = datetime.fromtimestamp(r.create_time / 1000, tz=CST)
        parsed = parse_content(r.content)
        records.append({
            "time": t.strftime("%H:%M"),
            "summary": parsed.render()[:80],
        })
    return records


async def _format_messages_for_judge(messages: list[ConversationMessage]) -> str:
    """将消息格式化为小模型判断用的文本"""
    lines = []
    for msg in messages:
        t = datetime.fromtimestamp(msg.create_time / 1000, tz=CST)
        time_str = t.strftime("%H:%M:%S")
        parsed = parse_content(msg.content)
        text = parsed.render()
        lines.append(f"[{time_str}] {msg.user_id[:8]}: {text}")
    return "\n".join(lines)


async def run_proactive_scan(source: str = "cron") -> None:
    """主入口：执行一次完整的窥屏扫描

    Args:
        source: 触发来源，"cron" 或 "piggyback"，仅用于日志
    """
    if _is_quiet_hours():
        return

    if not await should_scan():
        logger.debug("proactive_scan skipped: cooldown")
        return

    await _mark_scanned()

    # 1. 拉取未读消息
    unseen = await get_unseen_messages()
    if not unseen:
        logger.debug("proactive_scan: no unseen messages")
        return

    # 2. 收集上下文
    reply_style = await get_reply_style(TARGET_CHAT_ID)
    group_culture = await get_group_culture_gestalt(TARGET_CHAT_ID)
    recent_proactive = await _get_recent_proactive_records()
    messages_text = await _format_messages_for_judge(unseen)

    # 3. 小模型判断
    judgment = await judge_response(
        messages_text=messages_text,
        reply_style=reply_style,
        group_culture=group_culture,
        recent_proactive=recent_proactive,
    )

    if not judgment.get("respond"):
        logger.info(
            "proactive_scan: decided not to respond",
            extra={"source": source, "unseen_count": len(unseen)},
        )
        return

    # 4. 投递
    await submit_proactive_request(
        target_message_id=judgment.get("target_message_id"),
        stimulus=judgment.get("stimulus", ""),
    )
    logger.info(
        "proactive_scan: response submitted",
        extra={
            "source": source,
            "target_message_id": judgment.get("target_message_id"),
            "stimulus": judgment.get("stimulus", "")[:100],
        },
    )
```

- [ ] **Step 13: 运行全部测试**

Run: `cd apps/agent-service && python -m pytest tests/workers/test_proactive_scanner.py -v`
Expected: All PASS

- [ ] **Step 14: Commit**

```bash
cd apps/agent-service
git add app/workers/proactive_scanner.py tests/workers/test_proactive_scanner.py
git commit -m "feat(proactive): 新增 proactive_scanner 核心模块

扫描 → 小模型判断 → 合成消息 → 投递 chat_request 的完整流程。
MVP 硬编码目标群，含冷却检查和深夜静默。"
```

---

### Task 2: 注册 Cron Job — unified_worker.py

**Files:**
- Modify: `apps/agent-service/app/workers/unified_worker.py:9-84`

- [ ] **Step 1: 添加 import 和 cron job wrapper**

在 `unified_worker.py` 顶部 import 区域（约第 26 行之后）添加：

```python
from app.workers.proactive_scanner import run_proactive_scan
```

添加 cron job wrapper 函数（在 `on_startup` 之前，约第 32 行之前）：

```python
async def proactive_scan_job(ctx) -> None:
    """主动搭话扫描（cron 兜底触发）

    每 15 分钟执行一次，约 40% 概率真正扫描（平均有效间隔约 35 分钟）。
    """
    import random
    if random.random() > 0.4:
        return
    await run_proactive_scan(source="cron")
```

- [ ] **Step 2: 在 cron_jobs 列表中注册**

在 `cron_jobs` 列表（约第 65-84 行）末尾追加：

```python
            cron(proactive_scan_job, minute={0, 15, 30, 45}),  # 每 15 分钟，40% 概率执行
```

- [ ] **Step 3: 运行 lint/type check 确认无语法错误**

Run: `cd apps/agent-service && python -c "from app.workers.unified_worker import UnifiedWorkerSettings; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/workers/unified_worker.py
git commit -m "feat(proactive): 注册 cron job，每 15 分钟 40% 概率触发扫描"
```

---

### Task 3: Piggyback 触发 — chat_consumer.py

**Files:**
- Modify: `apps/agent-service/app/workers/chat_consumer.py:36-215`

- [ ] **Step 1: 在 handle_chat_request 末尾添加 piggyback 触发**

在 `handle_chat_request` 函数中，约第 48 行后（解析 body 之后）添加读取 `is_proactive`：

```python
        is_proactive = body.get("is_proactive", False)
```

在第 196 行（日志记录之后、except 之前）添加 piggyback 触发：

```python
            # Piggyback: 回复完后顺手刷一眼群聊（proactive 回复不触发，避免递归）
            if not is_proactive:
                import asyncio
                asyncio.create_task(_maybe_piggyback_scan())

```

- [ ] **Step 2: 添加 _maybe_piggyback_scan 辅助函数**

在文件末尾（`start_chat_consumer` 之前）添加：

```python
async def _maybe_piggyback_scan() -> None:
    """概率触发一次主动搭话扫描（piggyback 模式）"""
    import random
    try:
        if random.random() > 0.6:  # 约 60% 概率触发
            return
        from app.workers.proactive_scanner import run_proactive_scan
        await run_proactive_scan(source="piggyback")
    except Exception as e:
        logger.warning(f"piggyback scan failed: {e}")

```

- [ ] **Step 3: 运行 import 确认**

Run: `cd apps/agent-service && python -c "from app.workers.chat_consumer import handle_chat_request; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/workers/chat_consumer.py
git commit -m "feat(proactive): chat_consumer 添加 piggyback 触发，60% 概率顺手刷群"
```

---

### Task 4: context_builder 适配 proactive 模式

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/context_builder.py:26-148`
- Test: `apps/agent-service/tests/agents/test_proactive_context.py`

- [ ] **Step 1: 写测试 — proactive 模式下过滤合成消息**

```python
# tests/agents/test_proactive_context.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from app.agents.domains.main.context_builder import build_chat_context


def _make_quick_result(message_id, user_id, content, role, create_time_ms, chat_id="oc_test", chat_type="group", chat_name="测试群", username="test_user", reply_message_id=None):
    """创建 QuickSearchResult mock"""
    from app.services.quick_search import QuickSearchResult
    return QuickSearchResult(
        message_id=message_id,
        content=content,
        user_id=user_id,
        create_time=datetime.fromtimestamp(create_time_ms / 1000, tz=timezone.utc),
        role=role,
        username=username,
        chat_type=chat_type,
        chat_name=chat_name,
        reply_message_id=reply_message_id,
        chat_id=chat_id,
    )


@pytest.mark.asyncio
async def test_proactive_filters_out_synthetic_message():
    """proactive 模式下应过滤掉合成消息，返回 is_proactive 标记"""
    results = [
        _make_quick_result("m1", "user1", '{"text":"hello"}', "user", 1000, username="小明"),
        _make_quick_result("m2", "user2", '{"text":"聊番剧"}', "user", 2000, username="小红"),
        _make_quick_result("proactive_abc", "__proactive__", '{"text":"他们在聊番剧"}', "user", 3000, username=None, reply_message_id="m2"),
    ]

    with patch("app.agents.domains.main.context_builder.quick_search", new_callable=AsyncMock, return_value=results):
        with patch("app.agents.domains.main.context_builder.check_group_allows_download", new_callable=AsyncMock, return_value=False):
            msgs, registry, chat_id, trigger_username, chat_type, trigger_user_id, chat_name, chain_user_ids = await build_chat_context("proactive_abc")

    # 合成消息应被过滤，trigger 信息来自真实消息
    assert len(msgs) == 1  # group 模式返回单条 HumanMessage
    assert trigger_username == ""  # proactive 无 trigger user
    assert trigger_user_id == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && python -m pytest tests/agents/test_proactive_context.py -v`
Expected: FAIL — trigger_username 不为空（当前代码取 l1_results[-1] 即合成消息）

- [ ] **Step 3: 修改 build_chat_context 添加 proactive 分支**

在 `build_chat_context` 函数中（约第 39 行之后，`l1_results` 获取之后），添加 proactive 检测和处理逻辑。在 `chat_type = l1_results[-1].chat_type or "p2p"` 之后（第 46 行后）添加：

```python
    # --- Proactive 检测 ---
    is_proactive = l1_results[-1].user_id == PROACTIVE_USER_ID
    proactive_stimulus = ""
    proactive_target_id = ""

    if is_proactive:
        proactive_msg = l1_results.pop()
        proactive_stimulus = parse_content(proactive_msg.content).render()
        proactive_target_id = proactive_msg.reply_message_id or ""

        if not l1_results:
            logger.warning("proactive scan: no real messages found after filtering")
            return [], None, "", "", "group", "", "", []
```

在文件顶部添加常量（import 区域之后）：

```python
PROACTIVE_USER_ID = "__proactive__"
```

- [ ] **Step 4: 调整 trigger 信息提取逻辑**

修改第 128-137 行的 trigger 信息提取，改为区分 proactive 和普通模式：

```python
    # 提取触发消息的用户名和用户ID
    if is_proactive:
        trigger_username = ""
        trigger_user_id = ""
        chat_name = l1_results[-1].chat_name or "" if l1_results else ""
        # proactive 时 trigger_id 指向引起兴趣的消息（获得 ⭐ 标记）
        effective_trigger_id = proactive_target_id or (l1_results[-1].message_id if l1_results else message_id)
    else:
        trigger_username = l1_results[-1].username or ""
        trigger_user_id = l1_results[-1].user_id or ""
        chat_name = l1_results[-1].chat_name or ""
        effective_trigger_id = message_id
```

修改第 119 行群聊消息构建调用，使用 `effective_trigger_id`：

```python
    if chat_type == "group":
        messages = _build_group_messages(
            l1_results, effective_trigger_id, image_key_to_url, image_key_to_filename,
        )
```

修改返回值，附带 proactive 信息（在返回 tuple 末尾不变，但通过模块级变量传递 stimulus）：

在函数开头声明模块级存储（用于传递给 memory_context）：

```python
# 模块级缓存，用于 proactive stimulus 传递（同一 async context 内安全）
import contextvars
_proactive_stimulus_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "proactive_stimulus", default=""
)
_is_proactive_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_proactive", default=False
)
```

在 proactive 检测后设置：

```python
    _is_proactive_var.set(is_proactive)
    _proactive_stimulus_var.set(proactive_stimulus)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/agents/test_proactive_context.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/context_builder.py tests/agents/test_proactive_context.py
git commit -m "feat(proactive): context_builder 添加 proactive 分支，过滤合成消息"
```

---

### Task 5: memory_context 添加 proactive 场景提示

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py:86-146`
- Test: `apps/agent-service/tests/services/test_proactive_memory_context.py`

- [ ] **Step 1: 写测试**

```python
# tests/services/test_proactive_memory_context.py
import pytest
from unittest.mock import AsyncMock, patch

from app.services.memory_context import build_inner_context


@pytest.mark.asyncio
async def test_proactive_scene_hint_injected():
    """proactive 模式下应注入窥屏场景提示"""
    with patch("app.services.memory_context.get_plan_for_period", new_callable=AsyncMock, return_value=None), \
         patch("app.services.memory_context.get_journal", new_callable=AsyncMock, return_value=None), \
         patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value="二次元群"), \
         patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]):

        result = await build_inner_context(
            chat_id="oc_test",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="",
            trigger_username="",
            chat_name="测试群",
            is_proactive=True,
            proactive_stimulus="他们在聊新番",
        )

    assert "刷到了群里的对话" in result
    assert "他们在聊新番" in result
    assert "需要回复" not in result  # 不应出现"回复某人"的提示


@pytest.mark.asyncio
async def test_normal_mode_no_proactive_hint():
    """普通模式不应出现窥屏提示"""
    with patch("app.services.memory_context.get_plan_for_period", new_callable=AsyncMock, return_value=None), \
         patch("app.services.memory_context.get_journal", new_callable=AsyncMock, return_value=None), \
         patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value=""), \
         patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]):

        result = await build_inner_context(
            chat_id="oc_test",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="小明",
            chat_name="测试群",
        )

    assert "刷到了群里的对话" not in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && python -m pytest tests/services/test_proactive_memory_context.py -v`
Expected: FAIL — `build_inner_context() got an unexpected keyword argument 'is_proactive'`

- [ ] **Step 3: 修改 build_inner_context 签名和逻辑**

修改函数签名（约第 86 行），添加两个可选参数：

```python
async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str,
    trigger_username: str,
    chat_name: str = "",
    *,
    is_proactive: bool = False,
    proactive_stimulus: str = "",
) -> str:
```

修改场景提示部分（约第 109-117 行），添加 proactive 分支：

```python
    # === 场景提示 ===
    if is_proactive:
        scene = f"你在群聊「{chat_name}」中。" if chat_name else ""
        scene += "\n你刚刷到了群里的对话。如果你想说点什么就说，不想说也可以不说。"
        scene += "\n不要刻意解释为什么突然说话，像朋友在群里自然接话就好。"
        if proactive_stimulus:
            scene += f"\n（你注意到的：{proactive_stimulus}）"
        sections.append(scene)
    elif chat_type == "p2p":
        if trigger_username:
            sections.append(f"你正在和 {trigger_username} 私聊。")
    else:
        if chat_name:
            sections.append(f"你在群聊「{chat_name}」中。")
        if trigger_username:
            sections.append(f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && python -m pytest tests/services/test_proactive_memory_context.py -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py tests/services/test_proactive_memory_context.py
git commit -m "feat(proactive): memory_context 支持 proactive 场景提示注入"
```

---

### Task 6: agent.py 传递 proactive 参数

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/agent.py:229-283`

- [ ] **Step 1: 修改 _build_and_stream 传递 proactive 参数**

在 `_build_and_stream` 函数中（约第 274-283 行），修改 `build_inner_context` 调用，从 context_builder 的 contextvar 读取 proactive 状态：

```python
    # 构建统一 inner_context（场景 + 状态 + 印象 + 引导语）
    try:
        from app.agents.domains.main.context_builder import (
            _is_proactive_var,
            _proactive_stimulus_var,
        )

        prompt_vars["inner_context"] = await build_inner_context(
            chat_id=chat_id,
            chat_type=chat_type,
            user_ids=chain_user_ids,
            trigger_user_id=trigger_user_id,
            trigger_username=trigger_username,
            chat_name=chat_name,
            is_proactive=_is_proactive_var.get(False),
            proactive_stimulus=_proactive_stimulus_var.get(""),
        )
    except Exception as e:
        logger.error(f"Failed to build inner context: {e}")
```

- [ ] **Step 2: 确认 import 无误**

Run: `cd apps/agent-service && python -c "from app.agents.domains.main.agent import stream_chat; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/agent.py
git commit -m "feat(proactive): agent.py 传递 proactive 参数到 inner_context"
```

---

### Task 7: chat-response-worker 适配 proactive 发送

**Files:**
- Modify: `apps/lark-server/src/workers/chat-response-worker.ts:173-366`

- [ ] **Step 1: 更新 ChatResponsePayload 接口**

在 `ChatResponsePayload` 接口（约第 173 行）添加 `is_proactive` 和 `bot_name`：

```typescript
interface ChatResponsePayload {
    session_id: string;
    message_id: string;
    chat_id: string;
    is_p2p: boolean;
    root_id?: string;
    user_id?: string;
    content: string;
    full_content?: string;
    status: 'success' | 'failed';
    error?: string;
    lane?: string;
    part_index?: number;
    is_last?: boolean;
    is_proactive?: boolean;  // 新增
    bot_name?: string;       // 新增，proactive 场景用
}
```

- [ ] **Step 2: 修改 handleChatResponse — AgentResponse 查询兼容**

在 `handleChatResponse` 函数中（约第 210 行），解构时添加新字段：

```typescript
    const {
        session_id,
        message_id,
        chat_id,
        is_p2p,
        root_id,
        user_id,
        content,
        full_content,
        status,
        error,
        part_index = 0,
        is_last = false,
        is_proactive = false,
    } = payload;
```

修改 AgentResponse 查询（约第 228-241 行），兼容 proactive（无 AgentResponse 记录）：

```typescript
    const repo = AppDataSource.getRepository(AgentResponse);

    // 查询 agent_response 获取 bot_name
    const tDbQuery0 = Date.now();
    const agentResponse = await repo.findOneBy({ session_id });
    const dbQueryMs = Date.now() - tDbQuery0;
    chatResponseDuration.labels({ stage: 'db_query' }).observe(dbQueryMs / 1000);

    // proactive 消息没有 agent_response 记录，从 payload 获取 bot_name
    const botName = agentResponse?.bot_name || payload.bot_name;
    if (!botName) {
        console.error(`[ChatResponseWorker] No bot_name found: session_id=${session_id}, is_proactive=${is_proactive}`);
        rabbitmqClient.ack(msg);
        return;
    }
```

- [ ] **Step 3: 修改发送逻辑 — proactive 使用 sendPost 或 replyPost(root_id)**

修改发送消息部分（约第 279-286 行）：

```typescript
            // 发送消息并捕获 AI 消息 ID
            const tSend0 = Date.now();
            let aiMessageId: string | undefined;
            if (part_index === 0) {
                if (is_proactive) {
                    // proactive: reply to real message if available, else send new
                    if (root_id) {
                        aiMessageId = await replyPost(root_id, postContent);
                    } else {
                        aiMessageId = await sendPost(chat_id, postContent);
                    }
                } else {
                    aiMessageId = await replyPost(message_id, postContent);
                }
            } else {
                // 后续消息带延迟后发送
                await sleep(SEND_DELAY_MS);
                aiMessageId = await sendPost(chat_id, postContent);
            }
```

- [ ] **Step 4: 修改 storeMessage — proactive 的 reply_message_id**

修改存储部分（约第 295-306 行）：

```typescript
            await storeMessage({
                user_id: getBotUnionId(),
                content: MessageContentUtils.wrapMarkdownAsV2(content),
                role: 'assistant',
                message_id: effectiveMessageId,
                message_type: 'post',
                chat_id: chat_id,
                chat_type: is_p2p ? 'p2p' : 'group',
                create_time: String(now),
                root_message_id: is_proactive ? (root_id || effectiveMessageId) : (root_id || message_id),
                reply_message_id: is_proactive ? (root_id || undefined) : message_id,
            });
```

- [ ] **Step 5: 修改 AgentResponse 更新逻辑 — proactive 跳过不存在的记录更新**

在追加 replies 和 is_last 更新前添加条件判断：

```typescript
            // proactive 没有 agent_response 记录，跳过 replies 追加和状态更新
            if (agentResponse) {
                const replyEntry = [
                    {
                        message_id: effectiveMessageId,
                        content_type: 'post',
                        sent_at: new Date().toISOString(),
                    },
                ];
                await repo
                    .createQueryBuilder()
                    .update(AgentResponse)
                    .set({
                        replies: () =>
                            `COALESCE(replies, '[]'::jsonb) || :replyEntry::jsonb`,
                    })
                    .setParameter('replyEntry', JSON.stringify(replyEntry))
                    .where('session_id = :sid', { sid: session_id })
                    .execute();

                if (is_last) {
                    await repo.update(
                        { session_id },
                        {
                            response_text: full_content || content,
                            status: 'completed',
                        },
                    );
                }
            }
```

- [ ] **Step 6: Commit**

```bash
git add apps/lark-server/src/workers/chat-response-worker.ts
git commit -m "feat(proactive): chat-response-worker 适配 proactive 发送模式

proactive 消息用 sendPost/replyPost(root_id) 替代 replyPost(message_id)，
兼容无 AgentResponse 记录的场景。"
```

---

### Task 8: chat_consumer 传递 bot_name 到 response

**Files:**
- Modify: `apps/agent-service/app/workers/chat_consumer.py:74-83`

- [ ] **Step 1: 在 base_response 中添加 bot_name 和 is_proactive**

修改 base_response 构造（约第 74-83 行）：

```python
        base_response = {
            "session_id": session_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "is_p2p": is_p2p,
            "root_id": root_id,
            "user_id": user_id,
            "lane": lane,
            "is_proactive": is_proactive,
            "bot_name": bot_name,
        }
```

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/workers/chat_consumer.py
git commit -m "feat(proactive): chat_consumer 传递 is_proactive 和 bot_name 到 response"
```

---

### Task 9: Langfuse prompt 配置 + 模型配置

**Files:**
- 无代码文件变更，需要在外部系统配置

- [ ] **Step 1: 在 Langfuse 创建 proactive_judge prompt**

在 Langfuse 中创建名为 `proactive_judge` 的 prompt，内容：

```
你是赤尾的直觉系统。赤尾是一个元气活泼的女生，正在"刷手机"看群里的消息。

以下是群里最近的对话：
{messages}

赤尾当前的状态：
{reply_style}

赤尾对这个群的感觉：
{group_culture}

{recent_proactive}

请判断赤尾看到这些消息后，会不会想要主动说点什么。判断标准：
1. 有人提到了赤尾（不是@，是在聊天中提起她）→ 自然想回应
2. 话题是赤尾会感兴趣的 → 结合她的性格和对这个群的感觉来判断

注意：
- 大部分时候赤尾不会主动说话，只有真的触动了她才会
- 如果群里只是普通闲聊、打招呼、与赤尾无关的话题，就不要说
- 考虑赤尾当前的状态，累了就更不想说话

返回 JSON 格式（不要 markdown 包裹）：
{{"respond": true/false, "target_message_id": "回复哪条消息的ID（可选）", "stimulus": "赤尾为什么想说话（一句话）"}}
```

- [ ] **Step 2: 在数据库配置 proactive-judge-model**

通过 PaaS API 或数据库添加模型配置，model_id 为 `proactive-judge-model`，指向 gemini-2.0-flash 或同等小模型。

- [ ] **Step 3: 记录配置完成**

验证：
Run: `cd apps/agent-service && python -c "from app.agents.infra.langfuse_client import get_prompt; print(get_prompt('proactive_judge'))"`
Expected: 返回 prompt 对象（不报错）

---

### Task 10: 端到端验证

- [ ] **Step 1: 本地单元测试全部通过**

Run: `cd apps/agent-service && python -m pytest tests/workers/test_proactive_scanner.py tests/agents/test_proactive_context.py tests/services/test_proactive_memory_context.py -v`
Expected: All PASS

- [ ] **Step 2: Push 并部署到测试泳道**

```bash
git push origin feat/chiwei-proactive-chat
make deploy APP=agent-service LANE=feat-proactive GIT_REF=feat/chiwei-proactive-chat
make deploy APP=lark-server LANE=feat-proactive GIT_REF=feat/chiwei-proactive-chat
```

- [ ] **Step 3: 绑定 dev bot 测试**

```bash
/ops bind TYPE=bot KEY=dev LANE=feat-proactive
```

- [ ] **Step 4: 在目标群发消息测试触发**

1. 在 dev bot 的目标群发一些消息（提到赤尾或她感兴趣的话题）
2. @赤尾 触发一次普通回复，观察 piggyback 是否触发
3. 等待 cron 触发（最多 45 分钟）
4. 检查 agent-service 日志：`make logs APP=agent-service KEYWORD=proactive`

- [ ] **Step 5: 验证发送结果**

确认：
- 赤尾能在群里主动发消息
- 回复的消息 reply 到正确的目标消息（或发新消息）
- 深夜时段不触发
- 冷却机制生效（15 分钟内不重复扫描）

- [ ] **Step 6: 清理测试环境**

```bash
/ops unbind TYPE=bot KEY=dev
make undeploy APP=agent-service LANE=feat-proactive
make undeploy APP=lark-server LANE=feat-proactive
```
