# Context Architecture Phase 1: 瘦身 + 落库

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 system prompt 从 8000+ 字符瘦身到 ~3000 字符，reply_style 落库审计，移除主 agent 搜索工具

**Architecture:** 移除 inner-context 中的 schedule/碎片/daily dream 直接注入（Life Engine 已消化），reply_style 从 Redis 迁到 DB（append-only），主 agent 工具集从 11 个精简到 7 个

**Tech Stack:** Python, SQLAlchemy, PostgreSQL, Langfuse, LangChain

**Spec:** `docs/superpowers/specs/2026-04-08-context-architecture-redesign.md`

---

## File Structure

| 文件 | 变更类型 | 职责 |
|------|---------|------|
| `apps/agent-service/app/orm/memory_models.py` | 修改 | 新增 ReplyStyleLog 模型 |
| `apps/agent-service/app/orm/memory_crud.py` | 修改 | 新增 reply_style CRUD |
| `apps/agent-service/app/services/identity_drift.py` | 修改 | Redis→DB 读写 |
| `apps/agent-service/app/services/memory_context.py` | 修改 | 移除 schedule/碎片/dream 注入 |
| `apps/agent-service/app/services/bot_context.py` | 修改 | reply_style 读取链路 |
| `apps/agent-service/app/agents/domains/main/tools.py` | 修改 | 精简工具集 |
| `apps/agent-service/app/agents/core/agent.py` | 修改 | 添加工具调用限制 |
| `apps/agent-service/tests/unit/test_reply_style_log.py` | 新增 | reply_style 落库测试 |
| `apps/agent-service/tests/unit/test_inner_context.py` | 新增 | inner-context 瘦身测试 |

---

### Task 1: reply_style_log 表模型

**Files:**
- Modify: `apps/agent-service/app/orm/memory_models.py`
- Modify: `apps/agent-service/app/orm/memory_crud.py`
- Test: `apps/agent-service/tests/unit/test_reply_style_log.py`

- [ ] **Step 1: 写 ReplyStyleLog 模型**

在 `memory_models.py` 末尾添加：

```python
class ReplyStyleLog(Base):
    """Reply Style 审计日志 — 每次漂移 INSERT 一行，append-only"""

    __tablename__ = "reply_style_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    style_text: Mapped[str] = mapped_column(Text, nullable=False)
    observation: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # 'base' / 'drift' / 'manual'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 2: 写 CRUD 函数**

在 `memory_crud.py` 末尾添加：

```python
async def save_reply_style(
    persona_id: str,
    style_text: str,
    source: str,
    observation: str | None = None,
) -> None:
    """写入 reply_style 审计日志（append-only）"""
    from app.orm.memory_models import ReplyStyleLog

    async with AsyncSessionLocal() as session:
        session.add(ReplyStyleLog(
            persona_id=persona_id,
            style_text=style_text,
            source=source,
            observation=observation,
        ))
        await session.commit()


