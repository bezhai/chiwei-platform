# Multi-Bot Context 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让多个 persona bot（千凪/赤尾/绫奈）在同一群聊中各自拥有独立的人设、对话历史视角和上下文记忆。

**Architecture:** 新增 `bot_persona` 表存储每个 bot 的人设数据；在 agent-service 引入 `BotContext` 封装 per-(chat_id, bot_name) 的所有上下文；为 `PersonImpression`、`GroupCultureGestalt`、Redis reply_style 加 `bot_name` 维度实现完全隔离。

**Tech Stack:** Python/SQLAlchemy (agent-service), TypeORM/TypeScript (lark-server), PostgreSQL, Redis, Langfuse

**Spec:** `docs/superpowers/specs/2026-04-03-multi-bot-context-design.md`

---

## 文件清单

**新建：**
- `apps/lark-server/src/infrastructure/dal/entities/bot-persona.ts`
- `apps/agent-service/app/services/bot_context.py`
- `apps/agent-service/scripts/migrate_add_bot_name.sql`
- `apps/agent-service/scripts/seed_bot_persona.py`
- `apps/agent-service/tests/unit/test_bot_context.py`

**修改：**
- `apps/lark-server/src/ormconfig.ts` — 注册 BotPersona entity
- `apps/agent-service/app/orm/models.py` — 新增 BotPersona，更新 PersonImpression/GroupCultureGestalt
- `apps/agent-service/app/orm/crud.py` — 所有 impression/gestalt CRUD 加 bot_name 参数
- `apps/agent-service/app/services/identity_drift.py` — Redis key 加 bot_name 维度
- `apps/agent-service/app/services/memory_context.py` — 加 bot_name 参数，删 _DEFAULT_REPLY_STYLE
- `apps/agent-service/app/services/quick_search.py` — QuickSearchResult 加 bot_name，修复硬编码"赤尾"
- `apps/agent-service/app/agents/domains/main/context_builder.py` — p2p 历史加 bot 身份
- `apps/agent-service/app/agents/domains/main/agent.py` — 接入 BotContext，修复 3 处错误消息
- `apps/agent-service/app/workers/diary_worker.py` — per-bot 循环生成
- `apps/agent-service/app/workers/schedule_worker.py` — 从 DB 读 persona_core
- `apps/agent-service/app/workers/journal_worker.py` — 从 DB 读 persona_lite
- Langfuse `main` prompt — `<identity>` 改为 `{{identity}}`

---

## Task 1: lark-server BotPersona entity

**Files:**
- Create: `apps/lark-server/src/infrastructure/dal/entities/bot-persona.ts`
- Modify: `apps/lark-server/src/ormconfig.ts`

- [ ] **Step 1: 创建 BotPersona entity**

```typescript
// apps/lark-server/src/infrastructure/dal/entities/bot-persona.ts
import {
    Column,
    CreateDateColumn,
    Entity,
    PrimaryColumn,
    UpdateDateColumn,
} from 'typeorm'

@Entity('bot_persona')
export class BotPersona {
    @PrimaryColumn({ type: 'varchar', length: 50 })
    bot_name!: string

    @Column({ type: 'varchar', length: 50 })
    display_name!: string

    @Column({ type: 'text' })
    persona_core!: string

    @Column({ type: 'text' })
    persona_lite!: string

    @Column({ type: 'text' })
    default_reply_style!: string

    @Column({ type: 'jsonb', default: '{}' })
    error_messages!: Record<string, string>

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date

    @UpdateDateColumn({ name: 'updated_at' })
    updatedAt!: Date
}
```

- [ ] **Step 2: 注册到 ormconfig**

在 `apps/lark-server/src/ormconfig.ts` 的 entities 数组里加入 BotPersona：

```typescript
import { BotPersona } from './infrastructure/dal/entities/bot-persona'
// 在 entities: [...] 里加入 BotPersona
```

- [ ] **Step 3: 验证 TypeORM 同步创建表**

启动 lark-server 后检查 DB，确认 `bot_persona` 表已创建（`synchronize: true` 会自动建表）。

```bash
# 通过 /ops-db 执行
# SELECT table_name FROM information_schema.tables WHERE table_name = 'bot_persona';
```

- [ ] **Step 4: Commit**

```bash
git add apps/lark-server/src/infrastructure/dal/entities/bot-persona.ts apps/lark-server/src/ormconfig.ts
git commit -m "feat(lark-server): add BotPersona entity"
```

---

## Task 2: agent-service BotPersona ORM 模型

**Files:**
- Modify: `apps/agent-service/app/orm/models.py`

- [ ] **Step 1: 在 models.py 末尾添加 BotPersona 模型**

在 `apps/agent-service/app/orm/models.py` 末尾 `WeeklyReview` 类后面添加：

```python
class BotPersona(Base):
    """Bot 人设配置 — 每个 persona bot 的人设数据"""

    __tablename__ = "bot_persona"

    bot_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    persona_core: Mapped[str] = mapped_column(Text, nullable=False)
    persona_lite: Mapped[str] = mapped_column(Text, nullable=False)
    default_reply_style: Mapped[str] = mapped_column(Text, nullable=False)
    error_messages: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

（注意：`models.py` 顶部已有 `JSON` import，如无则加 `from sqlalchemy import JSON`）

- [ ] **Step 2: 更新 PersonImpression，加 bot_name 字段**

将 `apps/agent-service/app/orm/models.py` 中 PersonImpression 类改为：

```python
class PersonImpression(Base):
    """Bot 对群友的人物印象"""

    __tablename__ = "person_impression"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    bot_name: Mapped[str] = mapped_column(String(50), nullable=False, default="chiwei")
    impression_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("chat_id", "user_id", "bot_name"),)
