# Phase 2b: 聊天注入重构 — 统一 inner_context

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将分散的 `{user_context}` + `{schedule_context}` 合并为单一 `{inner_context}`，由 memory_context.py 统一输出，agent.py 只管注入。

**Architecture:** memory_context.py 的 `build_memory_context` 重命名为 `build_inner_context`，内联 inner_state.py 的 Schedule 加载逻辑，追加记忆回溯引导语。agent.py 的 `prompt_vars` 从 3 个变量简化为 `inner_context` 一个。Langfuse `main` prompt 的 XML 结构从 `<current-state>` + `<conversation-context>` 合并为 `<inner-context>`。

**Tech Stack:** Python 3.12, SQLAlchemy async, Langfuse prompts, pytest

**Spec:** `docs/superpowers/specs/2026-03-26-memory-and-life-system-v2.md` §三.4、§四

---

## 文件结构

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `app/services/memory_context.py` | 重命名入口 + 内联 schedule 加载 + 追加引导语 + 删除兼容 shim |
| Delete | `app/services/inner_state.py` | 逻辑已合并进 memory_context.py |
| Modify | `app/agents/domains/main/agent.py:229-308` | prompt_vars 简化为 inner_context |
| Delete | `tests/unit/test_inner_state.py` | 对应代码已删除 |
| Modify | `tests/unit/test_memory_context.py` | 更新测试覆盖新结构 |
| Langfuse | prompt `main` | `<current-state>` + `<conversation-context>` → `<inner-context>` |

---

### Task 1: 重写 memory_context.py

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py`
- Modify: `apps/agent-service/tests/unit/test_memory_context.py`

**变更点：**
1. 删除 `from app.services.inner_state import build_inner_state`
2. 新增 `from app.orm.crud import get_journal, get_plan_for_period`
3. 入口函数 `build_memory_context` → `build_inner_context`，返回值含场景提示 + schedule + 印象 + 引导语
4. 内联 inner_state 中的 schedule 加载（用 Journal daily 替代 raw Schedule）
5. 追加记忆回溯引导语
6. 删除所有兼容 shim（`build_diary_context` 等）

- [ ] **Step 1: 重写 memory_context.py**

```python
"""赤尾聊天注入上下文 — 统一 inner_context

构建注入 system prompt 的所有上下文：
- 场景提示（群名/私聊 + 回复谁）
- 今日状态（Schedule daily 内容）
- 对人和群的感觉
- 记忆回溯引导语
"""

import logging
from datetime import date, timedelta, timezone, datetime

from app.orm.crud import (
    get_cross_group_impressions,
    get_group_culture_gestalt,
    get_impressions_for_users,
    get_journal,
    get_plan_for_period,
    get_username,
)

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
MAX_IMPRESSION_USERS = 10
MAX_CROSS_GROUP_IMPRESSIONS = 5

_MEMORY_RECALL_HINT = (
    "（你有写日记的习惯。如果聊着聊着觉得"这个事我好像知道点什么但记不清了"，"
    "可以翻翻日记想一想。）"
)


async def _build_today_state() -> str:
    """构建今日状态：优先 Journal daily，fallback Schedule daily"""
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")

    # 优先用 Journal（模糊化的个人感受）
    journal = await get_journal("daily", today)
    if not journal:
        # 今天的还没生成，看昨天的
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        journal = await get_journal("daily", yesterday)

    if journal:
        return journal.content

    # fallback: Schedule daily
    schedule = await get_plan_for_period("daily", today, today)
    if schedule and schedule.content:
        return schedule.content

    return ""


