# Multi-Bot 消息路由收敛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将消息路由决策从 lark-server 收敛到 agent-service，支持多 bot 同群 @ 路由并行回复。

**Architecture:** lark-server 每个 bot 独立跑 runRules，但 makeTextReply 用 Redis SETNX 保证同一消息只发一次 MQ。MQ 消息携带 mentions 列表。agent-service 新增 MessageRouter 解析 @mention → persona_id 列表，为每个 persona 并行调用 stream_chat。stream_chat 全程 persona_id 驱动，不依赖 bot_name。

**Tech Stack:** TypeScript (lark-server), Python/FastAPI (agent-service), Redis SETNX, RabbitMQ, PostgreSQL, SQLAlchemy

---

## 前提：bot_config 表结构

bot_config 表（由 lark-server TypeORM 管理）当前有 `robot_union_id` 列但缺少 `persona_id` 列。bot_context.py 已有 `SELECT persona_id FROM bot_config` 的查询，需确保该列存在。

---

### Task 1: bot_config 表添加 persona_id 列

**Files:**
- Modify: `apps/lark-server/src/infrastructure/dal/entities/bot-config.ts:4-43`

- [ ] **Step 1: 给 bot_config TypeORM entity 添加 persona_id 列**

```typescript
// apps/lark-server/src/infrastructure/dal/entities/bot-config.ts
// 在 bot_role 列（第 35-36 行）后面添加：

    @Column({ type: 'varchar', length: 50, nullable: true })
    persona_id?: string; // 关联 bot_persona.persona_id
```

TypeORM 的 `synchronize: true` 会自动同步 DDL。

- [ ] **Step 2: 确认 bot_config 表中已有数据的 persona_id 值**

通过 `/ops-db` 查看当前 bot_config 数据：

```sql
SELECT bot_name, robot_union_id, persona_id, bot_role FROM bot_config WHERE is_active = true;
```

如果 persona_id 为空，需要手动填充（例如 fly → akao）。这是运维操作，不在代码提交范围内，但部署前必须完成。

- [ ] **Step 3: Commit**

```bash
git add apps/lark-server/src/infrastructure/dal/entities/bot-config.ts
git commit -m "feat(lark-server): add persona_id column to bot_config entity"
```

---

### Task 2: lark-server makeTextReply 不可重入锁 + mentions

**Files:**
- Modify: `apps/lark-server/src/core/services/ai/reply.ts:1-53`

- [ ] **Step 1: 添加 Redis SETNX 不可重入检查**

`reply.ts` 当前内容（12-53 行）是一个普通 async 函数。在函数入口加 SETNX 检查：如果 message_id 已被处理，直接返回。

```typescript
import { Message } from 'core/models/message';
import { context } from '@middleware/context';
import { v4 as uuidv4 } from 'uuid';
import { AgentResponseRepository } from '@repositories/repositories';
import { AgentResponse } from '@entities/agent-response';
import { rabbitmqClient, CHAT_REQUEST, getLane } from '@integrations/rabbitmq';
import { setNx } from '@cache/redis-client';

/**
 * 队列模式回复：发布 chat.request 到 RabbitMQ，立即返回。
 * agent-service 异步处理后发布 chat.response，由 chat-response-worker 消费并发送。
 *
 * 多 bot 同群场景：同一消息会触发 N 个 bot 的 runRules，
 * 用 Redis SETNX 保证只有第一个到达的 bot 发送 MQ 消息。
 */
export async function makeTextReply(message: Message): Promise<void> {
    // 不可重入：同一 message_id 只处理一次（TTL 60s 兜底）
    const lockKey = `make_reply:${message.messageId}`;
    const locked = await setNx(lockKey, '1', 60);
    if (!locked) {
        console.info(
            `[makeTextReply] Skipped duplicate: message_id=${message.messageId}, bot=${context.getBotName()}`,
        );
        return;
    }

    const sessionId = uuidv4();

    // 创建 agent_responses 记录
    try {
        const agentResponse = AgentResponseRepository.create({
            session_id: sessionId,
            trigger_message_id: message.messageId,
            chat_id: message.chatId,
            bot_name: context.getBotName() || undefined,
            status: 'pending',
        } as Partial<AgentResponse>);
        await AgentResponseRepository.save(agentResponse);
    } catch (e) {
        console.error('Failed to create agent_response:', e);
    }

    // 发布到 chat.request 队列
    const lane = context.getLane() || getLane() || undefined;
    await rabbitmqClient.publish(
        CHAT_REQUEST,
        {
            session_id: sessionId,
            message_id: message.messageId,
            chat_id: message.chatId,
            is_p2p: message.isP2P(),
            root_id: message.rootId,
            user_id: message.senderInfo?.union_id,
            mentions: message.getMentionedUsers(),
            bot_name: context.getBotName(),
            is_canary: message.basicChatInfo?.permission_config?.is_canary ?? false,
            lane: lane || undefined,
            enqueued_at: Date.now(),
        },
        undefined,
        undefined,
        lane,
    );

    console.info(
        `[makeTextReply] Published chat.request: session_id=${sessionId}, message_id=${message.messageId}`,
    );
}
```