```

- [ ] **Step 3: 更新 GroupCultureGestalt，加 bot_name 字段**

将 `apps/agent-service/app/orm/models.py` 中 GroupCultureGestalt 类改为：

```python
class GroupCultureGestalt(Base):
    """Bot 对一个群的整体感觉，一句话"""

    __tablename__ = "group_culture_gestalt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    bot_name: Mapped[str] = mapped_column(String(50), nullable=False, default="chiwei")
    gestalt_text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("chat_id", "bot_name"),)
```

注意：`GroupCultureGestalt` 从单 `chat_id` 主键改为 `id` 自增主键 + `(chat_id, bot_name)` 唯一约束。

- [ ] **Step 4: 写 DB migration SQL**

创建 `apps/agent-service/scripts/migrate_add_bot_name.sql`：

```sql
-- 1. person_impression 加 bot_name 列，存量数据默认 chiwei
ALTER TABLE person_impression ADD COLUMN IF NOT EXISTS bot_name VARCHAR(50) NOT NULL DEFAULT 'chiwei';

-- 删除旧唯一约束，建新约束
ALTER TABLE person_impression DROP CONSTRAINT IF EXISTS person_impression_chat_id_user_id_key;
ALTER TABLE person_impression ADD CONSTRAINT person_impression_chat_id_user_id_bot_name_key
    UNIQUE (chat_id, user_id, bot_name);

-- 2. group_culture_gestalt 从 chat_id 主键改为 id + 唯一约束
-- 先加新列
ALTER TABLE group_culture_gestalt ADD COLUMN IF NOT EXISTS bot_name VARCHAR(50) NOT NULL DEFAULT 'chiwei';
ALTER TABLE group_culture_gestalt ADD COLUMN IF NOT EXISTS id SERIAL;

-- 删除旧主键，建新主键
ALTER TABLE group_culture_gestalt DROP CONSTRAINT IF EXISTS group_culture_gestalt_pkey;
ALTER TABLE group_culture_gestalt ADD PRIMARY KEY (id);
ALTER TABLE group_culture_gestalt ADD CONSTRAINT group_culture_gestalt_chat_id_bot_name_key
    UNIQUE (chat_id, bot_name);
```

- [ ] **Step 5: 通过 ops-db 执行 migration SQL**

```
/ops-db <SQL 内容>
```

分语句逐条执行，确认每条无报错。

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/orm/models.py apps/agent-service/scripts/migrate_add_bot_name.sql
git commit -m "feat(agent-service): BotPersona model + bot_name migration for impression/gestalt"
```

---

## Task 3: CRUD 函数加 bot_name 参数

**Files:**
- Modify: `apps/agent-service/app/orm/crud.py`

- [ ] **Step 1: 新增 get_bot_persona 函数**

在 crud.py 适当位置（建议放在 `get_gray_config` 附近）添加：

```python
async def get_bot_persona(bot_name: str) -> "BotPersona | None":
    """获取 bot 人设配置"""
    from app.orm.models import BotPersona
    async with AsyncSessionLocal() as session:
        return await session.get(BotPersona, bot_name)
```

- [ ] **Step 2: 更新 get_impressions_for_users**

```python
async def get_impressions_for_users(
    chat_id: str, user_ids: list[str], bot_name: str
) -> list[PersonImpression]:
    """查询指定群中指定用户的印象（per-bot）"""
    if not user_ids:
        return []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonImpression)
            .where(PersonImpression.chat_id == chat_id)
            .where(PersonImpression.user_id.in_(user_ids))
            .where(PersonImpression.bot_name == bot_name)
        )
        return list(result.scalars().all())
```

- [ ] **Step 3: 更新 get_all_impressions_for_chat**

```python
async def get_all_impressions_for_chat(chat_id: str, bot_name: str) -> list[PersonImpression]:
    """查询指定群指定 bot 的所有已有印象"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonImpression)
            .where(PersonImpression.chat_id == chat_id)
            .where(PersonImpression.bot_name == bot_name)
        )
        return list(result.scalars().all())
```

- [ ] **Step 4: 更新 upsert_person_impression**

```python
async def upsert_person_impression(
    chat_id: str, user_id: str, impression_text: str, bot_name: str
) -> None:
    """插入或更新人物印象（per-bot）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonImpression)
            .where(PersonImpression.chat_id == chat_id)
            .where(PersonImpression.user_id == user_id)
            .where(PersonImpression.bot_name == bot_name)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.impression_text = impression_text
        else:
            session.add(
                PersonImpression(
                    chat_id=chat_id,
                    user_id=user_id,
                    bot_name=bot_name,
                    impression_text=impression_text,
                )
            )
        await session.commit()
```

- [ ] **Step 5: 更新 get_group_culture_gestalt**

```python
async def get_group_culture_gestalt(chat_id: str, bot_name: str) -> str:
    """获取群文化 gestalt（per-bot），无则返回空字符串"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(GroupCultureGestalt)
            .where(GroupCultureGestalt.chat_id == chat_id)
            .where(GroupCultureGestalt.bot_name == bot_name)
        )
        row = result.scalar_one_or_none()
        return row.gestalt_text if row else ""
```

- [ ] **Step 6: 更新 upsert_group_culture_gestalt**