async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str,
    trigger_username: str,
    chat_name: str = "",
) -> str:
    """构建统一的聊天注入上下文

    Args:
        chat_id: 群/私聊 ID
        chat_type: "group" 或 "p2p"
        user_ids: 当前对话中出现的用户 ID 列表
        trigger_user_id: 触发者 user_id
        trigger_username: 触发者用户名
        chat_name: 群名（群聊场景）

    Returns:
        组装好的 inner_context 文本，注入 system prompt
    """
    sections: list[str] = []

    # === 场景提示 ===
    if chat_type == "p2p":
        if trigger_username:
            sections.append(f"你正在和 {trigger_username} 私聊。")
    else:
        if chat_name:
            sections.append(f"你在群聊「{chat_name}」中。")
        if trigger_username:
            sections.append(f"需要回复 {trigger_username} 的消息（消息中用 ⭐ 标记）。")

    # === 今日状态（Journal / Schedule） ===
    today_state = await _build_today_state()
    if today_state:
        sections.append(f"你今天的状态：\n{today_state}")

    # === 对人和群的感觉 ===
    if chat_type == "group":
        group_gestalt = await get_group_culture_gestalt(chat_id)
        if group_gestalt:
            sections.append(f"你对这个群的感觉：{group_gestalt}")

        if user_ids:
            people_lines = await _build_people_gestalt(chat_id, user_ids)
            if people_lines:
                sections.append(
                    "你对当前对话中出现的人的感觉：\n" + "\n".join(people_lines)
                )
    else:
        cross_lines = await _build_cross_group_gestalt(
            trigger_user_id, trigger_username
        )
        if cross_lines:
            sections.append(cross_lines)

    # === 记忆回溯引导语 ===
    sections.append(_MEMORY_RECALL_HINT)

    return "\n\n".join(sections)


async def _build_people_gestalt(chat_id: str, user_ids: list[str]) -> list[str]:
    """构建对话者的感觉 gestalt 列表"""
    impressions = await get_impressions_for_users(
        chat_id, user_ids[:MAX_IMPRESSION_USERS]
    )
    if not impressions:
        return []
    lines = []
    for imp in impressions:
        name = await get_username(imp.user_id) or imp.user_id[:8]
        lines.append(f"- {name}：{imp.impression_text}")
    return lines


async def _build_cross_group_gestalt(user_id: str, trigger_username: str) -> str:
    """构建跨群人物 gestalt（私聊场景）"""
    rows = await get_cross_group_impressions(
        user_id, limit=MAX_CROSS_GROUP_IMPRESSIONS
    )
    if not rows:
        return ""
    lines = []
    for imp, group_name in rows:
        lines.append(f"- （{group_name}）{imp.impression_text}")
    return f"你对 {trigger_username} 的感觉：\n" + "\n".join(lines)


