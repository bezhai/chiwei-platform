# Context Architecture Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让赤尾对不同的人有不同的记忆和态度，用内心独白替代示例式行为锚点，让 per-user 差异自然涌现

**Architecture:** 新增 relationship_memory 表（per-user 自然语言关系记忆，afterthought 触发时提取）+ inner_monologue_log 表（替代 reply_style 的示例锚点）+ Life Engine tick prompt 扩充。过渡期 drift pipeline 保留，内心独白与 reply_style 并存。

**Tech Stack:** Python, SQLAlchemy, PostgreSQL, Langfuse, LangChain

**Spec:** `docs/superpowers/specs/2026-04-08-context-phase2-design.md`

---

## File Structure

| 文件 | 变更类型 | 职责 |
|------|---------|------|
| `apps/agent-service/app/orm/memory_models.py` | 修改 | 新增 RelationshipMemory + InnerMonologueLog 模型 |
| `apps/agent-service/app/orm/memory_crud.py` | 修改 | 新增关系记忆 + 内心独白 CRUD |
| `apps/agent-service/app/services/relationship_memory.py` | 新建 | 关系记忆提取逻辑 |
| `apps/agent-service/app/services/afterthought.py` | 修改 | 碎片生成后触发关系记忆提取 |
| `apps/agent-service/app/services/memory_context.py` | 修改 | 注入关系记忆到 inner_context |
| `apps/agent-service/app/services/inner_monologue.py` | 新建 | 内心独白生成逻辑 |
| `apps/agent-service/app/workers/monologue_worker.py` | 新建 | 内心独白 cron worker |
| `apps/agent-service/app/workers/unified_worker.py` | 修改 | 注册内心独白 cron |
| `apps/agent-service/app/services/bot_context.py` | 修改 | 加载内心独白替代 reply_style |
| `apps/agent-service/app/agents/domains/main/agent.py` | 修改 | prompt_vars 注入内心独白 |
| Langfuse prompts | 修改/新建 | relationship_extract, inner_monologue, main, life_engine_tick, context_builder |

---

### Task 1: relationship_memory + inner_monologue_log 表模型

**Files:**
- Modify: `apps/agent-service/app/orm/memory_models.py`
- Modify: `apps/agent-service/app/orm/memory_crud.py`

- [ ] **Step 1: 添加 RelationshipMemory 模型**

在 `memory_models.py` 末尾添加：

```python
class RelationshipMemory(Base):
    """关系记忆 — per-user 的自然语言关系描述，append-only"""

    __tablename__ = "relationship_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    memory_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # 'afterthought' / 'dream' / 'manual'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_rel_mem_persona_user_created", "persona_id", "user_id", created_at.desc()),
    )
```

- [ ] **Step 2: 添加 InnerMonologueLog 模型**

紧接着添加：

```python
class InnerMonologueLog(Base):
    """内心独白日志 — 替代 reply_style 的示例锚点，append-only"""

    __tablename__ = "inner_monologue_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    monologue: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # 'cron' / 'event'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 3: 添加 CRUD 函数**

在 `memory_crud.py` 末尾添加：

```python
async def save_relationship_memory(
    persona_id: str,
    user_id: str,
    user_name: str,
    memory_text: str,
    source: str,
) -> None:
    """写入关系记忆（append-only）"""
    from app.orm.memory_models import RelationshipMemory

    async with AsyncSessionLocal() as session:
        session.add(RelationshipMemory(
            persona_id=persona_id,
            user_id=user_id,
            user_name=user_name,
            memory_text=memory_text,
            source=source,
        ))
        await session.commit()