async def get_latest_reply_style(persona_id: str) -> str | None:
    """获取最新的 reply_style"""
    from app.orm.memory_models import ReplyStyleLog

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ReplyStyleLog.style_text)
            .where(ReplyStyleLog.persona_id == persona_id)
            .order_by(ReplyStyleLog.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row
```

- [ ] **Step 3: 写测试**

```python
# tests/unit/test_reply_style_log.py
import pytest
from app.orm.memory_crud import save_reply_style, get_latest_reply_style


@pytest.mark.asyncio
async def test_save_and_get_reply_style():
    await save_reply_style("akao", "style v1", "base")
    await save_reply_style("akao", "style v2", "drift", observation="太冷了")

    latest = await get_latest_reply_style("akao")
    assert latest == "style v2"


@pytest.mark.asyncio
async def test_get_latest_returns_none_when_empty():
    result = await get_latest_reply_style("nonexistent")
    assert result is None
```

- [ ] **Step 4: 提交 DDL 建表**

通过 `/ops-db` skill 提交：

```sql
CREATE TABLE reply_style_log (
    id SERIAL PRIMARY KEY,
    persona_id VARCHAR(50) NOT NULL,
    style_text TEXT NOT NULL,
    observation TEXT,
    source VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_reply_style_log_persona_created
    ON reply_style_log (persona_id, created_at DESC);
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_reply_style_log.py -v
```

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/orm/memory_models.py apps/agent-service/app/orm/memory_crud.py apps/agent-service/tests/unit/test_reply_style_log.py
git commit -m "feat(memory): add reply_style_log table for audit trail"
```

---

### Task 2: reply_style 读写迁移到 DB

**Files:**
- Modify: `apps/agent-service/app/services/identity_drift.py`
- Modify: `apps/agent-service/app/services/memory_context.py`
- Modify: `apps/agent-service/app/services/bot_context.py`

- [ ] **Step 1: 修改 identity_drift.py — 基线写入改 DB**

找到 `set_base_reply_style` 函数（约 L55-59），当前写 Redis：

```python
# 旧代码
await redis_client.set(f"reply_style:__base__:{persona_id}", style, ex=43200)
```

替换为：

```python
from app.orm.memory_crud import save_reply_style

await save_reply_style(persona_id, style, source="base")
```

- [ ] **Step 2: 修改 identity_drift.py — 漂移写入改 DB**

找到 `_run_drift` 中写 Redis 的地方（约 L107-114），当前：

```python
await redis_client.hset(f"reply_style:{chat_id}:{persona_id}", mapping={
    "state": style,
    "updated_at": now.isoformat(),
})
await redis_client.expire(f"reply_style:{chat_id}:{persona_id}", ttl)
```

替换为（注意：去掉 chat_id 维度，改为 per-persona only）：

```python
from app.orm.memory_crud import save_reply_style

await save_reply_style(
    persona_id,
    style,
    source="drift",
    observation=observation_report,
)
```

- [ ] **Step 3: 修改 memory_context.py — 读取改 DB**

找到 `get_reply_style` 函数（约 L143-157），当前三层 Redis fallback。替换为：

```python
async def get_reply_style(persona_id: str, default_style: str = "") -> str:
    """获取 reply_style：DB 最新记录 → DB 默认值"""
    from app.orm.memory_crud import get_latest_reply_style

    latest = await get_latest_reply_style(persona_id)
    if latest:
        return latest
    return default_style
```

- [ ] **Step 4: 修改 bot_context.py — 去掉 chat_id 参数**

找到 `_load_persona` 中调用 `get_reply_style` 的地方（约 L116-118）：

```python
# 旧代码
self._reply_style = await get_reply_style(
    self.chat_id, self._persona_id, default_style
)

# 新代码
self._reply_style = await get_reply_style(
    self._persona_id, default_style
)
```

- [ ] **Step 5: 删除 Redis 相关的旧函数**

在 `memory_context.py` 中删除：
- `get_identity_state()` — Redis 读取 per-chat 漂移
- `set_base_reply_style()` — Redis 写入基线

在 `identity_drift.py` 中删除：
- 所有 `redis_client.set/hset/get/hget` 相关 reply_style 的调用

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/services/identity_drift.py apps/agent-service/app/services/memory_context.py apps/agent-service/app/services/bot_context.py
git commit -m "feat(drift): migrate reply_style from Redis to DB (append-only audit)"
```

---

### Task 3: 瘦身 inner-context

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py`
- Test: `apps/agent-service/tests/unit/test_inner_context.py`

- [ ] **Step 1: 移除 schedule 直接注入**

在 `build_inner_context` 中，删除整个 `_build_today_state` 调用块（约 L108-110）：

```python
# 删除以下代码
today_state = await _build_today_state(persona_id)
if today_state:
    sections.append(f"你今天的基调：\n{today_state}")
```

同时删除 `_build_today_state` 函数定义（约 L26-33）。

- [ ] **Step 2: 移除 conversation 碎片直接注入**

在 `build_inner_context` 中，删除整个碎片注入块（约 L117-127）：

```python
# 删除以下代码
today_frags = await get_today_fragments(persona_id, grains=["conversation"])
if chat_type == "group":
    visible_frags = _filter_fragments_for_group(today_frags, chat_id)
else:
    visible_frags = today_frags
if visible_frags:
    frag_text = _format_fragment_section(visible_frags, MAX_FRAGMENT_SECTION_CHARS)
    if frag_text:
        sections.append(f"脑子里的东西（今天的经历）：\n{frag_text}")
```

- [ ] **Step 3: 移除 daily dream 直接注入**

删除 distant fragments 块（约 L129-133）：

```python
# 删除以下代码
distant_frags = await get_recent_fragments_by_grain(persona_id, "daily", limit=3)
if distant_frags:
    distant_text = _format_fragment_section(distant_frags, MAX_DISTANT_SECTION_CHARS)
    if distant_text:
        sections.append(f"更远的记忆：\n{distant_text}")
```

- [ ] **Step 4: 清理不再使用的常量和函数**

删除：
- `MAX_FRAGMENT_SECTION_CHARS = 3000`
- `MAX_DISTANT_SECTION_CHARS = 800`
- `_format_fragment_section()` 函数
- `_filter_fragments_for_group()` 函数
- `_MEMORY_RECALL_HINT` 常量及其注入（recall 工具也被移除了）

- [ ] **Step 5: 验证瘦身后的 build_inner_context**

瘦身后函数应该只剩：

```python
async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str,
    trigger_username: str,
    persona_id: str,
    chat_name: str = "",
    *,
    is_proactive: bool = False,
    proactive_stimulus: str = "",
) -> str:
    sections: list[str] = []

    # 场景提示
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

    # Life Engine 状态（唯一的状态来源）
    life_state = await _build_life_state(persona_id)
    if life_state:
        sections.append(life_state)

    return "\n\n".join(sections)
```

- [ ] **Step 6: 写测试确认瘦身效果**

```python
# tests/unit/test_inner_context.py
import pytest
from unittest.mock import AsyncMock, patch
from app.services.memory_context import build_inner_context


@pytest.mark.asyncio
async def test_inner_context_only_contains_scene_and_life_state():
    with patch("app.services.memory_context._build_life_state", new_callable=AsyncMock) as mock_life:
        mock_life.return_value = "你此刻的状态：在桌前整理照片\n你的心情：平静"

        result = await build_inner_context(
            chat_id="oc_test",
            chat_type="group",
            user_ids=["user1"],
            trigger_user_id="user1",
            trigger_username="冯宇林",
            persona_id="akao",
            chat_name="测试群",
        )

        assert "测试群" in result
        assert "冯宇林" in result
        assert "在桌前整理照片" in result
        # 不应包含 schedule、碎片、daily dream
        assert "今天的基调" not in result
        assert "脑子里的东西" not in result
        assert "更远的记忆" not in result


@pytest.mark.asyncio
async def test_inner_context_total_length_under_1000():
    with patch("app.services.memory_context._build_life_state", new_callable=AsyncMock) as mock_life:
        mock_life.return_value = "你此刻的状态：测试状态\n你的心情：正常"

        result = await build_inner_context(
            chat_id="oc_test",
            chat_type="group",
            user_ids=[],
            trigger_user_id="u1",
            trigger_username="测试用户",
            persona_id="akao",
            chat_name="测试群",
        )

        assert len(result) < 1000
```

- [ ] **Step 7: 运行测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_inner_context.py -v
```

- [ ] **Step 8: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py apps/agent-service/tests/unit/test_inner_context.py
git commit -m "refactor(context): remove schedule/fragments/dream raw injection from inner-context"
```

---

### Task 4: 精简主 agent 工具集

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/tools.py`
- Modify: Langfuse `main` prompt（移除工具说明）

- [ ] **Step 1: 修改 tools.py — 移除搜索工具**

```python
# 旧代码
BASE_TOOLS = [
    search_web,
    search_images,
    search_group_history,    # 移除
    list_group_members,      # 移除
    generate_image,
    read_images,
    recall,                  # 移除
    check_chat_history,      # 移除
]

# 新代码
BASE_TOOLS = [
    search_web,
    search_images,
    generate_image,
    read_images,
]

ALL_TOOLS = [
    *BASE_TOOLS,
    deep_research,
    load_skill,
    sandbox_bash,
]
```

- [ ] **Step 2: 更新 Langfuse main prompt — 移除工具说明**

创建 main prompt 新版本，`<tools>` 段中移除以下工具描述：
- `search_group_history`
- `list_group_members`
- `recall`（不再有记忆回溯工具）
- `check_chat_history`

同时修改 `search_web` 描述，加入限制：

```
- **search_web**: 查询外部信息。仅在用户明确要求你查某个东西时使用。不要用它搜索群聊内容或个人记忆。
```

移除 `<inner-context>` 末尾的记忆回溯引导语（"如果隐约觉得知道点什么..."）。

- [ ] **Step 3: 更新 deep_research 子 agent 的 BASE_TOOLS**

deep_research 的子 agent 也继承 BASE_TOOLS，移除搜索工具后自动生效。但需要确认 research_agent prompt 中是否引用了被移除的工具——如果有则需要同步更新。

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/tools.py
git commit -m "refactor(tools): remove search/recall tools from main agent"
```

---

### Task 5: 添加工具调用次数限制

**Files:**
- Modify: `apps/agent-service/app/agents/core/agent.py`

- [ ] **Step 1: 确认 LangGraph agent 的迭代限制配置**

查看 `create_agent()` 函数（`app/agents/core/agent.py`），找到 LangGraph agent 的创建位置。添加 `recursion_limit` 参数：

```python
# 在 create_agent 或 agent.run 调用中添加
config = {"recursion_limit": 10}  # 最多 10 轮工具调用
```

具体实现取决于 LangGraph 版本。如果用的是 `create_react_agent`：

```python
agent = create_react_agent(model, tools, system_prompt=system_prompt)
# 在 invoke/stream 时传 config
result = await agent.ainvoke(messages, config={"recursion_limit": 10})
```

- [ ] **Step 2: 测试确认限制生效**

手动触发一条需要多次工具调用的消息，确认不会超过 10 轮。

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/agents/core/agent.py
git commit -m "fix(agent): add recursion_limit=10 to prevent tool call loops"
```

---

### Task 6: 集成验证

- [ ] **Step 1: 本地运行完整测试套件**

```bash
cd apps/agent-service && uv run pytest tests/ -v --timeout=30
```

- [ ] **Step 2: 部署到测试泳道验证**

```bash
make deploy APP=agent-service LANE=ctx-v4 GIT_REF=chore/chiwei-feedback-analysis
```

- [ ] **Step 3: 绑定 dev bot 测试**

```
/ops bind TYPE=bot KEY=dev LANE=ctx-v4
```

在飞书 dev bot 中验证：
1. @赤尾 正常回复（inner-context 瘦身生效）
2. 回复长度在 20-100 字波动（之前的 prompt 改动生效）
3. 赤尾不会尝试搜索群历史（工具已移除）
4. 赤尾知道自己长什么样（appearance 注入生效）

- [ ] **Step 4: 查 Langfuse trace 确认 system prompt 大小**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py list-traces '{"limit":3,"orderBy":"timestamp.desc","name":"main"}'
```

获取最新 trace，确认 system prompt 总字符数 < 3500。

- [ ] **Step 5: 确认 reply_style_log 有审计记录**

```sql
SELECT id, persona_id, source, length(style_text), created_at
FROM reply_style_log
ORDER BY created_at DESC
LIMIT 5
```

- [ ] **Step 6: 验收后清理**

```
/ops unbind TYPE=bot KEY=dev
make undeploy APP=agent-service LANE=ctx-v4
```

- [ ] **Step 7: Commit 并推送**

```bash
git push origin chore/chiwei-feedback-analysis
```