# 向后兼容：旧名保留为别名
build_memory_context = build_inner_context
```

- [ ] **Step 2: 更新测试**

重写 `tests/unit/test_memory_context.py`：

```python
"""测试统一聊天注入上下文"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_build_inner_context_group():
    """群聊：场景 + 状态 + 群感觉 + 人物 + 引导语"""
    with (
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="今天想出门逛逛"),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value="最放飞的群"),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[
            MagicMock(user_id="u1", impression_text="群里的指挥官"),
        ]),
        patch("app.services.memory_context.get_username", new_callable=AsyncMock, return_value="A哥"),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥", chat_name="KA技术群",
        )

    assert "群聊「KA技术群」" in result
    assert "回复 A哥" in result
    assert "想出门逛逛" in result
    assert "放飞" in result
    assert "指挥官" in result
    assert "翻翻日记" in result  # 引导语


@pytest.mark.asyncio
async def test_build_inner_context_p2p():
    """私聊：场景 + 状态 + 跨群印象 + 引导语"""
    with (
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="心情不错"),
        patch("app.services.memory_context.get_cross_group_impressions", new_callable=AsyncMock, return_value=[
            (MagicMock(impression_text="聊动画很带劲"), "KA群"),
        ]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="p2p_001", chat_type="p2p", user_ids=["u1"],
            trigger_user_id="u1", trigger_username="A哥",
        )

    assert "私聊" in result
    assert "心情不错" in result
    assert "动画" in result
    assert "翻翻日记" in result


@pytest.mark.asyncio
async def test_build_inner_context_no_state():
    """无状态时仍包含场景和引导语"""
    with (
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value=""),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="A哥", chat_name="测试群",
        )

    assert "群聊「测试群」" in result
    assert "今天的状态" not in result  # 无状态时不出现
    assert "翻翻日记" in result


@pytest.mark.asyncio
async def test_build_inner_context_no_diary_content():
    """不含日记全文（回归测试）"""
    with (
        patch("app.services.memory_context._build_today_state", new_callable=AsyncMock, return_value="今天下午"),
        patch("app.services.memory_context.get_group_culture_gestalt", new_callable=AsyncMock, return_value="活跃"),
        patch("app.services.memory_context.get_impressions_for_users", new_callable=AsyncMock, return_value=[]),
    ):
        from app.services.memory_context import build_inner_context
        result = await build_inner_context(
            chat_id="chat_001", chat_type="group", user_ids=[],
            trigger_user_id="u1", trigger_username="Test", chat_name="群",
        )

    assert "--- 2026-" not in result
    assert "上周回顾" not in result
```

- [ ] **Step 3: 运行测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_memory_context.py -v`
Expected: 4 PASS

- [ ] **Step 4: 删除 inner_state.py 和它的测试**

```bash
rm apps/agent-service/app/services/inner_state.py
rm apps/agent-service/tests/unit/test_inner_state.py
```

- [ ] **Step 5: 运行全量测试确认无回归**

Run: `cd apps/agent-service && uv run pytest tests/unit/ --ignore=tests/unit/test_vectorize_permission.py -q`
Expected: 所有测试通过（test_inner_state 已删，不再被收集）

- [ ] **Step 6: Commit**

```bash
git add -u && git add apps/agent-service/app/services/memory_context.py apps/agent-service/tests/unit/test_memory_context.py
git commit -m "refactor(memory): unify inner_context, remove inner_state.py"
```

---

### Task 2: 简化 agent.py 的 prompt_vars

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/agent.py:229-310`

**变更点：**
- `prompt_vars` 从 `{user_context, schedule_context, ...}` 改为 `{inner_context, ...}`
- 删除 agent.py 中的场景提示构建（已移入 build_inner_context）
- import 从 `build_memory_context` 改为 `build_inner_context`

- [ ] **Step 1: 修改 agent.py**

将 lines 238-308 的 prompt_vars 构建替换为：

```python
    prompt_vars = {
        "complexity_hint": "",
        "inner_context": "",
        "available_skills": SkillRegistry.list_descriptions(),
    }

    # ... agent 创建不变 ...

    # 构建统一 inner_context
    try:
        prompt_vars["inner_context"] = await build_inner_context(
            chat_id=chat_id,
            chat_type=chat_type,
            user_ids=chain_user_ids,
            trigger_user_id=trigger_user_id,
            trigger_username=trigger_username,
            chat_name=chat_name,
        )
    except Exception as e:
        logger.error(f"Failed to build inner context: {e}")
```

同时更新文件顶部 import：
```python
from app.services.memory_context import build_inner_context
```

- [ ] **Step 2: 运行全量测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/ --ignore=tests/unit/test_vectorize_permission.py -q`

- [ ] **Step 3: Commit**

```bash
git commit -am "refactor(agent): simplify prompt_vars to single inner_context"
```

---

### Task 3: 更新 Langfuse main prompt

**变更点：**
- `<current-state>{{schedule_context}}</current-state>` + `<conversation-context>{{user_context}}</conversation-context>` → `<inner-context>{{inner_context}}</inner-context>`

- [ ] **Step 1: 创建 Langfuse main prompt 新版本**

将 prompt 中的：
```xml
<current-state>
{{schedule_context}}
</current-state>

<conversation-context>
{{user_context}}
</conversation-context>

以上是你的对话场景和记忆。
```

替换为：
```xml
<inner-context>
{{inner_context}}
</inner-context>

以上是你的内心世界和对话场景。
```

通过 langfuse skill 执行 create-prompt。

- [ ] **Step 2: 验证 prompt 变量一致性**

确认 agent.py 的 `prompt_vars` 中的 key 与 Langfuse prompt 中的 `{{...}}` 变量一一对应。

---

### Task 4: 部署验证

- [ ] **Step 1: Push + 部署 mem-v2**

```bash
git push && make deploy APP=agent-service GIT_REF=perf/deep-memory-optimize LANE=mem-v2
```

- [ ] **Step 2: 飞书 dev bot 发送测试消息**

在飞书 dev bot 发消息，检查：
1. 回复正常（不 500）
2. 回复中能体现 Journal 的情感基调
3. `make logs` 无 KeyError 或 prompt 变量缺失错误

- [ ] **Step 3: 查看 Langfuse trace**

用 langfuse skill 查看最新 trace，确认 inner_context 变量正确注入。