关键变更：
1. 导入 `setNx`，函数入口 SETNX 检查
2. MQ 消息体新增 `mentions: message.getMentionedUsers()`
3. `bot_name` 保留（P2P 场景和兼容）
4. agent_responses 记录创建保留（后续 Task 6 讨论是否迁移）

- [ ] **Step 2: Commit**

```bash
git add apps/lark-server/src/core/services/ai/reply.ts
git commit -m "feat(lark-server): add SETNX dedup + mentions to makeTextReply"
```

---

### Task 3: agent-service 新增 MessageRouter

**Files:**
- Create: `apps/agent-service/app/services/message_router.py`
- Create: `apps/agent-service/tests/unit/test_message_router.py`

- [ ] **Step 1: 编写 MessageRouter 测试**

```python
# apps/agent-service/tests/unit/test_message_router.py
import pytest
from unittest.mock import AsyncMock, patch

from app.services.message_router import MessageRouter


@pytest.fixture
def router():
    return MessageRouter()


@pytest.mark.asyncio
async def test_p2p_routes_to_bot_persona(router):
    """P2P 消息用 bot_name 反查 persona_id"""
    with patch(
        "app.services.message_router._resolve_persona_id",
        new_callable=AsyncMock,
        return_value="akao",
    ):
        result = await router.route(
            chat_id="c1", mentions=[], bot_name="fly", is_p2p=True
        )
    assert result == ["akao"]


@pytest.mark.asyncio
async def test_group_with_mention_routes_to_mentioned_personas(router):
    """群聊 @ 了已注册 bot → 返回对应 persona_id 列表"""
    with patch.object(
        router,
        "_resolve_mentioned_personas",
        new_callable=AsyncMock,
        return_value=["akao", "chinagi"],
    ):
        result = await router.route(
            chat_id="c1",
            mentions=["union_bot_fly", "union_bot_chinagi"],
            bot_name="fly",
            is_p2p=False,
        )
    assert result == ["akao", "chinagi"]


@pytest.mark.asyncio
async def test_group_with_mention_no_match_returns_empty(router):
    """群聊 @ 了非 bot 用户 → 返回空列表"""
    with patch.object(
        router,
        "_resolve_mentioned_personas",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await router.route(
            chat_id="c1",
            mentions=["union_some_user"],
            bot_name="fly",
            is_p2p=False,
        )
    assert result == []


@pytest.mark.asyncio
async def test_group_no_mention_returns_empty(router):
    """群聊无 @ → 不回复"""
    result = await router.route(
        chat_id="c1", mentions=[], bot_name="fly", is_p2p=False
    )
    assert result == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_message_router.py -v`
Expected: FAIL (import error, module not found)

- [ ] **Step 3: 实现 MessageRouter**