```python
async def upsert_group_culture_gestalt(
    chat_id: str, gestalt_text: str, bot_name: str
) -> None:
    """写入/更新群文化 gestalt（per-bot）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(GroupCultureGestalt)
            .where(GroupCultureGestalt.chat_id == chat_id)
            .where(GroupCultureGestalt.bot_name == bot_name)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.gestalt_text = gestalt_text
        else:
            session.add(GroupCultureGestalt(
                chat_id=chat_id, bot_name=bot_name, gestalt_text=gestalt_text
            ))
        await session.commit()
```

- [ ] **Step 7: 更新 get_cross_group_impressions**

```python
async def get_cross_group_impressions(
    user_id: str, bot_name: str, limit: int = 5
) -> list[tuple[PersonImpression, str]]:
    """查询某用户在所有群聊中的印象（per-bot，按更新时间倒序）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonImpression, LarkGroupChatInfo.name)
            .join(
                LarkGroupChatInfo,
                PersonImpression.chat_id == LarkGroupChatInfo.chat_id,
            )
            .where(PersonImpression.user_id == user_id)
            .where(PersonImpression.bot_name == bot_name)
            .order_by(PersonImpression.updated_at.desc())
            .limit(limit)
        )
        return [(row[0], row[1]) for row in result.all()]
```

- [ ] **Step 8: 新增 get_all_persona_bot_names**

diary_worker 需要知道当前系统有哪些 persona bot：

```python
async def get_all_persona_bot_names() -> list[str]:
    """获取所有 persona bot 的 bot_name 列表"""
    from app.orm.models import BotPersona
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotPersona.bot_name))
        return [row[0] for row in result.all()]
```

- [ ] **Step 9: Commit**

```bash
git add apps/agent-service/app/orm/crud.py
git commit -m "feat(agent-service): crud functions add bot_name dimension"
```

---

## Task 4: identity_drift.py — per-bot Redis keys

**Files:**
- Modify: `apps/agent-service/app/services/identity_drift.py`

- [ ] **Step 1: 更新 Redis key 构建函数**

将文件开头的 key 函数改为：

```python
def _state_key(chat_id: str, bot_name: str) -> str:
    return f"{_KEY_PREFIX}:{chat_id}:{bot_name}"


def _base_key(bot_name: str) -> str:
    return f"reply_style:__base__:{bot_name}"
```

删除旧的 `_BASE_KEY = "reply_style:__base__"` 常量。

- [ ] **Step 2: 更新所有使用旧 key 的函数签名**

```python
async def get_base_reply_style(bot_name: str) -> str | None:
    """读取指定 bot 的全局基线 reply_style"""
    redis = AsyncRedisClient.get_instance()
    return await redis.get(_base_key(bot_name))


async def set_base_reply_style(style: str, bot_name: str) -> None:
    """写入指定 bot 的全局基线 reply_style"""
    redis = AsyncRedisClient.get_instance()
    await redis.set(_base_key(bot_name), style, ex=_BASE_TTL_SECONDS)
    logger.info(f"[{bot_name}] Base reply_style updated: {style[:50]}...")


async def get_identity_state(chat_id: str, bot_name: str) -> str | None:
    """读取指定 bot 在指定群的漂移状态"""
    redis = AsyncRedisClient.get_instance()
    return await redis.get(_state_key(chat_id, bot_name))


async def set_identity_state(chat_id: str, bot_name: str, state: str, ttl: int = 86400) -> None:
    """写入指定 bot 在指定群的漂移状态"""
    redis = AsyncRedisClient.get_instance()
    await redis.set(_state_key(chat_id, bot_name), state, ex=ttl)
```

- [ ] **Step 3: 更新 generate_base_reply_style，加 bot_name 参数**

```python
async def generate_base_reply_style(bot_name: str) -> str | None:
    """基于当前 Schedule 为指定 bot 生成全局基线 reply_style"""
    schedule_context = await _get_schedule_context()
    if not schedule_context or schedule_context.startswith("（"):
        logger.info(f"[{bot_name}] No schedule, skip base reply_style generation")
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
        logger.warning(f"[{bot_name}] Base reply_style generation returned empty")
        return None

    await set_base_reply_style(style, bot_name)
    return style
```

- [ ] **Step 4: 更新 IdentityDriftManager.on_event 和 _run_drift，加 bot_name**

`IdentityDriftManager` 目前用 `chat_id` 作为实例 key。需要改为 `(chat_id, bot_name)` 作为 key：

```python
class IdentityDriftManager:
    _instance: "IdentityDriftManager | None" = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self._chat_locks: dict[str, asyncio.Lock] = {}  # key: "chat_id:bot_name"
        self._pending: dict[str, list] = {}
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        self._flush_counts: dict[str, int] = {}

    @classmethod
    def get_instance(cls) -> "IdentityDriftManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _key(self, chat_id: str, bot_name: str) -> str:
        return f"{chat_id}:{bot_name}"

    async def on_event(self, chat_id: str, bot_name: str) -> None:
        """收到新消息事件，触发漂移计划"""
        key = self._key(chat_id, bot_name)
        # 使用 key 替换原来所有用 chat_id 的地方
        # 逻辑不变，只是把 chat_id 换成 key，同时传 bot_name 给 _run_drift
        ...
```

（`_run_drift` 内部调用 `get_identity_state`、`set_identity_state` 时传入 `bot_name`）

- [ ] **Step 5: 更新 cron 入口（generate_base_reply_style 调用处）**

找到调用 `generate_base_reply_style()` 的 cron worker，改为对每个 persona bot 分别调用：

