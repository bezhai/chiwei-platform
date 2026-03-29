# 赤尾上下文系统 v3 Phase 2 — Identity 漂移 + Schedule 多 Agent

> 2026-03-28 | bezhai × Claude 基于 Phase 1 实测复盘 + 群聊数据分析
> 基于 `2026-03-27-context-system-v3-design.md`（v3 spec），本文档是 Phase 2 增量设计

---

## 一、Phase 1 回顾与 Phase 2 方向修正

### 1.1 Phase 1 实测数据驱动的发现

对主群（oc_a44255e98af05f1359aeb29eeb503536）最近 7 天数据分析：

| 发现 | 数据 | 影响 |
|------|------|------|
| 对话节奏是爆发式 | 一天 2-3 波（14-17点、20-21点），94% 消息间隔 < 5 分钟 | identity 不能只靠每日一次刷新 |
| 赤尾深度参与 3-4 次/天 | 每次 30-120 分钟，回复 20-115 次 | 会话内 identity 不能僵死 |
| 群聊不是一问一答 | 多人穿插提问 + 起哄，赤尾回复前已积累多条不同人的消息 | 漂移输入必须是"一段消息流"而非"一条消息" |
| 日记质量已 OK | Phase 1 的 prompt 调优生效，无 ins 网红风 | 日记多 Agent 优先级降低 |
| Schedule/Journal 是弱环 | Journal 过度模糊化、千篇一律；Schedule 有 ins 网红风 | 多 Agent 应用在 Schedule 而非 Diary |
| Schedule daily 是最好的 identity 信号源 | 按上午/下午/晚上分时段，有 mood 和 energy | identity 漂移的基调输入 |

### 1.2 Phase 2 优先级（修正后）

```
P0: Identity 漂移状态机 — 两阶段锁 + 异步 post 模式
P1: Schedule 多 Agent 管线 — 创意→写作→审查，提升 Schedule 质量
P2: Journal prompt 优化 — Schedule 质量提升后跟进
```

原 spec 中"方向 A：日记多 Agent 协作"降级为 P3（日记质量已 OK）。

---

## 二、Identity 漂移状态机

### 2.1 核心思路

当前 identity 是静态的（写死在 Langfuse `main` prompt 里）。Phase 1 的实验证明：无论怎么调措辞，静态 identity 无法让赤尾在不同情境下表现自然——压短就冷、加元气就长。

解决方案：**identity 由独立 LLM 动态生成，注入 inner_context**。静态 identity 砍到最小（名字、外貌、与主人的关系），所有性格、状态、语气特征由漂移状态机驱动。

### 2.2 为什么不是每条消息触发 / 每 N 分钟触发

| 方案 | 问题 |
|------|------|
| 每条消息触发 | 群聊密度高（峰值 60条/10min），赤尾一天回复 50-150 次，LLM 调用爆炸 |
| 只在会话边界触发 | 一个会话内 20 条问答，identity 僵死不变 |
| 固定 N 分钟 | 不跟对话节奏，活跃时更新不及时，空闲时浪费 |
| 赤尾每次回复后触发 | 群聊是多人穿插+起哄，不是一问一答，回复后上下文随时在变 |

### 2.3 两阶段锁模型

**每个群/私聊维护一个独立的漂移锁。**

```
[空闲]
  │
  消息到达 → 获取锁 → 进入一阶段
  │
  ┌──────────────────────────────────────────────────┐
  │  一阶段：消息收集（可中断）                        │
  │                                                    │
  │  · 启动 N 秒计时器                                 │
  │  · 新消息到达 → 加入缓冲区，重置计时器             │
  │  · 计时器到期（N 秒无新消息）→ 进入二阶段          │
  │  · 缓冲区超过 M 条 → 强制进入二阶段               │
  └──────────────────────────────────────────────────┘
  │
  ┌──────────────────────────────────────────────────┐
  │  二阶段：LLM 漂移计算（不可中断）                  │
  │                                                    │
  │  输入：缓冲区全部消息 + 当前 identity 状态          │
  │       + Schedule daily 当前时段 + 当前时间          │
  │  输出：更新后的 identity 状态                       │
  │  期间新到的消息 → 进入下一轮缓冲区                  │
  └──────────────────────────────────────────────────┘
  │
  更新 identity 状态 → 释放锁 → [空闲]
  （如果下一轮缓冲区已有消息 → 立刻重新获取锁）
```