```python
# apps/agent-service/app/services/message_router.py
"""消息路由器 — 决定哪些 persona 应回复某条消息

Phase 2: 只做 @ 路由。
Phase 3 扩展点: 无 @ 通用判断器、主动发言路由。
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


async def _resolve_persona_id(bot_name: str) -> str:
    """从 bot_config 表查 persona_id（复用 bot_context 的逻辑）"""
    from app.services.bot_context import _resolve_persona_id as _resolve
    return await _resolve(bot_name)


class MessageRouter:
    """消息路由决策器"""

    async def route(
        self,
        chat_id: str,
        mentions: list[str],
        bot_name: str,
        is_p2p: bool,
    ) -> list[str]:
        """返回需要回复的 persona_id 列表。

        Args:
            chat_id: 会话 ID
            mentions: 消息中 @mention 的 union_id 列表
            bot_name: 发送 MQ 消息的 bot（抢到锁的那个）
            is_p2p: 是否私聊

        Returns:
            persona_id 列表，空列表表示不回复
        """
        if is_p2p:
            pid = await _resolve_persona_id(bot_name)
            logger.info("P2P route: bot_name=%s → persona_id=%s", bot_name, pid)
            return [pid]

        if mentions:
            persona_ids = await self._resolve_mentioned_personas(mentions)
            logger.info(
                "Group @mention route: mentions=%s → persona_ids=%s",
                mentions, persona_ids,
            )
            return persona_ids

        # 群聊无 @ → 不回复（Phase 3 扩展点）
        return []

    async def _resolve_mentioned_personas(
        self, mentions: list[str]
    ) -> list[str]:
        """将 mention 的 union_id 列表映射到 persona_id 列表

        查询 bot_config 表：mention 的 union_id 匹配 robot_union_id 列。
        """
        from app.orm.base import AsyncSessionLocal
        from sqlalchemy import text

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT persona_id FROM bot_config "
                    "WHERE robot_union_id = ANY(:mentions) "
                    "AND is_active = true "
                    "AND persona_id IS NOT NULL"
                ),
                {"mentions": mentions},
            )
            return [row[0] for row in result.fetchall()]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_message_router.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/message_router.py apps/agent-service/tests/unit/test_message_router.py
git commit -m "feat(agent-service): add MessageRouter for @mention routing"
```

---

### Task 4: BotContext 支持 from_persona_id 工厂方法

**Files:**
- Modify: `apps/agent-service/app/services/bot_context.py:1-97`
- Modify: `apps/agent-service/tests/unit/test_bot_context.py`

- [ ] **Step 1: 编写测试**

在 `tests/unit/test_bot_context.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_from_persona_id_factory():
    """from_persona_id 工厂方法应直接用 persona_id 创建 BotContext"""
    with patch(
        "app.services.bot_context._resolve_bot_name_for_persona",
        new_callable=AsyncMock,
        return_value="fly",
    ), patch(
        "app.services.bot_context.get_bot_persona",
        new_callable=AsyncMock,
        return_value=MagicMock(
            persona_lite="我是赤尾",
            display_name="赤尾",
            default_reply_style="test style",
            error_messages={},
        ),
    ), patch(
        "app.services.bot_context.get_reply_style",
        new_callable=AsyncMock,
        return_value="test style",
    ):
        ctx = await BotContext.from_persona_id(chat_id="c1", persona_id="akao", chat_type="group")

    assert ctx.persona_id == "akao"
    assert ctx.bot_name == "fly"
    assert ctx.get_identity() == "我是赤尾"


def test_build_chat_history_uses_persona_id():
    """build_chat_history 按 persona_id 而非 bot_name 判断自己的消息"""
    ctx = BotContext(chat_id="c1", bot_name="fly", chat_type="group")
    ctx._persona_id = "akao"
    msgs = [
        _make_msg("assistant", "你好", "fly", "赤尾"),
        _make_msg("assistant", "我是千凪", "chinagi", "千凪"),
        _make_msg("user", "嗨", None, "张三"),
    ]
    # 给消息加上 persona_id 属性（模拟 quick_search join 后的结果）
    msgs[0].persona_id = "akao"
    msgs[1].persona_id = "chinagi"
    msgs[2].persona_id = None

    result = ctx.build_chat_history(msgs)
    assert isinstance(result[0], AIMessage)  # akao 的消息 → AI
    assert isinstance(result[1], HumanMessage)  # chinagi 的消息 → Human
    assert isinstance(result[2], HumanMessage)  # 用户消息 → Human
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_bot_context.py -v`
Expected: FAIL (from_persona_id not found, persona_id attribute missing)

- [ ] **Step 3: 实现 BotContext 改动**

