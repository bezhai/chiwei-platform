# Phase 1: 止血 + 三层记忆架构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解决回声放大（黄瓜问题）、注意力分散，建立三层记忆架构的第一层（简单占位）和第二层（对人和群的感觉 gestalt）。

**Architecture:** 重构 `memory_context.py`，将注入从"日记全文 + 印象列表"改为"内心状态(~200tok) + 感觉 gestalt(~200tok)"。印象蒸馏从描述性段落改为一句话感觉。新增群文化 gestalt。复用现有 `schedule_context` 作为第一层基础。

**Tech Stack:** Python 3.13, SQLAlchemy (async), PostgreSQL, Langfuse (prompt management), ArQ (cron workers), uv (package manager)

**Spec:** `docs/superpowers/specs/2026-03-25-deep-memory-optimize-design.md`

---

## 文件结构

### 修改的文件

| 文件 | 职责 | 改动 |
|------|------|------|
| `apps/agent-service/app/orm/models.py` | 数据模型 | 新增 `GroupCultureGestalt` 模型 |
| `apps/agent-service/app/orm/crud.py` | CRUD 操作 | 新增 gestalt 相关查询/写入函数 |
| `apps/agent-service/app/services/memory_context.py` | 记忆上下文构建 | **重写**：三层架构替代日记全文注入 |
| `apps/agent-service/app/workers/diary_worker.py` | 日记生成+印象后处理 | 改造 `post_process_impressions`，新增群文化蒸馏 |
| `apps/agent-service/app/agents/domains/main/agent.py` | 主对话流程 | 更新 `prompt_vars` 组装方式 |

### 新增的文件

| 文件 | 职责 |
|------|------|
| `apps/agent-service/app/services/inner_state.py` | 第一层：赤尾内心状态构建 |
| `apps/agent-service/tests/unit/test_memory_context.py` | memory_context 单元测试 |
| `apps/agent-service/tests/unit/test_inner_state.py` | inner_state 单元测试 |
| `apps/agent-service/tests/unit/test_impression_distill.py` | 印象蒸馏单元测试 |

### Langfuse Prompts（外部，通过 langfuse skill 更新）

| Prompt 名称 | 改动 |
|-------------|------|
| `diary_generation` | 增加自引用抑制指导 |
| `diary_extract_impressions` | 改为一句话 gestalt 蒸馏 |
| 新增 `group_culture_distill` | 群文化 gestalt 蒸馏 |

---

## Task 1: GroupCultureGestalt 数据模型

**Files:**
- Modify: `apps/agent-service/app/orm/models.py`
- Modify: `apps/agent-service/app/orm/crud.py`

- [ ] **Step 1: 在 models.py 中新增 GroupCultureGestalt 模型**

在 `PersonImpression` 模型之后添加：

```python
class GroupCultureGestalt(Base):
    """群文化 gestalt — 赤尾对一个群的整体感觉，一句话"""

    __tablename__ = "group_culture_gestalt"

    chat_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    gestalt_text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 2: 在 crud.py 中新增 CRUD 函数**

```python
from .models import GroupCultureGestalt

async def upsert_group_culture_gestalt(chat_id: str, gestalt_text: str) -> None:
    """写入/更新群文化 gestalt"""
    async with AsyncSessionLocal() as session:
        existing = await session.get(GroupCultureGestalt, chat_id)
        if existing:
            existing.gestalt_text = gestalt_text
            # updated_at 由 ORM onupdate=func.now() 自动更新
        else:
            session.add(GroupCultureGestalt(
                chat_id=chat_id, gestalt_text=gestalt_text
            ))
        await session.commit()


async def get_group_culture_gestalt(chat_id: str) -> str:
    """获取群文化 gestalt，无则返回空字符串"""
    async with AsyncSessionLocal() as session:
        result = await session.get(GroupCultureGestalt, chat_id)
        return result.gestalt_text if result else ""