```python
from app.orm.crud import get_all_persona_bot_names

async def cron_generate_base_reply_style(ctx) -> None:
    bot_names = await get_all_persona_bot_names()
    for bot_name in bot_names:
        await generate_base_reply_style(bot_name)
```

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/services/identity_drift.py
git commit -m "feat(agent-service): identity_drift per-bot Redis keys"
```

---

## Task 5: memory_context.py — 加 bot_name 参数

**Files:**
- Modify: `apps/agent-service/app/services/memory_context.py`

- [ ] **Step 1: 写失败测试**

在 `apps/agent-service/tests/unit/test_memory_context.py` 中添加：

```python
@pytest.mark.asyncio
async def test_get_reply_style_uses_bot_name():
    """get_reply_style 应使用 bot_name 维度的 Redis key"""
    with (
        patch("app.services.memory_context.get_identity_state", return_value="drifted") as mock_drift,
        patch("app.services.memory_context.get_base_reply_style", return_value=None),
    ):
        result = await get_reply_style("chat_abc", "chiwei")
        mock_drift.assert_called_once_with("chat_abc", "chiwei")
        assert result == "drifted"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_memory_context.py -v -k "test_get_reply_style_uses_bot_name"
```

Expected: FAIL（`get_reply_style` 目前不接受 `bot_name`）

- [ ] **Step 3: 更新 get_reply_style，加 bot_name 参数**

```python
async def get_reply_style(chat_id: str, bot_name: str, default_style: str = "") -> str:
    """获取动态 reply-style：per-chat 漂移 → 全局基线 → DB 默认"""
    try:
        drift_state = await get_identity_state(chat_id, bot_name)
        if drift_state:
            return drift_state
    except Exception:
        pass

    try:
        base_state = await get_base_reply_style(bot_name)
        if base_state:
            return base_state
    except Exception:
        pass

    return default_style  # 由调用方从 bot_persona.default_reply_style 传入
```

删除 `_DEFAULT_REPLY_STYLE` 常量。

- [ ] **Step 4: 更新 build_inner_context，加 bot_name 参数**

```python
async def build_inner_context(
    chat_id: str,
    chat_type: str,
    user_ids: list[str],
    trigger_user_id: str,
    trigger_username: str,
    bot_name: str,            # 新增
    chat_name: str = "",
    *,
    is_proactive: bool = False,
    proactive_stimulus: str = "",
) -> str:
```

在函数体内，把所有调用 `get_group_culture_gestalt`、`get_impressions_for_users`、`get_cross_group_impressions` 的地方加上 `bot_name=bot_name` 参数。

- [ ] **Step 5: 运行测试，确认通过**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_memory_context.py -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/services/memory_context.py
git commit -m "feat(agent-service): memory_context add bot_name dimension"
```

---

## Task 6: BotContext 类

**Files:**
- Create: `apps/agent-service/app/services/bot_context.py`
- Create: `apps/agent-service/tests/unit/test_bot_context.py`

- [ ] **Step 1: 写失败测试**

```python
# apps/agent-service/tests/unit/test_bot_context.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain.messages import AIMessage, HumanMessage

from app.services.bot_context import BotContext


def _make_msg(role: str, content: str, bot_name: str | None, username: str):
    from app.services.quick_search import QuickSearchResult
    from datetime import datetime
    m = QuickSearchResult(
        message_id="m1",
        content=content,
        user_id="u1",
        create_time=datetime.now(),
        role=role,
        username=username,
        bot_name=bot_name,
    )
    return m


def test_build_chat_history_current_bot_is_assistant():
    """当前 bot 的消息应映射为 AIMessage"""
    ctx = BotContext(chat_id="c1", bot_name="chiwei", chat_type="group")
    msgs = [
        _make_msg("assistant", "你好", "chiwei", "赤尾"),
        _make_msg("user", "嗨", None, "张三"),
    ]
    result = ctx.build_chat_history(msgs)
    assert isinstance(result[0], AIMessage)
    assert isinstance(result[1], HumanMessage)


def test_build_chat_history_other_bot_is_human():
    """其他 bot 的消息应映射为 HumanMessage，带名字前缀"""
    ctx = BotContext(chat_id="c1", bot_name="chiwei", chat_type="group")
    msgs = [
        _make_msg("assistant", "我是千凪", "chinagi", "千凪"),
        _make_msg("assistant", "我是赤尾", "chiwei", "赤尾"),
    ]
    result = ctx.build_chat_history(msgs)
    assert isinstance(result[0], HumanMessage)
    assert "千凪" in str(result[0].content)
    assert isinstance(result[1], AIMessage)


def test_get_error_message_uses_persona():
    """get_error_message 应从 persona 读，不硬编码"""
    ctx = BotContext(chat_id="c1", bot_name="chiwei", chat_type="group")
    ctx._persona = MagicMock()
    ctx._persona.display_name = "赤尾"
    ctx._persona.error_messages = {"guard": "赤尾不想讨论这个~"}
    assert ctx.get_error_message("guard") == "赤尾不想讨论这个~"
    assert "赤尾" in ctx.get_error_message("unknown_key")
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_bot_context.py -v
```

Expected: FAIL（`BotContext` 尚未实现）

- [ ] **Step 3: 实现 BotContext 类**