```python
# apps/agent-service/app/services/bot_context.py — 完整替换
"""Bot 上下文容器 — per-(chat_id, persona_id) 的所有上下文数据"""
import logging
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage

if TYPE_CHECKING:
    from app.orm.models import BotPersona
    from app.services.quick_search import QuickSearchResult

logger = logging.getLogger(__name__)


async def _resolve_persona_id(bot_name: str) -> str:
    """从 bot_config 表查 persona_id，找不到则用 bot_name 自身"""
    from app.orm.base import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT persona_id FROM bot_config WHERE bot_name = :bn"),
            {"bn": bot_name},
        )
        row = result.scalar_one_or_none()
        return row if row else bot_name


async def _resolve_bot_name_for_persona(persona_id: str, chat_id: str = "") -> str:
    """从 persona_id 反查 bot_name（同群约束下唯一）"""
    from app.orm.base import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT bot_name FROM bot_config "
                "WHERE persona_id = :pid AND is_active = true "
                "LIMIT 1"
            ),
            {"pid": persona_id},
        )
        row = result.scalar_one_or_none()
        return row if row else persona_id


class BotContext:
    def __init__(self, chat_id: str, bot_name: str, chat_type: str) -> None:
        self.chat_id = chat_id
        self.bot_name = bot_name
        self.chat_type = chat_type
        self._persona_id: str = ""
        self._persona: "BotPersona | None" = None
        self._reply_style: str = ""

    @classmethod
    async def from_persona_id(
        cls, chat_id: str, persona_id: str, chat_type: str
    ) -> "BotContext":
        """从 persona_id 创建 BotContext（多 bot 路由场景）"""
        bot_name = await _resolve_bot_name_for_persona(persona_id, chat_id)
        ctx = cls(chat_id=chat_id, bot_name=bot_name, chat_type=chat_type)
        ctx._persona_id = persona_id
        await ctx._load_persona()
        return ctx

    @property
    def persona_id(self) -> str:
        return self._persona_id

    async def load(self) -> None:
        """并行加载所有 per-bot 数据（从 bot_name 入口）"""
        self._persona_id = await _resolve_persona_id(self.bot_name)
        await self._load_persona()

    async def _load_persona(self) -> None:
        """加载 persona 数据和 reply_style"""
        from app.orm.crud import get_bot_persona
        from app.services.memory_context import get_reply_style

        self._persona = await get_bot_persona(self._persona_id)
        if self._persona is None:
            logger.warning(
                f"BotPersona not found for persona_id={self._persona_id} "
                f"(bot_name={self.bot_name}), using defaults"
            )

        default_style = self._persona.default_reply_style if self._persona else ""
        self._reply_style = await get_reply_style(
            self.chat_id, self._persona_id, default_style
        )

    @property
    def reply_style(self) -> str:
        return self._reply_style

    def get_identity(self) -> str:
        """返回注入 {{identity}} 的人设文本"""
        return self._persona.persona_lite if self._persona else ""

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
        """构建 LLM 对话历史：当前 persona → AIMessage，其余 → HumanMessage（带名字前缀）"""
        result: list[AIMessage | HumanMessage] = []
        for msg in messages:
            # 优先用 persona_id 判断，fallback 到 bot_name（兼容旧数据）
            msg_persona_id = getattr(msg, "persona_id", None)
            if msg_persona_id:
                is_self = msg.role == "assistant" and msg_persona_id == self._persona_id
            else:
                is_self = msg.role == "assistant" and getattr(msg, "bot_name", None) == self.bot_name

            if is_self:
                result.append(AIMessage(content=msg.content))
            else:
                if msg.username:
                    content = f"{msg.username}: {msg.content}"
                else:
                    content = msg.content
                result.append(HumanMessage(content=content))
        return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_bot_context.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/services/bot_context.py apps/agent-service/tests/unit/test_bot_context.py
git commit -m "feat(bot-context): add from_persona_id factory + persona_id-based history"
```

---

### Task 5: quick_search 返回 persona_id

**Files:**
- Modify: `apps/agent-service/app/services/quick_search.py:13-152`

- [ ] **Step 1: QuickSearchResult 添加 persona_id 属性**

在 `QuickSearchResult.__init__`（第 16-40 行）中添加 `persona_id` 参数：

```python
class QuickSearchResult:
    """搜索结果项"""

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
        bot_name: str | None = None,
        persona_id: str | None = None,
    ):
        self.message_id = message_id
        self.content = content
        self.user_id = user_id
        self.create_time = create_time
        self.role = role
        self.username = username
        self.chat_type = chat_type
        self.chat_name = chat_name
        self.reply_message_id = reply_message_id
        self.chat_id = chat_id
        self.bot_name = bot_name
        self.persona_id = persona_id
```

- [ ] **Step 2: quick_search 查询 join bot_config 获取 persona_id**

修改 `quick_search` 函数中的两个 select 语句，加入 bot_config join。在导入部分（第 10 行）添加 `text`：

```python
from sqlalchemy import select, text
```