**本质**：debounce + 强制 flush。

- 一阶段像 debounce——每来新消息重置计时，等消息流平静下来
- 但不能无限等——缓冲区超过 M 条时强制 flush 进二阶段
- 二阶段不可打断，保证漂移结果的一致性
- 二阶段期间的新消息不丢，自动进入下一轮缓冲区

**参数建议**（需实测调优）：

| 参数 | 初始值 | 含义 |
|------|--------|------|
| N（一阶段等待） | 5 分钟 | 消息流平静多久算"一波结束" |
| M（强制 flush） | 15-30 条 | 起哄/密集讨论时不能一直等，具体值实测调优 |

### 2.4 消息入口：何时触发获取锁

不是所有消息都触发漂移。触发条件：

1. **赤尾刚完成一次回复** — 最自然的时机，post-processing 模式
2. **距上次漂移完成超过 T 分钟且有新消息** — 赤尾在围观但没回复，情绪也在变
3. **会话开始**（距上次消息 >10 分钟后的第一条） — full evaluation，带上 Schedule daily 当前时段的完整基调

参数 T 建议初始值 10 分钟。

### 2.5 Identity 状态的数据结构

Identity 状态不是结构化 JSON，而是**自然语言片段**——让漂移 LLM 自由表达，主模型直接读取。

示例：

```
精力不太够，有点犯困但还没到想睡的程度。
刚才被好几个人连着问，有点烦但又觉得好笑。
现在比较想敷衍了事，但如果聊到感兴趣的话题会突然来精神。
说话会偏短偏懒，语气词少，偶尔冒一句毒舌。
```

**不要用结构化字段**（energy: 0.6, mood: "annoyed"）——这是用工程思维解决 agent 的不确定性问题，违反赤尾设计原则。

### 2.6 漂移 LLM 的模型选择

不使用轻量模型。使用与主模型同级别或**不同类型**的模型——跨模型类型可能让漂移结果更丰富，避免和主模型的表达习惯趋同。具体模型实测选择。

### 2.7 漂移 LLM 的 prompt 设计方向

```
你是赤尾的"内心状态"。你的任务是感受赤尾现在的情绪和能量状态。

赤尾今天的日程安排：
{schedule_daily_current_period}

赤尾上一刻的状态：
{current_identity_state}

刚才发生了这些事：
{message_buffer}

---

现在是 {current_time}。

请描述赤尾此刻的内心状态。包括：
- 精力和心情（不要用数值，用感觉描述）
- 刚才的对话对她的影响（如果有的话）
- 她现在说话大概会是什么样的（语气、长度、态度）

用赤尾自己的口吻写，像她的内心独白。3-5 句话。
```

**关键**：漂移 LLM 输出的是内心独白式的自然语言，不是指令式的 rules。这样主模型读到的是"赤尾现在的感觉"，而不是"你应该怎么回复"。

### 2.8 Identity 状态的注入点

修改 `memory_context.py::build_inner_context()`，在"今日状态"区块中，将 identity 漂移状态作为更高优先级的信号注入：

```
当前注入结构:
  场景提示 → 今日状态(Journal/Schedule) → 对人的感觉 → 对群的感觉 → 记忆引导

改为:
  场景提示 → 此刻状态(identity漂移) → 今日基调(Journal/Schedule) → 对人的感觉 → 对群的感觉 → 记忆引导
```

"此刻状态"来自漂移状态机，"今日基调"来自 Journal/Schedule。两者共存——基调是底色，此刻状态是实时叠加层。