```python
# apps/agent-service/app/services/bot_context.py
"""Bot 上下文容器 — per-(chat_id, bot_name) 的所有上下文数据"""
import asyncio
import logging
from typing import TYPE_CHECKING

from langchain.messages import AIMessage, HumanMessage

if TYPE_CHECKING:
    from app.orm.models import BotPersona
    from app.services.quick_search import QuickSearchResult

logger = logging.getLogger(__name__)


class BotContext:
    def __init__(self, chat_id: str, bot_name: str, chat_type: str) -> None:
        self.chat_id = chat_id
        self.bot_name = bot_name
        self.chat_type = chat_type
        self._persona: "BotPersona | None" = None
        self._reply_style: str = ""

    async def load(self) -> None:
        """并行加载所有 per-bot 数据"""
        from app.orm.crud import get_bot_persona
        from app.services.memory_context import get_reply_style

        self._persona = await get_bot_persona(self.bot_name)
        if self._persona is None:
            logger.warning(f"BotPersona not found for bot_name={self.bot_name}, using defaults")

        default_style = self._persona.default_reply_style if self._persona else ""
        self._reply_style = await get_reply_style(
            self.chat_id, self.bot_name, default_style
        )

    @property
    def reply_style(self) -> str:
        return self._reply_style

    def get_identity(self) -> str:
        """返回注入 {{identity}} 的人设文本"""
        return self._persona.persona_core if self._persona else ""

    def get_display_name(self) -> str:
        return self._persona.display_name if self._persona else self.bot_name

    def get_error_message(self, kind: str) -> str:
        """返回 bot 专属错误消息"""
        name = self.get_display_name()
        if self._persona and self._persona.error_messages:
            return self._persona.error_messages.get(kind, f"{name}遇到了问题QAQ")
        return f"{name}遇到了问题QAQ"

    def build_chat_history(
        self, messages: "list[QuickSearchResult]"
    ) -> list[AIMessage | HumanMessage]:
        """构建 LLM 对话历史：当前 bot → AIMessage，其余 → HumanMessage（带名字前缀）"""
        result: list[AIMessage | HumanMessage] = []
        for msg in messages:
            # 判断是否为当前 bot 的发言
            is_self = (msg.role == "assistant" and msg.bot_name == self.bot_name)
            if is_self:
                result.append(AIMessage(content=msg.content))
            else:
                # 人类用户或其他 bot：统一作为 HumanMessage，带发言者名字
                if msg.username:
                    content = f"{msg.username}: {msg.content}"
                else:
                    content = msg.content
                result.append(HumanMessage(content=content))
        return result
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_bot_context.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/bot_context.py apps/agent-service/tests/unit/test_bot_context.py
git commit -m "feat(agent-service): BotContext class"
```

---

## Task 7: quick_search.py — bot_name 字段 + 修复硬编码

**Files:**
- Modify: `apps/agent-service/app/services/quick_search.py`

- [ ] **Step 1: 给 QuickSearchResult 加 bot_name 字段**

```python
class QuickSearchResult:
    def __init__(
        self,
        message_id: str,
        content: str,
        user_id: str,
        create_time: datetime,
        role: str,
        username: str | None = None,
        chat_type: str | None = None,
        chat_name: str | None = None,
        reply_message_id: str | None = None,
        chat_id: str | None = None,
        bot_name: str | None = None,   # 新增
    ):
        ...
        self.bot_name = bot_name
```

- [ ] **Step 2: 修复硬编码 "赤尾"，使用 bot_name 的 display_name**

当前第 139 行：
```python
username=username if msg.role == "user" else "赤尾",
```

改为：
```python
username=username if msg.role == "user" else (msg.bot_name or "assistant"),
bot_name=msg.bot_name if msg.role == "assistant" else None,
```

说明：`display_name` 的映射留给 `BotContext` 处理；`quick_search` 只需传递原始 `bot_name`。

- [ ] **Step 3: 运行现有测试**

```bash
cd apps/agent-service && uv run pytest tests/ -v -k "quick_search or context_builder"
```

Expected: PASS（无破坏性变更）

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/services/quick_search.py
git commit -m "feat(agent-service): QuickSearchResult add bot_name field"
```

---

## Task 8: context_builder.py — p2p 历史使用 bot 身份

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/context_builder.py`

此任务只修改 `_build_p2p_messages`：在私聊中，如果消息是其他 bot 发的（`msg.bot_name != current_bot_name`），应生成 `HumanMessage`。群聊消息已全部合并成单条 `HumanMessage` via context_builder 模板，无需改动。

- [ ] **Step 1: build_chat_context 返回值加入 bot_name**

`build_chat_context` 目前返回 8 元素 tuple。需要从 `l1_results` 中获取当前消息的 `bot_name`（即本次请求由哪个 bot 触发）。

但实际上，`bot_name` 从 MQ 消息体通过 `header_vars["app_name"]` 注入，不从 DB 读取。所以 `_build_p2p_messages` 通过参数接收 `current_bot_name`：

```python
def _build_p2p_messages(
    messages: list[QuickSearchResult],
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
    current_bot_name: str,            # 新增
) -> list[HumanMessage | AIMessage]:
    ...
    for msg in messages:
        ...
        # 用 bot_name 判断，而不是 role
        is_self = (msg.role == "assistant" and msg.bot_name == current_bot_name)
        if is_self:
            result.append(AIMessage(content_blocks=content_blocks))
        else:
            result.append(HumanMessage(content_blocks=content_blocks))
```

- [ ] **Step 2: build_chat_context 接受 current_bot_name 并透传**

