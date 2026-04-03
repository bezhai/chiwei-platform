# Multi-Bot 消息路由收敛设计

> Phase 2 of multi-bot architecture. Phase 1 (PR #124) established persona_id isolation (bot_persona table, BotContext, 7 context tables with persona_id column).

## 目标

将消息路由决策从 lark-server（per-bot 视角）收敛到 agent-service（全局视角），支持多 bot 同群、@ 路由、并行回复。

## 约束

- 同一群内不允许拉多个 persona_id 相同的 bot
- 本阶段只做 @ 路由，无 @ 群消息不回复（通用判断器留给 Phase 3）
- 不用工程规则解决不确定性问题

## 整体架构

```
飞书消息（N 个 bot 各收一份 webhook）
    ↓ ×N
lark-proxy（不变，每个 bot 独立转发）
    ↓ ×N
lark-server
  ├─ runRules 各 bot 各跑（utility 规则照旧处理）
  └─ makeTextReply 加 message_id 不可重入锁
       └─ 第一个抢到锁的发 MQ（中性消息 + mentions）
            ↓ ×1
agent-service
  ├─ chat_consumer 收到消息
  ├─ MessageRouter.route(message)
  │    ├─ P2P → bot_name 查 persona_id
  │    ├─ 群聊有 @ → mentions 匹配 bot_config → persona_id 列表
  │    └─ 群聊无 @ → []（不回复）
  ├─ 为每个 persona 并行 stream_chat()
  └─ 各自独立发 chat.response（带 bot_name）
            ↓ ×M（M = 需要回复的 persona 数）
chat-response-worker（按 bot_name 用对应凭据发送，不改）
```

## 一、lark-server 侧改动

### 1.1 makeTextReply 不可重入锁

在 `makeTextReply` 入口对 `message_id` 加 Redis 分布式锁（复用现有 `@RedisLock` 装饰器）。N 个 bot 的 runRules 各自跑到这里，只有第一个抢到锁的继续执行，其余静默退出。

### 1.2 MQ chat.request 消息格式变更

当前格式：

```json
{
  "session_id": "uuid",
  "message_id": "lark_msg_id",
  "chat_id": "lark_chat_id",
  "is_p2p": true,
  "root_id": "thread_root_id",
  "user_id": "sender_union_id",
  "bot_name": "fly",
  "lane": "feat-xxx",
  "enqueued_at": 1234567890
}
```

变更为：

```json
{
  "session_id": "uuid",
  "message_id": "lark_msg_id",
  "chat_id": "lark_chat_id",
  "is_p2p": true,
  "root_id": "thread_root_id",
  "user_id": "sender_union_id",
  "mentions": ["union_id_1", "union_id_2"],
  "bot_name": "fly",
  "lane": "feat-xxx",
  "enqueued_at": 1234567890
}
```

- 新增 `mentions`：消息中 @mention 的 union_id 列表
- `bot_name` 保留：P2P 场景下 router 用它反查 persona_id

### 1.3 不改动的部分

- runRules 引擎框架不变
- utility 规则（余额/帮助/repeat/meme）照旧 per-bot 执行
- 消息存储（MongoDB）不变，每个 bot 的 webhook 各自存一份
- lark-proxy 不变

## 二、agent-service 侧改动

### 2.1 MessageRouter 服务类

新建 `app/services/message_router.py`：

```python
class MessageRouter:
    async def route(
        self,
        chat_id: str,
        mentions: list[str],
        bot_name: str,
        is_p2p: bool,
    ) -> list[str]:
        """返回需要回复的 persona_id 列表。"""

        if is_p2p:
            return [await resolve_persona_id(bot_name)]

        if mentions:
            persona_ids = await self._resolve_mentioned_personas(
                chat_id, mentions
            )
            return persona_ids  # 可能为空（@ 的不是已注册 bot）

        return []  # 群聊无 @ → 不回复

        # --- Phase 3 扩展点 ---
        # route_proactive(chat_id, message)
        # route_no_mention(chat_id, message)
```

`_resolve_mentioned_personas` 查询：

```sql
SELECT DISTINCT persona_id
FROM bot_config
WHERE union_id = ANY(:mentions)
  AND is_active = true
```

### 2.2 chat_consumer 改造

```python
async def handle_chat_request(message):
    body = json.loads(message.body)

    router = MessageRouter()
    persona_ids = await router.route(
        chat_id=body["chat_id"],
        mentions=body.get("mentions", []),
        bot_name=body["bot_name"],
        is_p2p=body["is_p2p"],
    )

    if not persona_ids:
        return  # 无需回复

    await asyncio.gather(*[
        _process_for_persona(body, pid)
        for pid in persona_ids
    ])
```

每个 persona 独立 session_id，独立调用 `stream_chat`。

### 2.3 stream_chat 纯 persona_id 驱动

- 入参从 bot_name 改为 persona_id
- `BotContext` 新增 `from_persona_id(chat_id, persona_id)` 工厂方法
- bot_name 在 agent-service 内部完全消失，只在 MQ 边界存在

### 2.4 chat history 查询改造

消息表中存的是 bot_name（lark-server 写入，不改）。quick_search 查询时 join bot_config 拿 persona_id，返回结果用 persona_id 标识消息归属：

- 当前 persona 的消息 → AIMessage
- 其他 persona 的消息 → HumanMessage（带 persona display_name 前缀）
- 用户消息 → HumanMessage（带 username 前缀）

### 2.5 chat.response 消息

每个 persona 独立发 chat.response，带上从 persona_id + chat_id 反查的 bot_name：

```json
{
  "session_id": "per-persona-uuid",
  "bot_name": "fly",
  "content": "...",
  "is_last": true
}
```

chat-response-worker 无需改动——它已按 bot_name 选凭据发送。

## 三、bot_name 在系统中的角色

改造后，bot_name 只在两个 MQ 边界有意义：

| 位置 | 用途 |
|------|------|
| chat.request（入口） | P2P 场景 router 用它反查 persona_id |
| chat.response（出口） | worker 用它选飞书 API 凭据发送 |

agent-service 内部全程 persona_id 驱动。

## 四、数据模型变更

### 4.1 bot_config 表

需确认 `union_id` 列存在且填充了各 bot 在飞书的 union_id。`_resolve_mentioned_personas` 依赖此列。

### 4.2 agent_responses 表

当前 agent_responses 由 lark-server 创建（一条消息一条记录）。多 persona 并行回复后，一条用户消息可能产生 M 条 agent_responses（每个 persona 一条，各自独立 session_id）。

当前 lark-server 在 makeTextReply 中创建 agent_responses 记录（一条消息一条）。多 persona 并行后，需要 M 条记录。改为：

- lark-server 的 makeTextReply 不再创建 agent_responses 记录
- agent-service 的 `_process_for_persona` 为每个 persona 创建独立的 agent_responses 记录（独立 session_id）
- chat-response-worker 按 session_id 查 agent_responses，逻辑不变

## 五、兼容性

- 单 bot 群不受影响：只有一个 webhook → 一次 runRules → makeTextReply 无锁竞争 → 走原有流程
- P2P 不受影响：router 直接用 bot_name 查 persona_id
- chat-response-worker 不受影响：消息格式中 bot_name 字段不变
- lark-proxy 不受影响：纯转发，不关心消息内容
