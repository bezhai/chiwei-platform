# 漂移系统重构：观察-生成两阶段管线

## 背景

### 问题

identity_drift v1（文学独白）→ v2（行为示例）的实验验证了"漂移接管 reply-style"的方向是对的，但暴露了三个系统性问题：

1. **基模本能 vs 人设**：gemini 遇到知识类问题会切回 AI 助手模式，漂移和示例都压不住
2. **表达趋同**：主模型快速收敛到少数表达模式（特定颜文字、语气词），多样性衰减
3. **漂移-行为脱节**：漂移说"偏懒"但回复写 176 字科普，示例是间接信号，主模型可以选择性忽略

这些问题无法通过 prompt 补丁解决——每个补丁解决一个问题引入新问题。

### 核心思路

从"一次做对"改为"观察-纠正"。引入观察 agent 看赤尾的实际回复表现，发现偏差后通过下一轮 reply_style 温和拉回。不追求每条回复完美，但保证系统性偏差在 1-2 个漂移周期内收敛。

## 架构

```
赤尾回复完成
  → fire-and-forget: 两阶段锁（debounce 2min / flush 10条）
    → Agent 1（观察）：群聊事件 + 赤尾近期回复 + 基准人设 → 观察报告
    → Agent 2（生成）：观察报告 → reply_style
    → Redis: reply_style:{chat_id}

下一次主模型调用
  → get_reply_style() 从 Redis 读取 → 注入 main prompt 的 {{reply_style}}
```

两阶段锁机制不变（debounce 2min / flush 10 条），`_run_drift` 内部从单次 LLM 调用变成两次串行调用。对外接口完全不变。

## Agent 1：观察

### 输入

| 变量 | 来源 | 说明 |
|------|------|------|
| `schedule_daily` | DB `get_plan_for_period()` | 今日日程/基调 |
| `current_reply_style` | Redis 当前值 | 上一轮生成的 reply_style |
| `message_buffer` | DB 消息表 | 上次漂移以来的群聊消息时间线 |
| `recent_akao_replies` | DB 消息表（**新增**） | 赤尾最近 10 条回复原文 |
| `personality_anchor` | 写死在 prompt 中 | 基准人设，不从外部传入 |

`recent_akao_replies` 从现有 `get_chat_messages_in_range()` 过滤 `role == "assistant"` 获取，不需要新的 DB 查询。取最近 10 条，不限时间窗口。格式：

```
1. 那我会觉得有点寂寞吧... 也就指甲盖那么大一点点！哼 (｀^´)
2. 不去，风这么大，只想在被窝里看阿哈发癫 (-_-)
3. 抱着枕头滚来滚去，顺便想想怎么讹你的圣代 (￣▽￣)
```

编号是为了让观察 agent 可以引用具体哪条有问题。

### 输出（观察报告）

自然语言，大致结构：

```
## 情感状态
精力偏低，被追问有点烦但不至于炸

## 偏差诊断
- 最近 10 条回复平均 60 字，偏长。状态是"偏懒"时不应该展开回答
- 颜文字只用了 (￣▽￣) 和 (｀^´)，需要换
- 知识类问题连续认真回答了 3 次，应该敷衍或拒绝
- "略——！"出现 4 次，过于频繁

## 下一轮方向
示例要短（10 字以内为主），知识类问题给敷衍示例，颜文字换一批
```

关键：观察 agent 不生成示例，只输出诊断和方向。

### 模型

gpt-5.4（offline-model）

### Langfuse prompt

`drift_observer`

## Agent 2：生成

### 输入

观察 Agent 的完整报告（直接透传，不做裁剪）。

### 输出

reply_style，格式：

```
[一句话状态]

--- 场景描述 ---
示例回复
另一条示例

--- 另一个场景 ---
示例回复
```

Agent 2 不需要理解群聊上下文，不需要判断情感，只需要根据观察报告生成符合当前状态的行为示例。所有判断力都在 Agent 1，Agent 2 是纯执行。

调优重心在 Agent 1 的观察质量上。

### 模型

gpt-5.4（offline-model）

### Langfuse prompt

`drift_generator`

## Fallback

- Agent 1 失败 → 跳过整轮漂移，Redis 保留上一轮 reply_style
- Agent 2 失败 → 同上
- Redis 为空（首次/过期） → fallback 到 `_DEFAULT_REPLY_STYLE`（静态示例）

## 不变的部分

- 两阶段锁机制（debounce 2min / flush 10 条）
- Redis key `reply_style:{chat_id}`，TTL 24h
- 主模型消费方式（`get_reply_style()` → `{{reply_style}}`）
- post-safety 链路
- main prompt 结构（`{{reply_style}}` 变量注入 `<reply-style>` 区域）

## 代码改动范围

| 文件 | 改动 |
|------|------|
| `identity_drift.py` `_run_drift()` | 单次调用 → 两次串行调用（observer → generator） |
| `identity_drift.py` | 新增 `_get_recent_akao_replies()` |
| Langfuse | 新建 `drift_observer`、`drift_generator`，替代 `identity_drift` |
| 其他文件 | 不动 |