```python
async def build_chat_context(
    message_id: str,
    current_bot_name: str = "",   # 新增，默认空字符串兼容旧调用
    limit: int = 10,
) -> tuple[...]:
    ...
    if chat_type == "group":
        messages = _build_group_messages(...)
    else:
        messages = _build_p2p_messages(
            l1_results, image_key_to_url, image_key_to_filename,
            current_bot_name=current_bot_name,
        )
```

- [ ] **Step 3: 运行现有测试**

```bash
cd apps/agent-service && uv run pytest tests/ -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/context_builder.py
git commit -m "feat(agent-service): context_builder p2p history bot-aware"
```

---

## Task 9: agent.py — 接入 BotContext，修复错误消息

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/agent.py`

- [ ] **Step 1: 在 _build_and_stream 里获取 bot_name 并创建 BotContext**

找到 `_build_and_stream` 函数，在最开头加：

```python
from app.middleware.chat_context import get_app_name  # 或直接用 header_vars
from app.services.bot_context import BotContext

async def _build_and_stream(
    message_id: str,
    gray_config: dict,
    session_id: str | None = None,
) -> AsyncGenerator[str, None]:
    t_build_start = time.monotonic()
    from app.skills.registry import SkillRegistry

    # 获取当前 bot_name（由 MQ consumer 注入到 context var）
    from app.workers.chat_consumer import _get_current_bot_name
    bot_name = _get_current_bot_name()  # 返回 header_vars["app_name"].get()
```

注意：`bot_name` 已在 `chat_consumer.py` 中通过 `header_vars["app_name"].set(bot_name)` 注入，直接读取即可。查看 `chat_consumer.py` 确认读取方式，通常是：

```python
from app.middleware.lane import header_vars
bot_name = header_vars["app_name"].get() or ""
```

- [ ] **Step 2: 创建 BotContext 并加载**

在获取 `bot_name` 后、`build_chat_context` 之前（或并行）加载：

```python
    # 构建上下文
    (messages, image_registry, chat_id, trigger_username,
     chat_type, trigger_user_id, chat_name, chain_user_ids) = await build_chat_context(
        message_id, current_bot_name=bot_name
    )

    # 创建并加载 BotContext
    bot_ctx = BotContext(chat_id=chat_id, bot_name=bot_name, chat_type=chat_type)
    await bot_ctx.load()
```

- [ ] **Step 3: 用 BotContext 替换 prompt_vars 里的散装调用**

```python
    # identity 注入（新）
    prompt_vars["identity"] = bot_ctx.get_identity()

    # inner_context
    try:
        from app.agents.domains.main.context_builder import _is_proactive_var, _proactive_stimulus_var
        prompt_vars["inner_context"] = await build_inner_context(
            chat_id=chat_id,
            chat_type=chat_type,
            user_ids=chain_user_ids,
            trigger_user_id=trigger_user_id,
            trigger_username=trigger_username,
            bot_name=bot_name,          # 新增
            chat_name=chat_name,
            is_proactive=_is_proactive_var.get(False),
            proactive_stimulus=_proactive_stimulus_var.get(""),
        )
    except Exception as e:
        logger.error(f"Failed to build inner context: {e}")

    # reply_style（直接从 BotContext 取，已在 load() 中加载）
    prompt_vars["reply_style"] = bot_ctx.reply_style
```

- [ ] **Step 4: ChatAgent 使用 bot_persona.langfuse_prompt_key（预留扩展）**

默认仍用 `"main"`，但从 BotContext 读：

```python
    prompt_id = "main"  # bot_persona 目前没有 langfuse_prompt_key 字段，留默认
    agent = ChatAgent(prompt_id, ALL_TOOLS, model_id=model_id, trace_name="main")
```

（如果后续 bot_persona 加了 langfuse_prompt_key 字段，这里改为 `bot_ctx._persona.langfuse_prompt_key`）

- [ ] **Step 5: 修复 3 处硬编码错误消息**

```python
# L31 附近
GUARD_REJECT_MESSAGE = ""  # 不再用静态常量，在调用处从 bot_ctx 读

# 调用 GUARD_REJECT_MESSAGE 的地方改为：
# yield bot_ctx.get_error_message("guard")

# L319 附近
# 原: yield "小尾有点不想讨论这个话题呢~"
# 改为:
yield bot_ctx.get_error_message("content_filter")

# L376 附近
# 原: yield "赤尾好像遇到了一些问题呢QAQ"
# 改为:
yield bot_ctx.get_error_message("error")
```

注意：`GUARD_REJECT_MESSAGE` 可能在 `pre/nodes/safety.py` 里被引用，需要一并查找：

```bash
grep -rn "GUARD_REJECT_MESSAGE" apps/agent-service/
```

找到所有引用，改为通过 `bot_ctx.get_error_message("guard")` 或传入参数。

- [ ] **Step 6: IdentityDriftManager.on_event 调用加 bot_name**

```python
# 原: IdentityDriftManager.get_instance().on_event(chat_id)
# 改为:
asyncio.create_task(
    IdentityDriftManager.get_instance().on_event(chat_id, bot_name)
)
```

- [ ] **Step 7: 运行现有测试**

```bash
cd apps/agent-service && uv run pytest tests/ -v
```

Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/agent.py
git commit -m "feat(agent-service): wire BotContext in _build_and_stream"
```

---

## Task 10: Langfuse main prompt — 参数化 `<identity>`