修改第 75-89 行的 root_result 查询，在 `.outerjoin(LarkGroupChatInfo, ...)` 之后加入 bot_config 子查询 join：

```python
        # 2. 获取同一root_message_id的所有消息
        # 用子查询 join bot_config 获取 persona_id
        from sqlalchemy import literal_column
        from sqlalchemy.orm import aliased

        bot_config_persona = (
            select(
                literal_column("bot_config.bot_name").label("bc_bot_name"),
                literal_column("bot_config.persona_id").label("bc_persona_id"),
            )
            .select_from(text("bot_config"))
            .where(literal_column("bot_config.is_active") == True)
            .subquery("bc")
        )

        root_result = await session.execute(
            select(
                ConversationMessage,
                LarkUser.name.label("username"),
                LarkGroupChatInfo.name.label("chat_name"),
                bot_config_persona.c.bc_persona_id.label("persona_id"),
            )
            .outerjoin(LarkUser, ConversationMessage.user_id == LarkUser.union_id)
            .outerjoin(
                LarkGroupChatInfo,
                ConversationMessage.chat_id == LarkGroupChatInfo.chat_id,
            )
            .outerjoin(
                bot_config_persona,
                ConversationMessage.bot_name == bot_config_persona.c.bc_bot_name,
            )
            .where(ConversationMessage.root_message_id == current_msg.root_message_id)
            .where(ConversationMessage.create_time <= current_msg.create_time)
            .order_by(ConversationMessage.create_time.asc())
        )
        root_rows = root_result.all()
        root_messages: list[tuple[ConversationMessage, str | None, str | None, str | None]] = [
            (row[0], row[1], row[2], row[3]) for row in root_rows
        ]
```

对 additional_result 查询（第 102-121 行）做同样的 join：

```python
            additional_result = await session.execute(
                select(
                    ConversationMessage,
                    LarkUser.name.label("username"),
                    LarkGroupChatInfo.name.label("chat_name"),
                    bot_config_persona.c.bc_persona_id.label("persona_id"),
                )
                .outerjoin(LarkUser, ConversationMessage.user_id == LarkUser.union_id)
                .outerjoin(
                    LarkGroupChatInfo,
                    ConversationMessage.chat_id == LarkGroupChatInfo.chat_id,
                )
                .outerjoin(
                    bot_config_persona,
                    ConversationMessage.bot_name == bot_config_persona.c.bc_bot_name,
                )
                .where(
                    ConversationMessage.chat_id == current_msg.chat_id,
                    ConversationMessage.root_message_id != current_msg.root_message_id,
                    ConversationMessage.create_time >= time_threshold,
                    ConversationMessage.create_time < current_msg.create_time,
                )
                .order_by(ConversationMessage.create_time.desc())
                .limit(needed)
            )
            additional_rows = additional_result.all()
            additional_messages = [(row[0], row[1], row[2], row[3]) for row in additional_rows]
```

- [ ] **Step 3: 结果构建中传递 persona_id**

修改第 132-150 行的结果构建循环：

```python
        results = []
        for msg, username, chat_name, persona_id in all_messages:
            results.append(
                QuickSearchResult(
                    message_id=str(msg.message_id),
                    content=str(msg.content),
                    user_id=str(msg.user_id),
                    create_time=datetime.fromtimestamp(msg.create_time / 1000),
                    role=str(msg.role),
                    username=username if msg.role == "user" else (msg.bot_name or "assistant"),
                    bot_name=msg.bot_name if msg.role == "assistant" else None,
                    persona_id=persona_id if msg.role == "assistant" else None,
                    chat_type=str(msg.chat_type),
                    chat_name=chat_name,
                    reply_message_id=(
                        str(msg.reply_message_id) if msg.reply_message_id else None
                    ),
                    chat_id=msg.chat_id,
                )
            )
```

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/services/quick_search.py
git commit -m "feat(quick-search): join bot_config to return persona_id in results"
```

---

### Task 6: context_builder 切换到 persona_id

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/context_builder.py:32-36, 159-167, 303-349`

- [ ] **Step 1: build_chat_context 参数从 current_bot_name 改为 current_persona_id**

修改函数签名（第 32-36 行）：

```python
async def build_chat_context(
    message_id: str,
    current_persona_id: str = "",
    limit: int = 10,
) -> tuple[list[HumanMessage | AIMessage], ImageRegistry | None, str, str, str, str, str, list[str]]:
```

