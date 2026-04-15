# 跨 Chat 对话上下文打通

## 问题

同一个用户在不同 chat（群聊 vs 私聊）里跟同一个 persona 说话时，persona 完全不知道之前在别的 chat 聊过什么。群友反馈"像人格分裂"。

现状：
- `relationship_memory_v2` 按 `(persona_id, user_id)` 索引，已跨 chat — 赤尾知道"这个人是谁"
- `experience_fragment` 按 `source_chat_id` 隔离 — 赤尾不知道"我和这个人在别处聊了什么"
- `quick_search` 按 `chat_id` 查询 — 对话历史完全隔离

结果：用户在群里刚跟赤尾聊完笋干烧肉，转到私聊说"刚才说的那个事"，赤尾一脸茫然。

## 设计

### 核心思路

在 `build_inner_context()` 中新增一个 section：**跨 chat 互动上下文**。当 persona 在任何 chat 回复 user X 时，注入 user X 与该 persona 在其他 chat 的近期对话原文。

### 数据源范围

只查两类 chat（硬编码，后续可配置化）：
- **ka群**：`oc_54713c53ff0b46cb9579d3695e16cbf8`
- **当前 persona 与 trigger_user 的 p2p 对话**：`chat_type = 'p2p'` 且包含该 user

排除当前 chat 本身（已在 `quick_search` 中）。

### 裁切规则

1. **只取直接互动**：user X 发的消息 + persona 回复 X 的消息
2. **回复链展开**：如果 user X 的消息是回复某条群友消息，带上被回复的那条（1层），避免语境断裂
3. **时间窗口**：最近 24 小时
4. **条数上限**：每个 chat 最多 10 组交互（一问一答算一组，共 20 条消息 + 展开的回复链）
5. **排序**：按时间正序（旧→新），让 LLM 看到对话发展脉络

### 注入格式

作为 `inner_context` 的一个新 section，插在 relationship memory 之后、recent fragments 之前：

```
[你和 {username} 最近在其他地方的互动]

{chat_name} · {relative_time}:
  {username}: {message_text}
  你: {reply_text}

{chat_name} · {relative_time}:
  {context_user}: {replied_to_text}    ← 回复链展开（如有）
  {username}: {message_text}
  你: {reply_text}
```

如果没有跨 chat 互动，不注入此 section。

### 不注入的情况

- trigger_user 在其他 chat 没有与当前 persona 的互动记录
- 当前 chat 是唯一的互动 chat（没有"其他地方"）

## 实现要点

### 1. 数据库索引

`conversation_messages` 表需要新索引：

```sql
CREATE INDEX idx_conv_msg_user_bot_time 
ON conversation_messages(user_id, bot_name, create_time DESC);
```

### 2. 新增查询函数 `queries.py`

```python
async def find_cross_chat_interactions(
    session,
    user_id: str,
    bot_name: str,
    exclude_chat_id: str,
    allowed_chat_ids: list[str] | None,  # None = 只查 p2p
    since_hours: int = 24,
    limit_per_chat: int = 10,
) -> dict[str, list[ConversationMessage]]:
    """
    查找 user 与 bot 在其他 chat 的近期互动。
    
    返回 {chat_id: [messages]} 字典，每个 chat 最多 limit_per_chat 组交互。
    包含：
    - user 发送的消息（role='user', user_id=user_id）
    - bot 的回复（role='assistant', bot_name=bot_name, reply 关联到 user 的消息）
    - 被回复的上下文消息（1层回复链展开）
    """
```

查询逻辑：
1. 查 `conversation_messages` 中 `user_id = X AND bot_name = Y AND chat_id != current`
2. 对于 ka群：`chat_id = ka群id`
3. 对于 p2p：`chat_type = 'p2p' AND chat_id != current AND user_id = X`
4. 取这些消息的 message_id，再查 `reply_message_id` 指向它们的 assistant 消息
5. 对于有 `reply_message_id` 的 user 消息，展开被回复的那条

### 3. 新增格式化函数 `context.py`

```python
def format_cross_chat_context(
    interactions: dict[str, list[ConversationMessage]],
    username: str,
    chat_names: dict[str, str],  # chat_id → display name
) -> str:
    """将跨 chat 互动格式化为可读文本。"""
```

### 4. 修改 `build_inner_context()`

在 relationship memory section 之后调用：

```python
# 跨 chat 互动
cross_chat = await find_cross_chat_interactions(
    session,
    user_id=trigger_user_id,
    bot_name=persona_to_bot_name(persona_id),
    exclude_chat_id=chat_id,
    allowed_chat_ids=CROSS_CHAT_IDS,  # [ka群id]
)
if cross_chat:
    sections.append(format_cross_chat_context(
        cross_chat, trigger_username, chat_names
    ))
```

### 5. 配置

初始硬编码 `CROSS_CHAT_IDS = ["oc_54713c53ff0b46cb9579d3695e16cbf8"]`，后续可迁入 dynamic config。

## 边界情况

- **多个群都有互动**：按 chat 分组展示，每个 chat 独立裁切
- **p2p 对话找不到 chat_id**：通过 `chat_type='p2p' AND user_id=X AND bot_name=Y` 查询，不需要预知 chat_id
- **bot 消息关联**：assistant 消息的 `bot_name` 字段标识属于哪个 persona，`reply_message_id` 或时间邻近关联到 user 消息
- **消息内容格式**：`conversation_messages.content` 是 JSON（v2 格式），需要提取 `text` 字段
- **性能**：新索引 + 限定 chat_id 范围 + 24h 时间窗口，查询量可控

## 不做的事

- 不修改 `quick_search` — 当前 chat 的历史加载逻辑不变
- 不引入新表 — 复用现有 `conversation_messages`
- 不做全量跨 chat（只限 ka群 + p2p）
- 不做 glimpse 观察注入（后续迭代）
- 不修改 Langfuse prompt 模板 — 注入到 `inner_context` 变量中，模板无需改动