- [ ] **Step 1: 获取当前 main prompt production 版本号**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py get-prompt '{"name":"main","label":"production"}'
```

记录当前版本号（当前为 v85）。

- [ ] **Step 2: 创建新版本，替换 `<identity>` 块**

将 `<identity>` 到 `</identity>` 之间的硬编码内容替换为 `{{identity}}`：

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py create-prompt '{
  "name": "main",
  "type": "text",
  "prompt": "（完整 prompt 内容，只改 <identity>{{identity}}</identity> 这一处）",
  "labels": ["staging"]
}'
```

- [ ] **Step 3: 验证 staging 版本**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py get-prompt '{"name":"main","label":"staging"}'
```

确认 `<identity>{{identity}}</identity>` 已正确写入。

- [ ] **Step 4: 部署到泳道测试，确认 {{identity}} 能被编译**

在测试泳道发一条消息，确认赤尾的人设（从 DB 读）能正常注入。确认通过后：

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py update-labels '{
  "name": "main",
  "version": 86,
  "newLabels": ["production", "latest"]
}'
```

- [ ] **Step 5: Commit（记录 prompt 版本变更）**

```bash
git commit --allow-empty -m "feat(langfuse): main prompt v86 — parameterize identity block"
```

---

## Task 11: diary_worker.py — per-bot 生成

**Files:**
- Modify: `apps/agent-service/app/workers/diary_worker.py`

- [ ] **Step 1: 替换 _get_persona_lite() 函数**

删除从 Langfuse 加载 `persona_lite` 的逻辑，改为从 DB 读：

```python
async def _get_persona_lite_for_bot(bot_name: str) -> str:
    """从 bot_persona 表加载 persona_lite"""
    from app.orm.crud import get_bot_persona
    try:
        persona = await get_bot_persona(bot_name)
        return persona.persona_lite if persona else ""
    except Exception as e:
        logger.warning(f"[{bot_name}] Failed to load persona_lite: {e}")
        return ""
```

- [ ] **Step 2: cron_generate_diaries 改为 per-bot 循环**

```python
async def cron_generate_diaries(ctx) -> None:
    """cron 入口：为活跃群和私聊的每个 persona bot 生成昨天的日记"""
    from app.orm.crud import get_all_persona_bot_names
    yesterday = date.today() - timedelta(days=1)

    bot_names = await get_all_persona_bot_names()
    group_ids = await get_active_diary_chat_ids(min_replies=5, days=7)
    p2p_ids = await get_active_p2p_chat_ids(min_replies=2, days=1)
    all_ids = group_ids + p2p_ids

    if not all_ids or not bot_names:
        logger.info("No active chats or bots, skip diary generation")
        return

    for bot_name in bot_names:
        for chat_id in all_ids:
            try:
                await generate_diary_for_chat(chat_id, yesterday, bot_name=bot_name)
            except Exception as e:
                logger.error(f"[{bot_name}] Diary failed for {chat_id}: {e}")
```

- [ ] **Step 3: generate_diary_for_chat 加 bot_name 参数**

```python
async def generate_diary_for_chat(
    chat_id: str,
    target_date: date | None = None,
    bot_name: str = "chiwei",   # 默认值保持向后兼容
) -> str | None:
    ...
    persona_lite = await _get_persona_lite_for_bot(bot_name)
    ...
```

- [ ] **Step 4: post_process_impressions 加 bot_name**

```python
async def post_process_impressions(
    chat_id: str,
    diary_content: str,
    existing_msgs: list,
    bot_name: str,
) -> None:
    ...
    existing = await get_all_impressions_for_chat(chat_id, bot_name)
    ...
    await upsert_person_impression(chat_id, user_id, text, bot_name)
```

- [ ] **Step 5: post_process_group_culture 加 bot_name**

```python
async def post_process_group_culture(
    chat_id: str,
    diary_content: str,
    bot_name: str,
) -> None:
    ...
    await upsert_group_culture_gestalt(chat_id, gestalt_text, bot_name)
```

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/workers/diary_worker.py
git commit -m "feat(agent-service): diary_worker per-bot generation"
```

---

## Task 12: schedule_worker / journal_worker — 从 DB 读人设

**Files:**
- Modify: `apps/agent-service/app/workers/schedule_worker.py`
- Modify: `apps/agent-service/app/workers/journal_worker.py`

- [ ] **Step 1: schedule_worker — 替换 persona_core 加载**

找到加载 `persona_core` 的地方：

```bash
grep -n "persona_core" apps/agent-service/app/workers/schedule_worker.py
```

替换为从 DB 读（注意 schedule_worker 目前只为赤尾跑，先保留 bot_name 参数接口）：

```python
async def _get_persona_core_for_bot(bot_name: str) -> str:
    from app.orm.crud import get_bot_persona
    try:
        persona = await get_bot_persona(bot_name)
        return persona.persona_core if persona else ""
    except Exception as e:
        logger.warning(f"[{bot_name}] Failed to load persona_core: {e}")
        return ""
```

- [ ] **Step 2: journal_worker — 替换 persona_lite 加载**

```bash
grep -n "persona_lite" apps/agent-service/app/workers/journal_worker.py
```

类似替换，使用 `_get_persona_lite_for_bot(bot_name)`（可以从 diary_worker 共享此函数，或在各 worker 内独立定义）。

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/workers/schedule_worker.py apps/agent-service/app/workers/journal_worker.py
git commit -m "feat(agent-service): schedule/journal workers load persona from DB"
```

---

## Task 13: Seed bot_persona 数据

**Files:**
- Create: `apps/agent-service/scripts/seed_bot_persona.py`

- [ ] **Step 1: 创建 seed 脚本**

