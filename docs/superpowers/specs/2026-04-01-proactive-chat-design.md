# 赤尾主动搭话系统设计 — 群聊窥屏 MVP

> 赤尾不是在等你找她。你不找她的时候，她在过自己的日子。
> 她刷到了一个有意思的东西，想找人分享。
> — 赤尾宣言 2.1

## 一、背景与目标

赤尾当前的所有对话都是**被动触发**：用户 @赤尾 或命中关键词 → agent-service 处理 → 回复。这意味着赤尾永远不会主动说话，与宣言中"她是一个发光体，不是一面镜子"的定位矛盾。

**MVP 目标**：让赤尾能在群聊中**主动插话**——像一个朋友在群里看到有意思的内容忍不住接一句。

**MVP 范围**：
- 仅灰度 `oc_a44255e98af05f1359aeb29eeb503536` 一个群
- 仅做群聊窥屏插话，不做私聊主动搭话（后续迭代）
- 复用现有 chat_request → agent-service → chat_response 链路

## 二、触发机制

两层触发模拟人"刷手机"的自然节奏：

### 2.1 顺手刷（Piggyback Trigger）

**时机**：赤尾回复完任意一条消息后（不限于目标群）。

**类比**：回完一条微信，手机还在手里，顺手划一下看看群里在聊什么。

**实现位置**：agent-service `chat_consumer` 处理完 chat_request 后，异步触发一次窥屏扫描。

**概率门控**：不是每次都触发。设置一个基础概率（如 50-70%），模拟"有时顺手看看，有时直接放下手机"。概率值可从 Schedule 的当日基调动态微调。

**冷却**：如果 15 分钟内已经执行过一次扫描（无论是 piggyback 还是 cron 触发），跳过本次。这是唯一的硬性工程规则，目的是避免短时间内重复扫描浪费资源，不是频率控制。

### 2.2 闲着翻手机（Cron Fallback）

**时机**：没人找赤尾时的兜底，保证她不会因为没人 @ 就完全不看群。

**调度**：ARQ cron job 每 15 分钟触发一次，但每次执行时以约 40% 概率决定是否真的扫描（平均有效间隔约 35 分钟，但不固定）。加上 piggyback 触发的 15 分钟冷却互斥，实际节奏自然不规律。

**冷却状态**：Redis key `proactive:last_scan_time` 记录上次扫描时间戳，piggyback 和 cron 共用。

**下班静默**：23:00-09:00 不触发。赤尾在睡觉或还没醒，被 @ 可以回（起床气），但不会自己凑上来。

## 三、扫描与判断

两种触发方式共用同一套扫描 → 判断 → 投递流程。

### 3.1 拉取"没看过的消息"

```
1. target_chat_id = "oc_a44255e98af05f1359aeb29eeb503536"（MVP 硬编码）
2. last_presence_time = 赤尾在该群的最后一条 assistant 消息的 create_time
3. new_messages = 该群 last_presence_time 之后的所有 role="user" 的消息
4. 如果 new_messages 为空 → 跳过
```

**"不在场"语义**：`last_presence_time` 之后的消息 = 赤尾"没看过"的消息。她上次回复时"在场"，那些消息她已经"看到了"，不会再回头去接。

### 3.2 小模型判断

用小模型（如 gemini-2.0-flash）判断赤尾"想不想说话"。

**输入**：
| 字段 | 来源 | 作用 |
|------|------|------|
| `new_messages` | conversation_message 表 | 群里的新消息 |
| `reply_style` | Redis `reply_style:__base__` | 赤尾当前基调 |
| `group_culture` | `group_culture_gestalt` 表 | 群的氛围 |
| `recent_proactive` | 今天赤尾在该群的主动发言记录 | 让模型自然抑制频率 |

**判断标准**（写在 prompt 中，不是工程规则）：
1. **有人提到了赤尾**（不是 @，是在聊天中提到她）→ 自然想回应
2. **话题是赤尾感兴趣的** → 结合人设和群文化判断

**输出**：
```json
{
  "respond": true,
  "target_message_id": "om_xxx",  // 可选，想回复哪条具体消息
  "stimulus": "他们在聊最近新出的番，赤尾刚好也看了这个"
}
```

**频率控制**：不设硬性计数器或阈值。`recent_proactive` 作为上下文喂给小模型，模型自然判断"今天已经主动说了两次了，算了不凑了"。说得越多，上下文里记录越长，模型越倾向克制。这是赤尾自己的判断，不是工程规则。

### 3.3 硬性降噪规则

仅保留两条无法由模型兜底的硬规则：

| 规则 | 原因 |
|------|------|
| 不回"已在场"的消息 | 时序逻辑问题，模型无法感知"我当时在不在" |
| 下班不触发（23:00-09:00） | 触发层直接跳过，省 token |

## 四、Proactive Chat Request 构造