- [ ] **Step 2: _build_p2p_messages 参数改为 current_persona_id**

修改第 159-167 行的调用和第 303-355 行的函数：

在 `build_chat_context` 中（第 164-167 行）：

```python
    else:
        messages = _build_p2p_messages(
            l1_results, image_key_to_url, image_key_to_filename,
            current_persona_id=current_persona_id,
        )
```

修改 `_build_p2p_messages` 签名（第 303-308 行）：

```python
def _build_p2p_messages(
    messages: list[QuickSearchResult],
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
    current_persona_id: str = "",
) -> list[HumanMessage | AIMessage]:
```

修改第 344-349 行的 is_self 判断：

```python
        # 当前 persona 自己的消息 → AIMessage，其余 → HumanMessage
        msg_persona_id = getattr(msg, "persona_id", None)
        if msg_persona_id:
            is_self = msg.role == "assistant" and msg_persona_id == current_persona_id
        else:
            is_self = False
```

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/context_builder.py
git commit -m "refactor(context-builder): switch from bot_name to persona_id"
```

---

### Task 7: stream_chat + _build_and_stream 接受 persona_id

**Files:**
- Modify: `apps/agent-service/app/agents/domains/main/agent.py:32-42, 48-113, 244-289`

- [ ] **Step 1: stream_chat 添加 persona_id 参数**

修改第 48-50 行：

```python
async def stream_chat(
    message_id: str, session_id: str | None = None, persona_id: str | None = None,
) -> AsyncGenerator[str, None]:
```

修改第 82-84 行（guard 消息和 bot_name 获取）：

```python
            # 获取 guard 消息：优先用 persona_id，fallback header_vars
            effective_persona = persona_id or header_vars["app_name"].get() or ""
            guard_message = await _get_guard_message(effective_persona)
```

修改第 101-103 行和第 108-109 行（传递 persona_id 给 _build_and_stream）：

```python
                async for text in _build_and_stream(
                    message_id, gray_config, request_id, persona_id=persona_id
                ):
```

```python
                raw_stream = _build_and_stream(
                    message_id, gray_config, request_id, persona_id=persona_id
                )
```

- [ ] **Step 2: _build_and_stream 接受 persona_id，改用 BotContext.from_persona_id**

修改第 244-248 行签名：

```python
async def _build_and_stream(
    message_id: str,
    gray_config: dict,
    session_id: str | None = None,
    persona_id: str | None = None,
) -> AsyncGenerator[str, None]:
```

修改第 253-254 行（bot_name 获取）和第 274-289 行（上下文构建 + BotContext 创建）：

```python
    # 获取 bot 标识：优先 persona_id（路由场景），fallback header_vars（兼容）
    bot_name = header_vars["app_name"].get() or ""

    # 构建上下文
    (
        messages,
        image_registry,
        chat_id,
        trigger_username,
        chat_type,
        trigger_user_id,
        chat_name,
        chain_user_ids,
    ) = await build_chat_context(
        message_id,
        current_persona_id=persona_id or "",
    )
    CHAT_PIPELINE_DURATION.labels(stage="context_build").observe(time.monotonic() - t_build_start)

    # 创建并加载 BotContext
    if persona_id:
        bot_ctx = await BotContext.from_persona_id(
            chat_id=chat_id, persona_id=persona_id, chat_type=chat_type
        )
    else:
        # 兼容：没有 persona_id 时走老路径
        bot_ctx = BotContext(chat_id=chat_id, bot_name=bot_name, chat_type=chat_type)
        await bot_ctx.load()
```

- [ ] **Step 3: _get_guard_message 兼容 persona_id 入参**

修改第 32-41 行：

```python
async def _get_guard_message(persona_or_bot: str) -> str:
    """获取 guard 拒绝消息（persona/bot 专属，fallback 为通用消息）"""
    try:
        from app.orm.crud import get_bot_persona
        persona = await get_bot_persona(persona_or_bot)
        if persona and persona.error_messages:
            return persona.error_messages.get("guard", "不想讨论这个话题呢~")
    except Exception as e:
        logger.warning(f"Failed to get guard message for {persona_or_bot}: {e}")
    return "不想讨论这个话题呢~"
```

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/agents/domains/main/agent.py
git commit -m "feat(stream-chat): accept persona_id, use BotContext.from_persona_id"
```

---

### Task 8: chat_consumer 集成 MessageRouter