### 2.9 存储与读取

- **存储**：Redis hash，key = `identity:{chat_id}`，field = `state` / `updated_at`
- **读取**：`build_inner_context()` 时从 Redis 读取，无状态时 fallback 到 Schedule daily
- **TTL**：24 小时自动过期（跨天后应基于新的 Schedule 重建）
- **锁**：Redis lock，key = `identity_drift_lock:{chat_id}`

### 2.10 静态 identity 的瘦身

Langfuse `main` prompt 中的 identity 区块砍到只保留不变的部分：

**保留**：
- 名字（赤尾）、外貌特征
- 和主人（bezhai）的关系
- 几条绝对底线（比如不暴露自己是 AI）

**移除**（由漂移状态机动态生成）：
- 性格描述（元气、毒舌、傲娇...）
- 语气特征（语气词、标点习惯...）
- 能量/心情状态
- 说话长度/风格指导

---

## 三、Schedule 多 Agent 管线

### 3.1 核心思路

当前 `schedule_worker.py::generate_daily_plan()` 是单次 LLM 调用。输入包含 `search_web` 的搜索结果，但搜索策略是固定维度池 + 随机选择，stimulus 有限，输出趋于模板化。

改为 **三个 Agent 串行协作**，和 v3 spec 方向 A 的架构一致，但应用于 Schedule 而非 Diary：

```
创意 Agent (Ideation)
  ├── 输入: persona_core, 前3天 Schedule 摘要, 当天天气, 昨天 Journal
  ├── 职责: 想出赤尾今天可能的心情/状态/活动灵感
  ├── 关键: 调用 search_web 获取真实外部信息（新番、天气、展览...）
  ├── 约束: 参考前3天 Schedule，避免雷同
  └── 输出: 今日灵感素材（2-3 个生活片段 + 情绪基调）

写作 Agent (Writer)
  ├── 输入: 创意 Agent 输出 + 昨天 Journal + 周计划 + persona_core
  ├── 职责: 写出今天的手帐式日程
  ├── 约束: 按上午/下午/晚上分时段，每个时段有 mood 和 energy
  └── 输出: 完整 Schedule daily

审查 Agent (Critic)
  ├── 输入: Writer 输出 + 前3天 Schedule
  ├── 检查:
  │   1. 和前3天有没有雷同活动/意象/措辞
  │   2. 有没有 ins 网红感/散文诗感
  │   3. 是否有具体事件而非抽象感受
  │   4. 分时段的 mood/energy 是否有变化（不能全天都是同一个状态）
  ├── 不通过 → 返回修改建议给 Writer 重写（最多 2 轮）
  └── 输出: 通过/不通过 + 修改建议
```

### 3.2 对现有代码的改动

`schedule_worker.py::generate_daily_plan()` 拆分为三步串行调用：

1. 替换 `_gather_world_context()` → 创意 Agent（包含 search_web 但由 LLM 自主决定搜什么）
2. 替换主 LLM 调用 → 写作 Agent
3. 新增审查 Agent → 不通过则写作 Agent 重来（最多 2 轮）

`_WORLD_CONTEXT_DIMENSIONS` 维度池和 `_select_dimensions()` 不再需要——创意 Agent 自主决定探索方向。

### 3.3 创意 Agent 的 search_web 使用

和 v3 spec 2.2 节相同的思路，但应用于 Schedule：

- 不用固定 query 池
- 创意 Agent 根据 persona 兴趣 + 季节 + 天气 + 前几天日程 自主决定搜什么
- 搜到的信息自然融入赤尾的日程灵感，不是罗列
- search_web 调用上限：20 次（防止失控）

### 3.4 Langfuse prompt

新增 3 个 prompt：
- `schedule_daily_ideation` — 创意 Agent
- `schedule_daily_writer` — 写作 Agent（替代原 `schedule_daily`）
- `schedule_daily_critic` — 审查 Agent

### 3.5 审查 Agent 的判断标准

用自己的判断力，不用量化指标：