```

- [ ] **Step 3: 通过 PaaS API 创建数据库表**

使用 `/ops-db` skill 执行：

```sql
CREATE TABLE IF NOT EXISTS group_culture_gestalt (
    chat_id VARCHAR(100) PRIMARY KEY,
    gestalt_text TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

- [ ] **Step 4: 验证表创建成功**

使用 `/ops-db` 查询：`SELECT * FROM group_culture_gestalt LIMIT 1;`

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/orm/models.py apps/agent-service/app/orm/crud.py
git commit -m "feat(memory): add GroupCultureGestalt model and CRUD"
```

---

## Task 2: 第一层 — 赤尾内心状态构建

**Files:**
- Create: `apps/agent-service/app/services/inner_state.py`
- Create: `apps/agent-service/tests/unit/test_inner_state.py`

- [ ] **Step 1: 写测试**

```python
# tests/unit/test_inner_state.py
import pytest
from unittest.mock import AsyncMock, patch
from app.services.inner_state import build_inner_state


@pytest.mark.asyncio
async def test_build_inner_state_with_schedule():
    """有日程时，内心状态包含日程内容"""
    mock_schedule_content = "早上看了芙莉莲第8集，下午想出门走走"
    with patch(
        "app.services.inner_state.get_plan_for_period",
        new_callable=AsyncMock,
        return_value=type("Obj", (), {"content": mock_schedule_content, "mood": "开心", "energy_level": 4})(),
    ):
        result = await build_inner_state()
    assert "芙莉莲" in result
    assert len(result) > 0


@pytest.mark.asyncio
async def test_build_inner_state_without_schedule():
    """无日程时，返回基于时间的基本状态"""
    with patch(
        "app.services.inner_state.get_plan_for_period",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await build_inner_state()
    assert len(result) > 0  # 至少有基于时间的状态
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_inner_state.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 实现 inner_state.py**

```python
"""第一层：赤尾的内心状态

基于当前时间和日程，构建赤尾此刻的内心状态。
Phase 1 为简单占位，Phase 2 接入生活引擎后丰富。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.orm.crud import get_plan_for_period

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 时段 → 基本状态映射
_TIME_VIBES = {
    (0, 7): "深夜，有点迷迷糊糊的",
    (7, 10): "刚醒来，还没完全清醒",
    (10, 12): "上午，精力还不错",
    (12, 14): "中午，刚吃完饭有点犯困",
    (14, 17): "下午，状态一般般",
    (17, 19): "傍晚，精力开始恢复",
    (19, 22): "晚上，比较放松",
    (22, 24): "夜里，有点困但还不想睡",
}

_WEEKDAY_VIBES = {
    0: "周一",
    1: "周二",
    2: "周三",
    3: "周四",
    4: "周五，快周末了",
    5: "周六，休息日",
    6: "周日，明天又要上班了",
}


def _get_time_vibe(hour: int) -> str:
    for (start, end), vibe in _TIME_VIBES.items():
        if start <= hour < end:
            return vibe
    return "深夜"


async def build_inner_state() -> str:
    """构建赤尾此刻的内心状态（第一层）

    Phase 1: 基于时间 + 今日手帐
    Phase 2: 接入生活引擎（天气、番剧、音乐等）

    Returns:
        内心状态文本，约 100-200 tokens
    """
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    hour = now.hour
    weekday = now.weekday()

    # 基础时间感
    time_vibe = _get_time_vibe(hour)
    weekday_vibe = _WEEKDAY_VIBES.get(weekday, "")

    # 查今日手帐（已有的日程系统）
    daily = await get_plan_for_period("daily", today, today)

    if daily and daily.content:
        # 有手帐时，直接用手帐内容作为内心状态
        # 手帐已经是赤尾第一人称的、包含心情和活动的文本
        mood_hint = f"（心情：{daily.mood}）" if daily.mood else ""
        return f"现在是{weekday_vibe}的{time_vibe}。{mood_hint}\n{daily.content}"

    # 无手帐时，返回基于时间的最小状态
    return f"现在是{weekday_vibe}的{time_vibe}。今天没什么特别的安排。"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_inner_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/inner_state.py apps/agent-service/tests/unit/test_inner_state.py
git commit -m "feat(memory): add Layer 1 inner state builder"
```

---

## Task 3: 印象蒸馏改造 — 从描述到感觉 gestalt

**Files:**
- Modify: `apps/agent-service/app/workers/diary_worker.py` (`post_process_impressions`)
- Create: `apps/agent-service/tests/unit/test_impression_distill.py`

- [ ] **Step 1: 写测试**

```python
# tests/unit/test_impression_distill.py
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_gestalt_impression_is_short():
    """蒸馏后的印象应该是一句话（≤50字）"""
    fake_llm_response = json.dumps([
        {"user_id": "uid_001", "impression_text": "群里的指挥官，嘴硬心软，跟他互动很轻松"}
    ])

    mock_response = MagicMock()
    mock_response.content = fake_llm_response

    with patch(
        "app.workers.diary_worker.ModelBuilder.build_chat_model",
        new_callable=AsyncMock,
        return_value=MagicMock(ainvoke=AsyncMock(return_value=mock_response)),
    ), patch(
        "app.workers.diary_worker.get_all_impressions_for_chat",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "app.workers.diary_worker.upsert_person_impression",
        new_callable=AsyncMock,
    ) as mock_upsert:
        from app.workers.diary_worker import post_process_impressions

        await post_process_impressions(
            chat_id="chat_001",
            diary_content="A哥今天又在组织角色分配",
            user_names={"uid_001": "A哥"},
        )

        mock_upsert.assert_called_once()
        impression_text = mock_upsert.call_args[1].get("impression_text") or mock_upsert.call_args[0][2]
        assert len(impression_text) <= 60, f"Impression too long: {len(impression_text)} chars"
```

- [ ] **Step 2: 运行测试确认当前行为**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_impression_distill.py -v`

- [ ] **Step 3: 更新 Langfuse prompt `diary_extract_impressions`**

使用 `/langfuse` skill 更新 prompt。核心变更：

旧指导："提取关于每个人的描述性信息"
新指导（注意变量名必须与代码中 `compile()` 的参数名一致：`diary`, `existing_impressions`, `user_mapping`）：

```
你刚写完今天的日记。现在闭上眼睛，想想今天日记中提到的每个人。

对每个人，写下你脑子里浮现的第一个感觉——用一句话，不超过 30 个字。

不是总结他做了什么。是你对他的"感觉"。
- 好的例子："群里的指挥官，嘴硬心软，跟他互动很轻松"
- 不好的例子："经常组织群活动，喜欢发起角色分配挑战，观察力很强"

如果你对某人印象模糊，就用模糊的语气写——"好像..."、"不太确定但感觉..."。
不要编造确定性。

你之前对他们的印象（如果有的话，结合今天的日记更新）：
{existing_impressions}

用户 ID 映射（输出 JSON 时使用这些 user_id）：
{user_mapping}

今天的日记：
{diary}

输出 JSON 数组：[{"user_id": "对应的user_id", "impression_text": "一句话感觉"}]
```

**注意**：代码中 `post_process_impressions` 的 `compile()` 调用无需修改——变量名 `diary`, `existing_impressions`, `user_mapping` 保持不变，只是 prompt 的指导文字变了。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_impression_distill.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/tests/unit/test_impression_distill.py
git commit -m "feat(memory): add impression gestalt distillation tests"
```

---

## Task 4: 群文化 gestalt 蒸馏

**Files:**
- Modify: `apps/agent-service/app/workers/diary_worker.py`

- [ ] **Step 1: 在 diary_worker.py 中新增 `post_process_group_culture` 函数**

在 `post_process_impressions` 之后添加：

```python
async def post_process_group_culture(
    chat_id: str,
    diary_content: str,
) -> None:
    """从日记中蒸馏群文化 gestalt

    一句话描述赤尾对这个群的整体感觉。
    """
    from app.orm.crud import get_group_culture_gestalt, upsert_group_culture_gestalt

    existing_gestalt = await get_group_culture_gestalt(chat_id)

    prompt_template = get_prompt("group_culture_distill")
    compiled_prompt = prompt_template.compile(
        diary=diary_content,
        previous_gestalt=existing_gestalt or "（这是第一次写，没有参考）",
    )

    model = await ModelBuilder.build_chat_model(settings.diary_model)
    response = await model.ainvoke(
        [{"role": "user", "content": compiled_prompt}],
    )

    raw = response.content
    if isinstance(raw, list):
        raw = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in raw
        )
    raw = raw.strip()

    if raw:
        await upsert_group_culture_gestalt(chat_id, raw)
        logger.info(f"Group culture gestalt updated for {chat_id}: {raw[:50]}")
```

- [ ] **Step 2: 在 `generate_diary_for_chat` 中调用群文化蒸馏**

在现有 `post_process_impressions` 调用之后添加：

```python
    # 9. 后处理：蒸馏群文化 gestalt
    try:
        await post_process_group_culture(chat_id, diary_content)
    except Exception as e:
        logger.error(f"Group culture distill failed for {chat_id}: {e}")
```

- [ ] **Step 3: 在 Langfuse 中创建 `group_culture_distill` prompt**

使用 `/langfuse` skill 创建新 prompt：

```
你刚写完今天的日记。想想这个群给你的整体感觉。

用一两句话描述（不超过 50 字），像跟朋友随口说的那种：
- "最放飞的一个群，二次元浓度拉满，大家都很能玩"
- "比较安静的群，偶尔有人聊技术，氛围很舒服"

你之前对这个群的感觉（如果有）：
{previous_gestalt}

今天的日记：
{diary}

你对这个群的感觉（一两句话）：
```

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/workers/diary_worker.py
git commit -m "feat(memory): add group culture gestalt distillation"
```

---

## Task 5: 重构 memory_context.py — 三层架构

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py`
- Create: `apps/agent-service/tests/unit/test_memory_context.py`

这是本次最关键的改动。

- [ ] **Step 1: 写测试**

```python
# tests/unit/test_memory_context.py
import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_build_memory_context_group():
    """群聊场景：返回第一层 + 第二层，不含日记全文"""
    with patch(
        "app.services.memory_context.build_inner_state",
        new_callable=AsyncMock,
        return_value="周三下午，有点犯困。今天没什么特别的安排。",
    ), patch(
        "app.services.memory_context.get_group_culture_gestalt",
        new_callable=AsyncMock,
        return_value="最放飞的群，二次元浓度拉满",
    ), patch(
        "app.services.memory_context.get_impressions_for_users",
        new_callable=AsyncMock,
        return_value=[
            type("Imp", (), {"user_id": "u1", "impression_text": "群里的指挥官，嘴硬心软"})(),
        ],
    ), patch(
        "app.services.memory_context.get_username",
        new_callable=AsyncMock,
        return_value="A哥",
    ):
        from app.services.memory_context import build_memory_context

        result = await build_memory_context(
            chat_id="chat_001",
            chat_type="group",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
        )

    # 应该包含内心状态
    assert "犯困" in result
    # 应该包含群感觉
    assert "放飞" in result
    # 应该包含人物 gestalt
    assert "指挥官" in result
    # 不应该包含日记全文
    assert "---" not in result
    # 总长度应该远小于 2000
    assert len(result) < 800


@pytest.mark.asyncio
async def test_build_memory_context_p2p():
    """私聊场景：应包含跨群印象"""
    with patch(
        "app.services.memory_context.build_inner_state",
        new_callable=AsyncMock,
        return_value="周末，心情不错。",
    ), patch(
        "app.services.memory_context.get_cross_group_impressions",
        new_callable=AsyncMock,
        return_value=[
            (type("Imp", (), {"impression_text": "聊动画很带劲"})(), "KA群"),
        ],
    ):
        from app.services.memory_context import build_memory_context

        result = await build_memory_context(
            chat_id="p2p_001",
            chat_type="p2p",
            user_ids=["u1"],
            trigger_user_id="u1",
            trigger_username="A哥",
        )

    assert "心情不错" in result
    assert "动画" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_memory_context.py -v`
Expected: FAIL — `build_memory_context` 不存在

- [ ] **Step 3: 重写 memory_context.py**

```python
"""记忆上下文构建服务 — 三层架构

第一层：赤尾的内心状态（始终存在，~200 tokens）
第二层：对人和群的感觉 gestalt（按场景加载，~200 tokens）
第三层：自然联想（对话中通过 load_memory 按需触发，不在此处注入）

旧的 build_diary_context / build_impression_context 保留为向后兼容，
新入口为 build_memory_context。
"""

import logging

from app.orm.crud import (
    get_cross_group_impressions,
    get_group_culture_gestalt,
    get_impressions_for_users,
    get_username,
)
from app.services.inner_state import build_inner_state

logger = logging.getLogger(__name__)

MAX_IMPRESSION_USERS = 10
MAX_CROSS_GROUP_IMPRESSIONS = 5


async def build_memory_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str,
    trigger_username: str,
) -> str:
    """构建三层记忆上下文（新入口）

    Args:
        chat_id: 群/私聊 ID
        chat_type: "group" 或 "p2p"
        user_ids: 当前对话中出现的用户 ID 列表
        trigger_user_id: 触发者 user_id
        trigger_username: 触发者用户名

    Returns:
        组装好的记忆上下文文本，注入 system prompt
    """
    sections = []

    # === 第一层：赤尾的内心 ===
    inner = await build_inner_state()
    if inner:
        sections.append(f"你现在的内心：\n{inner}")

    # === 第二层：对人和群的感觉 ===
    if chat_type == "group":
        # 群感觉
        group_gestalt = await get_group_culture_gestalt(chat_id)
        if group_gestalt:
            sections.append(f"你对这个群的感觉：{group_gestalt}")

        # 对话者的感觉
        if user_ids:
            people_lines = await _build_people_gestalt(chat_id, user_ids)
            if people_lines:
                sections.append("你对当前对话中出现的人的感觉：\n" + "\n".join(people_lines))
    else:
        # 私聊：跨群印象
        cross_lines = await _build_cross_group_gestalt(trigger_user_id, trigger_username)
        if cross_lines:
            sections.append(cross_lines)

    return "\n\n".join(sections)


async def _build_people_gestalt(chat_id: str, user_ids: list[str]) -> list[str]:
    """构建对话者的感觉 gestalt 列表"""
    impressions = await get_impressions_for_users(chat_id, user_ids[:MAX_IMPRESSION_USERS])
    if not impressions:
        return []
    lines = []
    for imp in impressions:
        name = await get_username(imp.user_id) or imp.user_id[:8]
        lines.append(f"- {name}：{imp.impression_text}")
    return lines


async def _build_cross_group_gestalt(user_id: str, trigger_username: str) -> str:
    """构建跨群人物 gestalt（私聊场景）"""
    rows = await get_cross_group_impressions(user_id, limit=MAX_CROSS_GROUP_IMPRESSIONS)
    if not rows:
        return ""
    lines = []
    for imp, group_name in rows:
        lines.append(f"- （{group_name}）{imp.impression_text}")
    return f"你对 {trigger_username} 的感觉：\n" + "\n".join(lines)


# === 向后兼容：保留旧函数签名，内部委托新逻辑 ===
# 旧函数在 Phase 1 完成后可以删除

async def build_diary_context(chat_id: str) -> str:
    """[已废弃] 旧的日记注入，三层架构下不再直接注入日记"""
    return ""


async def build_impression_context(chat_id: str, user_ids: list[str]) -> str:
    """[已废弃] 旧的印象注入，由 build_memory_context 替代"""
    return ""


async def build_cross_group_impression_context(
    user_id: str, trigger_username: str
) -> str:
    """[已废弃] 旧的跨群印象注入，由 build_memory_context 替代"""
    return ""
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_memory_context.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py apps/agent-service/tests/unit/test_memory_context.py
git commit -m "feat(memory): rewrite memory_context with three-layer architecture"
```

---

## Task 6: 更新 agent.py — 使用新的记忆上下文

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/agent.py`

- [ ] **Step 1: 阅读当前 agent.py 中记忆注入逻辑**

Read: `apps/agent-service/app/agents/domains/main/agent.py` 行 259-320 的上下文构建部分。

- [ ] **Step 2: 替换记忆注入为 `build_memory_context` 调用**

将原来分散的 `build_diary_context` + `build_impression_context` + `build_cross_group_impression_context` 三次调用，替换为单次 `build_memory_context` 调用：

```python
# 导入新函数
from app.services.memory_context import build_memory_context

# 替换原来的多次调用为：
memory_text = await build_memory_context(
    chat_id=chat_id,
    chat_type=chat_type,
    user_ids=chain_user_ids,
    trigger_user_id=trigger_user_id,
    trigger_username=trigger_username,
)
if memory_text:
    context_lines.append(memory_text)
```

移除原来的 `build_diary_context`、`build_impression_context`、`build_cross_group_impression_context` 的 import 和调用。

- [ ] **Step 3: 将 `schedule_context` 设为空字符串，避免重复注入**

`build_inner_state()` 内部已调用 `get_plan_for_period("daily", ...)` 获取今日手帐，与 `build_schedule_context()` 逻辑完全相同。如果两者都注入，日程内容会出现两次。

**决策**：将 `prompt_vars["schedule_context"]` 直接设为 `""`。不修改 Langfuse 模板（模板中 `{schedule_context}` 占位符为空即可），改动最小、风险最低。

```python
# 日程信息已融入第一层内心状态，不再单独注入
prompt_vars["schedule_context"] = ""
```

移除对 `build_schedule_context` 的 import 和调用。

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/agent.py
git commit -m "feat(memory): integrate three-layer memory context into agent"
```

---

## Task 7: 更新 Langfuse Prompts

**Files:** Langfuse 外部系统（通过 langfuse skill 操作）

- [ ] **Step 1: 更新 `diary_generation` prompt — 增加自引用抑制**

使用 `/langfuse` skill 读取当前 prompt，在其中增加以下指导段落：

```
【重要】关于自引用抑制：
在消息时间线中，标记为"赤尾"的是你自己在群里说过的话。
你自己说过的话不是新发现——它们来自你之前的记忆。
如果你在群里提到了某个话题（比如某人的某个特点），不要在日记中再次强调它，
除非今天有真正来自群友的新信息让你对此有了新的感受。

只记让你有感觉的事。被提到 10 次但没有触动你的事，不如被提到 1 次但让你有情绪波动的事。
```

- [ ] **Step 2: 更新 `diary_extract_impressions` prompt — 改为 gestalt 蒸馏**

（见 Task 3 Step 3 中的完整 prompt 文本）

- [ ] **Step 3: 创建 `group_culture_distill` prompt**

（见 Task 4 Step 3 中的完整 prompt 文本）

- [ ] **Step 4: 验证 prompts 更新成功**

使用 `/langfuse` skill 分别读取三个 prompt 确认内容正确。

- [ ] **Step 5: Commit（无代码改动，记录 prompt 版本）**

```bash
git commit --allow-empty -m "chore(langfuse): update diary/impression/culture prompts for Phase 1"
```

---

## Task 8: 端到端验证

- [ ] **Step 1: 运行全部测试（新增 + 回归）**

Run: `cd apps/agent-service && uv run pytest tests/ -v`
Expected: ALL PASS（包括新增的 3 个测试文件和已有的 test_langfuse.py 等）

- [ ] **Step 2: 手动触发日记生成验证蒸馏效果**

在 Python REPL 中调用原子函数（取一个活跃群的昨天日期）：

```python
from app.workers.diary_worker import generate_diary_for_chat
from datetime import date, timedelta

yesterday = date.today() - timedelta(days=1)
result = await generate_diary_for_chat("TARGET_CHAT_ID", yesterday)
```

验证：
- 日记内容中不再因自引用而反复强调同一细节
- `person_impression` 表中新写入的印象是一句话 gestalt（≤50 字）
- `group_culture_gestalt` 表中有新写入的群感觉

- [ ] **Step 3: 验证对话中的记忆注入**

部署到测试泳道，在飞书 dev bot 发消息，观察：
- 赤尾的回复不再被无关日记细节污染
- 赤尾仍然能表现出认识群友（gestalt 在起作用）
- 总注入 token 量明显减少

- [ ] **Step 4: Commit + Push**

```bash
git push origin perf/deep-memory-optimize
```

---

## 依赖关系

```
Task 1 (数据模型) ───→ Task 4 (群文化蒸馏，依赖 GroupCultureGestalt)
                  ───→ Task 5 (memory_context，依赖 get_group_culture_gestalt)

Task 2 (第一层) ─────→ Task 5 (memory_context，依赖 build_inner_state)

Task 3 (印象蒸馏) ───→ Task 5 (memory_context，依赖 gestalt 格式)

Task 7 (Langfuse) ← 与 Task 1-4 并行，不阻塞代码

    Task 1 ─┐
    Task 2 ─┼─→ Task 5 → Task 6 → Task 8
    Task 3 ─┤
    Task 4 ─┘
    Task 7 ─────────────────────────→ Task 8
```

**Task 1, 2, 3 可以三路并行。Task 4 依赖 Task 1。Task 7 独立于代码改动。**