```python
#!/usr/bin/env python3
"""初始化 bot_persona 表数据

从 Langfuse 读取现有 persona_core/lite 内容迁移到 DB，
同时为千凪和绫奈创建占位数据（人设后续完善）。

运行: uv run python scripts/seed_bot_persona.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


CHIWEI_ERROR_MESSAGES = {
    "guard": "你发了一些赤尾不想讨论的话题呢~",
    "content_filter": "小尾有点不想讨论这个话题呢~",
    "error": "赤尾好像遇到了一些问题呢QAQ",
}

CHINAGI_DEFAULT_REPLY_STYLE = """--- 有人来找你倾诉 ---
千凪: 嗯，我在听，慢慢说
千凪: 没事的，这种事情很正常

--- 开心帮到人了 ---
千凪: 能帮上你就好 (◡‿◡✿)
千凪: 有什么不明白的随时问我~

--- 轻松的时候 ---
千凪: 哈哈今天的咖啡特别香
千凪: 一起！"""

CHINAGI_ERROR_MESSAGES = {
    "guard": "千凪觉得这个话题不太合适呢~",
    "content_filter": "这个千凪不太好说诶",
    "error": "千凪遇到了点小问题，等一下~",
}

AYANA_DEFAULT_REPLY_STYLE = """--- 不知道怎么办 ---
绫奈: 这个……这个是什么？
绫奈: 哦哦哦！原来是这样！

--- 开心 ---
绫奈: 哇！！！好厉害！！！
绫奈: 我我我也想学！

--- 害羞 ---
绫奈: 诶……这……
绫奈: 那个，谢谢你……"""

AYANA_ERROR_MESSAGES = {
    "guard": "绫奈…绫奈不懂这个…",
    "content_filter": "这个绫奈不知道诶…",
    "error": "绫奈好像做错什么了QAQ",
}


async def main():
    from app.agents.infra.langfuse_client import get_prompt
    from app.orm.base import AsyncSessionLocal
    from app.orm.models import BotPersona

    # 从 Langfuse 读取赤尾现有人设
    persona_core_text = get_prompt("persona_core").compile()
    persona_lite_text = get_prompt("persona_lite").compile()

    bots = [
        BotPersona(
            bot_name="chiwei",  # 替换为实际 bot_config.bot_name
            display_name="赤尾",
            persona_core=persona_core_text,
            persona_lite=persona_lite_text,
            default_reply_style="""--- 大家在闲聊，你被随便 cue ---
赤尾: 跟我有啥关系嘛，我又不健身！

--- 有人让你做事没心情 ---
赤尾: 不要～困死了啦""",
            error_messages=CHIWEI_ERROR_MESSAGES,
        ),
        BotPersona(
            bot_name="chinagi",  # 替换为实际 bot_config.bot_name
            display_name="千凪",
            persona_core="（千凪人设待完善）",
            persona_lite="你是千凪，温柔体贴的知心大姐姐。",
            default_reply_style=CHINAGI_DEFAULT_REPLY_STYLE,
            error_messages=CHINAGI_ERROR_MESSAGES,
        ),
        BotPersona(
            bot_name="ayana",  # 替换为实际 bot_config.bot_name
            display_name="绫奈",
            persona_core="（绫奈人设待完善）",
            persona_lite="你是绫奈，懵懂天真的小妹妹。",
            default_reply_style=AYANA_DEFAULT_REPLY_STYLE,
            error_messages=AYANA_ERROR_MESSAGES,
        ),
    ]

    async with AsyncSessionLocal() as session:
        for bot in bots:
            existing = await session.get(BotPersona, bot.bot_name)
            if existing:
                print(f"[skip] {bot.bot_name} 已存在")
            else:
                session.add(bot)
                print(f"[insert] {bot.bot_name}")
        await session.commit()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 确认 bot_config 中实际的 bot_name 值**

```
/ops-db SELECT bot_name, display_name FROM bot_config WHERE bot_role = 'persona';
```

根据查询结果更新 seed 脚本中的 `bot_name` 值（替换 "chiwei"/"chinagi"/"ayana"）。

- [ ] **Step 3: 在开发机本地运行 seed 脚本**

```bash
cd apps/agent-service && uv run python scripts/seed_bot_persona.py
```

- [ ] **Step 4: 验证数据已写入**

```
/ops-db SELECT bot_name, display_name FROM bot_persona;
```

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/scripts/seed_bot_persona.py
git commit -m "feat(agent-service): seed bot_persona data"
```

---

## Task 14: 运行全量测试 + 部署验证

- [ ] **Step 1: 运行全量单元测试**

```bash
cd apps/agent-service && uv run pytest tests/ -v
```

Expected: 全部 PASS，无新失败

- [ ] **Step 2: 提交分支，部署到测试泳道**

```bash
git push origin feat/multi-bot-context
```

然后：

```bash
make deploy APP=agent-service LANE=feat-multi-bot-context GIT_REF=feat/multi-bot-context
```

- [ ] **Step 3: 绑定 dev bot，端到端测试**

```
/ops bind TYPE=bot KEY=dev LANE=feat-multi-bot-context
```

在飞书 dev bot 群里发消息，验证：
1. 赤尾能正常回复（人设从 DB 加载）
2. 错误消息不再硬编码（可临时触发错误验证）
3. reply_style Redis key 格式正确（`reply_style:chat_id:chiwei`）

- [ ] **Step 4: 清理测试泳道**

```
/ops unbind TYPE=bot KEY=dev
make undeploy APP=agent-service LANE=feat-multi-bot-context
```