async def get_latest_relationship_memory(
    persona_id: str, user_id: str
) -> str | None:
    """获取某用户的最新关系记忆"""
    from app.orm.memory_models import RelationshipMemory

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RelationshipMemory.memory_text)
            .where(RelationshipMemory.persona_id == persona_id)
            .where(RelationshipMemory.user_id == user_id)
            .order_by(RelationshipMemory.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_relationship_memories_for_users(
    persona_id: str, user_ids: list[str]
) -> dict[str, str]:
    """批量获取多个用户的最新关系记忆，返回 {user_id: memory_text}"""
    from app.orm.memory_models import RelationshipMemory

    if not user_ids:
        return {}

    async with AsyncSessionLocal() as session:
        # 用 DISTINCT ON 取每个 user 的最新记录
        result = await session.execute(
            select(RelationshipMemory.user_id, RelationshipMemory.memory_text, RelationshipMemory.user_name)
            .where(RelationshipMemory.persona_id == persona_id)
            .where(RelationshipMemory.user_id.in_(user_ids))
            .distinct(RelationshipMemory.user_id)
            .order_by(RelationshipMemory.user_id, RelationshipMemory.created_at.desc())
        )
        return {row.user_id: row.memory_text for row in result.all()}


async def save_inner_monologue(
    persona_id: str,
    monologue: str,
    source: str,
) -> None:
    """写入内心独白（append-only）"""
    from app.orm.memory_models import InnerMonologueLog

    async with AsyncSessionLocal() as session:
        session.add(InnerMonologueLog(
            persona_id=persona_id,
            monologue=monologue,
            source=source,
        ))
        await session.commit()


async def get_latest_inner_monologue(persona_id: str) -> str | None:
    """获取最新的内心独白"""
    from app.orm.memory_models import InnerMonologueLog

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(InnerMonologueLog.monologue)
            .where(InnerMonologueLog.persona_id == persona_id)
            .order_by(InnerMonologueLog.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
```

- [ ] **Step 4: 提交建表 DDL**

通过 `/ops-db` skill 提交：

```sql
CREATE TABLE relationship_memory (
    id SERIAL PRIMARY KEY,
    persona_id VARCHAR(50) NOT NULL,
    user_id VARCHAR(100) NOT NULL,
    user_name VARCHAR(100) NOT NULL DEFAULT '',
    memory_text TEXT NOT NULL,
    source VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_rel_mem_persona_user_created
    ON relationship_memory (persona_id, user_id, created_at DESC);

CREATE TABLE inner_monologue_log (
    id SERIAL PRIMARY KEY,
    persona_id VARCHAR(50) NOT NULL,
    monologue TEXT NOT NULL,
    source VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_inner_monologue_persona_created
    ON inner_monologue_log (persona_id, created_at DESC);
```

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/orm/memory_models.py apps/agent-service/app/orm/memory_crud.py
git commit -m "feat(memory): add relationship_memory + inner_monologue_log models and CRUD"
```

---

### Task 2: 关系记忆提取服务

**Files:**
- Create: `apps/agent-service/app/services/relationship_memory.py`

- [ ] **Step 1: 创建关系记忆提取模块**

```python
"""关系记忆提取 — afterthought 碎片生成后，判断是否需要更新 per-user 关系记忆"""

import json
import logging

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import get_username
from app.orm.memory_crud import (
    get_relationship_memories_for_users,
    save_relationship_memory,
)

logger = logging.getLogger(__name__)


async def extract_relationship_updates(
    persona_id: str,
    chat_id: str,
    user_ids: list[str],
    messages_timeline: str,
) -> None:
    """从一段对话中提取关系记忆更新

    在 afterthought 生成 conversation 碎片后调用。
    让 LLM 判断对话中涉及的人是否有关系变化，有则写入 relationship_memory。
    """
    if not user_ids:
        return

    # 获取当前关系记忆
    current_memories = await get_relationship_memories_for_users(persona_id, user_ids)

    # 构建当前记忆上下文
    memory_lines = []
    for uid in user_ids:
        name = await get_username(uid) or uid[:6]
        mem = current_memories.get(uid)
        if mem:
            memory_lines.append(f"- {name}({uid}): {mem}")
        else:
            memory_lines.append(f"- {name}({uid}): （第一次互动，没有记忆）")

    prompt = get_prompt("relationship_extract")
    compiled = prompt.compile(
        messages=messages_timeline,
        current_memories="\n".join(memory_lines),
    )

    model = await ModelBuilder.build_chat_model(settings.diary_model)
    response = await model.ainvoke([{"role": "user", "content": compiled}])

    content = response.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()

    if not content or content.strip() == "[]":
        logger.info(f"[{persona_id}] No relationship updates for chat {chat_id}")
        return

    # 解析 JSON 输出
    try:
        updates = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"[{persona_id}] Failed to parse relationship extract: {content[:100]}")
        return

    for item in updates:
        if not isinstance(item, dict):
            continue
        if item.get("action") != "UPDATE":
            continue
        uid = item.get("user_id", "")
        name = item.get("user_name", "") or await get_username(uid) or uid[:6]
        memory = item.get("memory", "")
        if uid and memory:
            await save_relationship_memory(
                persona_id=persona_id,
                user_id=uid,
                user_name=name,
                memory_text=memory,
                source="afterthought",
            )
            logger.info(f"[{persona_id}] Relationship updated for {name}: {memory[:50]}...")
```

- [ ] **Step 2: 创建 Langfuse prompt `relationship_extract`**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py create-prompt '{
  "name": "relationship_extract",
  "type": "text",
  "labels": ["production"],
  "prompt": "你是赤尾的\"关系记忆管理\"。\n\n赤尾刚才参与了一段对话：\n{{messages}}\n\n赤尾对涉及的人当前的记忆：\n{{current_memories}}\n\n---\n\n判断：这次对话中，赤尾对哪些人的印象应该更新？\n\n规则：\n- 日常寒暄、重复话题、没什么新信息 → NO_UPDATE\n- 对方展示了新的性格特点、发生了有感情意义的事件、关系升温或降温 → 需要更新\n- 更新后的记忆像赤尾自己写的备忘录，3-5 句话，自然口吻，不要用结构化格式\n- 保留之前记忆中仍然有效的信息，在此基础上增减\n\n输出 JSON 数组（不要加 markdown 代码块）：\n[{\"user_id\": \"xxx\", \"user_name\": \"xxx\", \"action\": \"UPDATE\", \"memory\": \"更新后的完整记忆\"}, {\"user_id\": \"yyy\", \"action\": \"NO_UPDATE\"}]\n\n如果所有人都不需要更新，输出 []"
}'
```

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/services/relationship_memory.py
git commit -m "feat(memory): add relationship memory extraction service"
```

---

### Task 3: afterthought 接入关系记忆提取

**Files:**
- Modify: `apps/agent-service/app/services/afterthought.py`

- [ ] **Step 1: 在碎片生成后调用关系记忆提取**

在 `_generate_conversation_fragment` 函数末尾（当前 L193 打印日志之后），添加：

```python
    # 关系记忆提取（fire-and-forget，不阻塞主流程）
    try:
        from app.services.relationship_memory import extract_relationship_updates

        # 从消息中提取涉及的用户 ID（排除 bot 自身）
        unique_user_ids = list({
            m.user_id for m in messages
            if m.role == "user" and m.user_id
        })

        if unique_user_ids:
            await extract_relationship_updates(
                persona_id=persona_id,
                chat_id=chat_id,
                user_ids=unique_user_ids,
                messages_timeline=timeline,
            )
    except Exception as e:
        logger.warning(f"[{persona_id}] Relationship extract failed (non-fatal): {e}")
```

注意：`messages` 变量（原始消息列表）和 `timeline` 变量（格式化后的时间线字符串）在函数内已存在。`persona_id` 也已存在。

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/services/afterthought.py
git commit -m "feat(memory): afterthought triggers relationship memory extraction"
```

---

### Task 4: build_inner_context 注入关系记忆

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py`

- [ ] **Step 1: 在 build_inner_context 中添加关系记忆注入**

在 Life Engine 状态注入之后、最近碎片之前，添加关系记忆查询和注入：

```python
    # === 关系记忆（对当前对话者的印象）===
    if trigger_user_id and trigger_user_id != "__proactive__":
        from app.orm.memory_crud import get_latest_relationship_memory
        from app.orm.crud import get_username

        rel_memory = await get_latest_relationship_memory(persona_id, trigger_user_id)
        if rel_memory:
            name = trigger_username or await get_username(trigger_user_id) or trigger_user_id[:6]
            sections.append(f"关于 {name}：\n{rel_memory}")
```

这段代码插入到 `_build_life_state` 调用之后、碎片注入之前。

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py
git commit -m "feat(context): inject per-user relationship memory into inner_context"
```

---

### Task 5: 内心独白生成服务 + cron

**Files:**
- Create: `apps/agent-service/app/services/inner_monologue.py`
- Create: `apps/agent-service/app/workers/monologue_worker.py`
- Modify: `apps/agent-service/app/workers/unified_worker.py`

- [ ] **Step 1: 创建内心独白生成服务**

```python
"""内心独白生成 — 替代 reply_style 的示例锚点

定期生成赤尾此刻的内心感受，注入 system prompt 的 <voice> 段。
LLM 从感受自然涌现回复风格，不再模仿示例。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import get_bot_persona, get_plan_for_period
from app.orm.memory_crud import get_latest_inner_monologue, get_today_fragments, save_inner_monologue

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


async def generate_inner_monologue(persona_id: str) -> str | None:
    """生成赤尾此刻的内心独白"""
    # 1. 收集上下文
    persona = await get_bot_persona(persona_id)
    if not persona:
        return None

    # Life Engine 状态
    from app.services.life_engine import _load_state
    le_state = await _load_state(persona_id)
    current_state = le_state.current_state if le_state else "（状态未知）"
    response_mood = le_state.response_mood if le_state else ""

    # 当前时段的 schedule
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    schedule = await get_plan_for_period("daily", today, today, persona_id)
    schedule_text = schedule.content if schedule else "（今天没有安排）"

    # 最近碎片
    frags = await get_today_fragments(persona_id, grains=["conversation"])
    frag_text = "\n".join(f.content[:100] for f in frags[-3:]) if frags else "（今天还没跟人聊过）"

    # 2. 调用 LLM
    prompt = get_prompt("inner_monologue")
    compiled = prompt.compile(
        persona_name=persona.display_name,
        persona_lite=persona.persona_lite,
        current_state=current_state,
        response_mood=response_mood,
        schedule_segment=schedule_text,
        recent_fragments=frag_text,
        current_time=now.strftime("%H:%M"),
    )

    model = await ModelBuilder.build_chat_model(settings.identity_drift_model)
    response = await model.ainvoke([{"role": "user", "content": compiled}])

    content = response.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()

    if not content:
        logger.warning(f"[{persona_id}] Inner monologue generation returned empty")
        return None

    await save_inner_monologue(persona_id, content, source="cron")
    logger.info(f"[{persona_id}] Inner monologue generated: {content[:50]}...")
    return content
```

- [ ] **Step 2: 创建 cron worker**

```python
"""内心独白 cron worker"""

import logging

from app.config.config import settings

logger = logging.getLogger(__name__)


async def cron_generate_inner_monologue(ctx) -> None:
    """arq cron: 为每个 persona 生成内心独白"""
    if settings.lane and settings.lane != "prod":
        return

    from app.orm.crud import get_all_persona_ids
    from app.services.inner_monologue import generate_inner_monologue

    for persona_id in await get_all_persona_ids():
        try:
            result = await generate_inner_monologue(persona_id)
            if result:
                logger.info(f"[{persona_id}] Inner monologue generated: {len(result)} chars")
        except Exception as e:
            logger.error(f"[{persona_id}] Inner monologue generation failed: {e}", exc_info=True)
```

- [ ] **Step 3: 注册到 unified_worker.py**

在 cron_jobs 列表中，base_reply_style 那行附近添加：

```python
    # 7. 内心独白：每小时整点（与 base_reply_style 错开）
    cron(cron_generate_inner_monologue, hour=set(range(8, 24)), minute={30}, timeout=1800),
```

需要在文件头部添加 import：
```python
from app.workers.monologue_worker import cron_generate_inner_monologue
```

- [ ] **Step 4: 创建 Langfuse prompt `inner_monologue`**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py create-prompt '{
  "name": "inner_monologue",
  "type": "text",
  "labels": ["production"],
  "prompt": "你是{{persona_name}}。\n\n{{persona_lite}}\n\n现在是 {{current_time}}。\n\n你现在的状态：{{current_state}}\n你的心情：{{response_mood}}\n今天的安排：\n{{schedule_segment}}\n\n最近发生的事：\n{{recent_fragments}}\n\n---\n\n用一小段内心独白描述你此刻的感受。\n- 3-5 句话，像在脑子里自言自语\n- 写你真实会想的事，不是\"你应该怎么回消息\"\n- 自然、琐碎、有你的性格\n- 不要提及具体的群友名字"
}'
```

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/inner_monologue.py apps/agent-service/app/workers/monologue_worker.py apps/agent-service/app/workers/unified_worker.py
git commit -m "feat(monologue): add inner monologue generation service and cron"
```

---

### Task 6: 替换 reply_style 注入为内心独白

**Files:**
- Modify: `apps/agent-service/app/services/bot_context.py`
- Modify: `apps/agent-service/app/agents/domains/main/agent.py`
- Langfuse: 更新 `main` prompt

- [ ] **Step 1: BotContext 加载内心独白**

在 `bot_context.py` 的 BotContext 类中：

1. 添加 `_inner_monologue` 属性到 `__init__`：
```python
self._inner_monologue: str = ""
```

2. 在 `_load_persona` 末尾加载内心独白：
```python
    # 加载内心独白（替代 reply_style 的示例锚点）
    from app.orm.memory_crud import get_latest_inner_monologue
    self._inner_monologue = await get_latest_inner_monologue(self._persona_id) or ""
```

3. 添加 property：
```python
@property
def inner_monologue(self) -> str:
    return self._inner_monologue
```

- [ ] **Step 2: agent.py 注入内心独白**

在 `_build_and_stream` 中，L328（`prompt_vars["reply_style"]`）之后添加：

```python
    prompt_vars["inner_monologue"] = bot_ctx.inner_monologue
```

- [ ] **Step 3: 更新 Langfuse main prompt**

获取当前 production main prompt，做以下修改：

1. 将 `<reply-style>` 段替换为 `<voice>` 段：

旧：
```xml
<reply-style>

以下是你在群聊中的回复模式参考，参考这些例子的长度范围和语气自然波动即可：

{{reply_style}}

</reply-style>
```

新：
```xml
<voice>

赤尾此刻的内心：
{{inner_monologue}}

说话习惯（不是示例，只是底线约束）：
- 打字像发微信，不是写作文。一条消息通常一两句话。
- 困了就说短的，精神好可以多说几句，但也不会超过两三行。
- 不解释自己为什么这么说，说完就完了。

过渡期备用风格参考：
{{reply_style}}

</voice>
```

注意：过渡期保留 `{{reply_style}}`，等内心独白稳定后再移除。

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/services/bot_context.py apps/agent-service/app/agents/domains/main/agent.py
git commit -m "feat(voice): inject inner monologue into system prompt, keep reply_style as fallback"
```

---

### Task 7: Life Engine tick prompt 扩充 + context_builder 清理

**Files:**
- Langfuse: 更新 `life_engine_tick` prompt
- Langfuse: 更新 `context_builder` prompt

- [ ] **Step 1: 更新 life_engine_tick prompt**

获取当前 production `life_engine_tick`，修改 `<format>` 段中 `current_state` 的说明：

旧：
```
"current_state": "此刻的状态，用内心独白的方式写",
```

新：
```
"current_state": "此刻的状态，2-3 句话。写你在做什么、周围发生了什么、脑子里在想什么。自然带入最近聊天中有印象的事。",
```

- [ ] **Step 2: 更新 context_builder prompt**

获取当前 production `context_builder`，移除末尾的 `search_group_history` 引导语：

旧（末尾）：
```
如果需要更早的历史信息，使用 search_group_history 工具。
```

新（末尾）：
```
请回复 ⭐ 标记的消息。基于回复链的上下文理解触发消息的含义后再作答。
```

- [ ] **Step 3: Commit（无代码变更，仅记录 prompt 版本）**

```bash
git commit --allow-empty -m "chore(prompt): expand life_engine_tick output + clean context_builder"
```

---

### Task 8: 集成验证

- [ ] **Step 1: 本地测试**

```bash
cd apps/agent-service && uv run pytest tests/ -v --timeout=30
```

- [ ] **Step 2: 部署测试泳道**

```bash
make deploy APP=agent-service LANE=ctx-v4-p2 GIT_REF=<branch>
```

- [ ] **Step 3: 绑定群验证**

```
/ops bind TYPE=chat KEY=oc_a44255e98af05f1359aeb29eeb503536 LANE=ctx-v4-p2
```

验证项：
1. @赤尾 回复正常，有内心独白影响风格
2. 对不同用户的态度有差异（需要先积累一些关系记忆）
3. Langfuse trace 确认：system prompt 中有 `<voice>` 段 + `关于 xxx` 段
4. DB 中 relationship_memory 和 inner_monologue_log 有记录

- [ ] **Step 4: 查 DB 确认数据写入**

```sql
SELECT * FROM relationship_memory ORDER BY created_at DESC LIMIT 10;
SELECT * FROM inner_monologue_log ORDER BY created_at DESC LIMIT 5;
```

- [ ] **Step 5: 解绑清理**

```
/ops unbind TYPE=chat KEY=oc_a44255e98af05f1359aeb29eeb503536
make undeploy APP=agent-service LANE=ctx-v4-p2
```