**Files:**
- Modify: `apps/agent-service/app/workers/chat_consumer.py:36-222`

- [ ] **Step 1: 重构 handle_chat_request 引入 MessageRouter**

替换第 36-222 行的 `handle_chat_request` 函数。核心变更：
1. 提取 mentions 字段
2. 调用 MessageRouter.route() 获取 persona_id 列表
3. 为每个 persona 并行执行 `_process_for_persona`

```python
async def handle_chat_request(message: AbstractIncomingMessage) -> None:
    """消费 chat_request queue 中的消息，路由到对应 persona 并行处理"""
    async with message.process(requeue=False):
        body = json.loads(message.body)
        session_id = body.get("session_id")
        message_id = body.get("message_id")
        chat_id = body.get("chat_id")
        is_p2p = body.get("is_p2p", False)
        root_id = body.get("root_id")
        user_id = body.get("user_id")
        lane = body.get("lane")
        bot_name = body.get("bot_name")
        is_proactive = body.get("is_proactive", False)
        mentions = body.get("mentions", [])

        # MQ consumer 不走 HTTP 中间件，手动注入 contextvars
        if bot_name:
            header_vars["app_name"].set(bot_name)
        if lane:
            header_vars["lane"].set(lane)

        logger.info(
            "Chat request received: session_id=%s, message_id=%s, lane=%s, bot_name=%s, mentions=%s",
            session_id, message_id, lane, bot_name, mentions,
        )

        # 路由：决定哪些 persona 回复
        from app.services.message_router import MessageRouter

        router = MessageRouter()
        persona_ids = await router.route(
            chat_id=chat_id,
            mentions=mentions,
            bot_name=bot_name or "",
            is_p2p=is_p2p,
        )

        if not persona_ids:
            logger.info("No persona to reply: message_id=%s", message_id)
            return

        # 构建公共 payload
        base_payload = {
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

        if len(persona_ids) == 1:
            # 单 persona：复用原始 session_id
            await _process_for_persona(base_payload, persona_ids[0])
        else:
            # 多 persona：并行处理，各自独立 session_id
            import asyncio
            await asyncio.gather(*[
                _process_for_persona(base_payload, pid)
                for pid in persona_ids
            ])


async def _process_for_persona(base_payload: dict, persona_id: str) -> None:
    """为单个 persona 执行完整的 stream_chat + 发布 response 流程"""
    import time
    import traceback
    from uuid import uuid4

    t_start = time.monotonic()
    message_id = base_payload["message_id"]
    lane = base_payload.get("lane")
    is_proactive = base_payload.get("is_proactive", False)

    # 多 persona 场景下每个 persona 需要独立 session_id
    session_id = base_payload["session_id"]

    # 从 persona_id 反查 bot_name（用于 chat.response）
    from app.services.bot_context import _resolve_bot_name_for_persona
    response_bot_name = await _resolve_bot_name_for_persona(persona_id)

    client = RabbitMQClient.get_instance()

    base_response = {
        "session_id": session_id,
        "message_id": message_id,
        "chat_id": base_payload["chat_id"],
        "is_p2p": base_payload["is_p2p"],
        "root_id": base_payload.get("root_id"),
        "user_id": base_payload.get("user_id"),
        "lane": lane,
        "is_proactive": is_proactive,
        "bot_name": response_bot_name,
    }

    # Measure MQ queue wait time
    queue_wait_ms = 0.0
    enqueued_at = base_payload.get("enqueued_at")
    if enqueued_at:
        queue_wait_s = (time.time() * 1000 - enqueued_at) / 1000
        queue_wait_ms = queue_wait_s * 1000
        CHAT_QUEUE_WAIT.observe(queue_wait_s)

    try:
        sent_length = 0
        messages_sent = 0
        full_content = ""
        t_first_token: float | None = None
        token_count = 0

        async for text in stream_chat(
            message_id, session_id=session_id, persona_id=persona_id
        ):
            if not text:
                continue
            if t_first_token is None:
                t_first_token = time.monotonic()
            token_count += 1
            full_content += text

            # 检测分隔符，逐段发送
            pending = full_content[sent_length:]
            while SPLIT_MARKER in pending and messages_sent < MAX_MESSAGES - 1:
                idx = pending.index(SPLIT_MARKER)
                part = pending[:idx].strip()
                if part:
                    base_response["published_at"] = int(time.time() * 1000)
                    await client.publish(
                        CHAT_RESPONSE,
                        {
                            **base_response,
                            "content": part,
                            "status": "success",
                            "part_index": messages_sent,
                        },
                        lane=lane,
                    )
                    messages_sent += 1
                    logger.info(
                        "Chat response part %d published: session_id=%s, persona=%s",
                        messages_sent - 1, session_id, persona_id,
                    )
                sent_length += idx + len(SPLIT_MARKER)
                pending = full_content[sent_length:]

        # 流结束
        t_stream_end = time.monotonic()
        stream_ms = (t_stream_end - t_start) * 1000
        if t_first_token is not None:
            CHAT_FIRST_TOKEN.observe(t_first_token - t_start)
        CHAT_PIPELINE_DURATION.labels(stage="agent_stream").observe(t_stream_end - t_start)
        CHAT_TOKENS.labels(type="text").inc(token_count)

        remaining = full_content[sent_length:].replace(SPLIT_MARKER, "").strip()
        clean_full = full_content.replace(SPLIT_MARKER, "\n\n").strip()

        t_publish_start = time.monotonic()
        if remaining or messages_sent == 0:
            base_response["published_at"] = int(time.time() * 1000)
            await client.publish(
                CHAT_RESPONSE,
                {
                    **base_response,
                    "content": remaining or full_content,
                    "full_content": clean_full,
                    "status": "success",
                    "part_index": messages_sent,
                    "is_last": True,
                },
                lane=lane,
            )
        else:
            base_response["published_at"] = int(time.time() * 1000)
            await client.publish(
                CHAT_RESPONSE,
                {
                    **base_response,
                    "content": "",
                    "full_content": clean_full,
                    "status": "success",
                    "part_index": messages_sent,
                    "is_last": True,
                },
                lane=lane,
            )

        logger.info(
            "Chat response final part %d published: session_id=%s, persona=%s",
            messages_sent, session_id, persona_id,
        )

        t_end = time.monotonic()
        publish_ms = (t_end - t_publish_start) * 1000
        total_ms = (t_end - t_start) * 1000
        ttft_ms = (t_first_token - t_start) * 1000 if t_first_token is not None else 0.0
        CHAT_PIPELINE_DURATION.labels(stage="mq_publish").observe(t_end - t_publish_start)
        CHAT_PIPELINE_DURATION.labels(stage="total").observe(t_end - t_start)
        logger.info(
            "chat_request_done",
            extra={
                "event": "chat_request_done",
                "session_id": session_id,
                "persona_id": persona_id,
                "queue_wait_ms": round(queue_wait_ms),
                "stream_ms": round(stream_ms),
                "ttft_ms": round(ttft_ms),
                "publish_ms": round(publish_ms),
                "total_ms": round(total_ms),
                "tokens": token_count,
                "parts": messages_sent + 1,
            },
        )

    except Exception as e:
        logger.error(
            "Chat request failed: session_id=%s, persona=%s, error=%s\n%s",
            session_id, persona_id, str(e), traceback.format_exc(),
        )
        await client.publish(
            CHAT_RESPONSE,
            {
                **base_response,
                "content": "",
                "status": "failed",
                "error": str(e),
            },
            lane=lane,
        )
```

`_maybe_piggyback_scan` 和 `start_chat_consumer` 保持不变。

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/workers/chat_consumer.py
git commit -m "feat(chat-consumer): integrate MessageRouter for multi-persona routing"
```

---

### Task 9: 验证 + 兼容性检查

**Files:** 无新文件，只跑测试

- [ ] **Step 1: 运行 agent-service 全量单元测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/ -v`

检查是否有因 `current_bot_name` → `current_persona_id` 参数名变更导致的失败。如有，修复调用方。

- [ ] **Step 2: 检查所有 build_chat_context 调用点**

Run: `cd apps/agent-service && grep -rn "build_chat_context\|current_bot_name" app/`

确保所有调用都已从 `current_bot_name=` 改为 `current_persona_id=`。

- [ ] **Step 3: 检查所有 stream_chat 调用点**

Run: `cd apps/agent-service && grep -rn "stream_chat" app/`

确保 proactive_scanner 等其他调用 stream_chat 的地方也兼容新的 persona_id 参数（新参数有默认值 None，不传则走兼容路径）。

- [ ] **Step 4: Commit 任何修复**

```bash
git add -A
git commit -m "fix: update all callsites for persona_id parameter changes"
```