### 4.1 合成消息

现有 pipeline 强依赖 `message_id` 作为 context_builder 的入口。proactive 场景没有"用户对赤尾说的消息"，因此插入一条合成消息作为触发锚点：

```python
stimulus_msg = ConversationMessage(
    message_id=generate_id(),           # 合成 ID（前缀 "proactive_" 便于识别）
    chat_id=target_chat_id,
    chat_type="group",
    role="user",
    user_id="__proactive__",            # 特殊标记，不是真实用户
    content=stimulus_text,              # 小模型生成的 stimulus 描述
    message_type="proactive_trigger",   # 新类型，区分于普通 "text"
    reply_message_id=target_message_id, # 指向触发兴趣的那条真实消息（可选）
    create_time=now_ms()
)
```

### 4.2 chat_request 消息体

复用现有 `chat.request` 队列，消息体扩展一个字段：

```python
{
    "session_id": str(uuid4()),
    "message_id": stimulus_msg.message_id,
    "chat_id": target_chat_id,
    "is_p2p": False,
    "root_id": target_message_id or "",
    "user_id": "__proactive__",
    "bot_name": "chiwei",
    "lane": "prod",
    "is_proactive": True,               # 新增字段
    "enqueued_at": now_ms()
}
```

## 五、Pipeline 适配

### 5.1 chat_consumer

识别 `is_proactive` 字段。处理逻辑不变，仅在**处理完成后**触发 piggyback 扫描时跳过（避免"主动回复完又触发一次扫描"的递归）。

### 5.2 context_builder

检测到 `user_id == "__proactive__"` 或 `message_type == "proactive_trigger"` 时：

- **不把合成消息当作对话历史的一部分**
- 用合成消息的 `reply_message_id` 或 `create_time` 作为锚点，拉取真实的群聊消息作为上下文
- 其余逻辑（图片处理、quick_search 等）保持不变

### 5.3 inner_context / prompt

`build_inner_context()` 检测到 proactive 模式时，追加场景提示：

```
[场景] 你刚刷到了群里的对话。如果你想说点什么就说，不想说也可以不说。
不要刻意解释为什么突然说话，像朋友在群里自然接话就好。
```

stimulus 内容（小模型生成的"为什么想说话"）注入到 inner_context 中，作为赤尾的内在动机，不直接暴露给用户。

### 5.4 chat-response-worker

检测到 proactive 消息时调整发送方式：

- 如果有 `target_message_id`（root_id）：回复那条具体消息（飞书 reply_message 模式）
- 如果没有：直接发新消息到群里
- 如果 agent 返回空内容或明确表示"不想说"：静默丢弃，不发送

## 六、数据流总览

```
[触发层]
  顺手刷: chat_consumer 完成 → 概率门控 → 冷却检查 → 通过
  cron:   ARQ 定时 → 随机抖动 → 深夜检查 → 通过
      ↓
[扫描层]
  拉取目标群 last_presence 后的新消息
  无新消息 → 结束
      ↓
[判断层]
  小模型（gemini-2.0-flash）
  输入: 新消息 + reply_style + group_culture + 今日主动记录
  输出: respond? + target_message_id? + stimulus
  不想说 → 结束
      ↓
[投递层]
  插入合成 conversation_message（proactive_trigger 类型）
  构造 chat_request（is_proactive=true）→ RabbitMQ chat.request
      ↓
[处理层 — 复用现有链路]
  chat_consumer → context_builder（proactive 分支）→ main agent
  → chat_response → chat-response-worker → 飞书
```

## 七、改动清单

| 组件 | 改动 | 规模 |
|------|------|------|
| agent-service/workers/unified_worker.py | 新增 proactive_scan cron job | 新增 |
| agent-service/workers/chat_consumer.py | 处理完后触发 piggyback；识别 is_proactive 跳过递归 | 小改 |
| agent-service/workers/proactive_scanner.py | 新文件：扫描 + 小模型判断 + 合成消息 + 投递 | 新增（核心） |
| agent-service/agents/domains/main/context_builder.py | proactive 模式下的上下文构建分支 | 小改 |
| agent-service/services/memory_context.py | proactive 场景提示注入 | 小改 |
| lark-server/workers/chat-response-worker.ts | proactive 消息的发送模式（reply vs 新消息） | 小改 |
| Langfuse | 无需新 prompt，通过 inner_context 注入场景 | 无代码改动 |

## 八、后续迭代方向（不在 MVP 范围）

- **扩展到更多群**：去掉硬编码，用日记产出 + 近期互动作为资格门槛
- **私聊主动搭话**：内在状态（Schedule/Journal）产生冲动 → 印象系统选择对象 → 发起对话
- **消息事件流加速**：lark-server 侧加轻量信号，活跃群提前触发扫描
- **多模态窥屏**：感知群里的图片、表情包等非文本内容