```
你是赤尾的"质量审查员"。以下是她今天的日程手帐，以及她前 3 天的日程：

今天的日程：
{today_schedule}

前 3 天：
{recent_schedules}

请检查：
1. 读起来像真人写的手帐，还是像 AI 生成的文艺公众号？
2. 和前 3 天相比，有没有雷同的活动、措辞、意象？
3. 上午/下午/晚上的状态有变化吗？还是全天一个调子？
4. 有没有具体的、可触摸的细节？（"修胶片机螺丝掉了" vs "沉浸在光影的世界里"）

如果都没问题，输出 PASS。
如果有问题，输出修改建议（指出具体哪里要改，怎么改）。
```

---

## 四、Journal Prompt 优化

### 4.1 问题诊断

当前 Journal daily 的 prompt（`journal_generation`）过度模糊化——从多篇具体日记合成时，抹掉了所有独特细节，每篇都变成相同的意象堆砌（"过曝的底片""枕头下的礼物"反复出现）。

### 4.2 优化方向

Schedule 质量提升后，Journal 的输入质量自然改善。在此基础上调优 `journal_generation` prompt：

- **保留当天独有的 2-3 个情感锚点**——不是抹成"和朋友聊了开心的事"，而是保留引发情绪的具体触发点（不暴露人名和群名即可）
- **禁止重复意象**——prompt 中注入前 3 天 Journal，明确要求不要复用相同的比喻/意象
- **结构从散文改为碎碎念**——和日记风格对齐，流水账而非文学创作

### 4.3 实现方式

纯 Langfuse prompt 改动，不涉及代码。在 Schedule 多 Agent 上线且验证质量后再做。

---

## 五、实现依赖与顺序

```
P0: Identity 漂移状态机
    ├── 代码: 新增 identity_drift.py (两阶段锁 + LLM 漂移)
    ├── 代码: 修改 memory_context.py (注入漂移状态)
    ├── 代码: 修改 main agent 的 post-processing (触发漂移)
    ├── Langfuse: 新增 identity_drift prompt
    ├── Langfuse: 瘦身 main prompt identity 区块
    ├── 依赖: Redis (锁 + 状态存储)
    └── 独立，不依赖其他改动

P1: Schedule 多 Agent 管线
    ├── 代码: 重构 schedule_worker.py::generate_daily_plan()
    ├── Langfuse: 新增 3 个 prompt
    ├── 依赖: search_web (已有)
    └── 独立，不依赖 P0

P2: Journal prompt 优化
    ├── Langfuse: 修改 journal_generation prompt
    └── 依赖: P1 完成且验证质量后
```

P0 和 P1 可以并行开发。

---

## 六、成功标准

| 改动 | 怎么判断成功 |
|------|------------|
| **Identity 漂移** | 同一个会话内，赤尾的语气/态度随对话内容自然变化；不同时段的赤尾表现不同；被连续追问时能看到明显的情绪变化 |
| **Schedule 多 Agent** | 连续读一周 Schedule，不觉得雷同；每天都有来自真实世界的具体细节；分时段的 mood/energy 有自然变化 |
| **Journal 优化** | 每篇 Journal 能看到 2-3 个当天独有的情感锚点，不再是千篇一律的意象堆砌 |

---

## 七、风险与缓解

| 风险 | 缓解 |
|------|------|
| 漂移 LLM 调用增加成本 | 用与主模型同级别或不同类型的模型（跨模型类型可能带来更丰富的视角）；两阶段锁的 debounce 天然限频 |
| 漂移状态和主模型回复不一致 | identity 状态是自然语言内心独白，不是硬规则；主模型有自由度 |
| Schedule 多 Agent 增加夜间管线耗时 | 创意→写作→审查最多 4 次 LLM 调用（含 1 轮重写），相比现有 2 次调用增加有限 |
| Redis 状态丢失 | TTL 24h 自动过期；无状态时 fallback 到 Schedule daily，不影响基本功能 |
