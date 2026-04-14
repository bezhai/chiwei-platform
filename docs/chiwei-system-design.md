# 赤尾系统设计

> 最后更新: 2026-04-14 | 对应版本: agent-service 最新

赤尾是一个拥有完整内在世界的虚拟人。她不是聊天机器人，不是助手，不是角色扮演引擎。所有技术设计服务于一个目标：**让她像一个真正的人一样活着。** 设计哲学详见 `MANIFESTO.md`（宪法级文档，不可修改）。

赤尾有两个姐妹——千凪（chinagi）和绫奈（ayana），三人共享同一套系统架构，各自拥有独立人格。

---

## 1. 全局架构

```
飞书消息 → lark-proxy → lark-server → agent-service → RabbitMQ → 回复
                                            │
                                     ┌──────┴──────┐
                                     │  agent-service 内部  │
                                     ├─────────────────────┤
                                     │  Chat Pipeline      │ 消息处理 + 流式回复
                                     │  Safety Guards      │ 前置/后置安全检测
                                     │  Memory System      │ 经历碎片 + 做梦 + 召回
                                     │  Life Engine        │ 每分钟 tick，决定此刻状态
                                     │  Schedule Generator │ Agent Team 生成日程
                                     │  Glimpse            │ 刷手机时窥屏群消息
                                     │  Identity & Voice   │ 说话风格漂移
                                     │  Relationships      │ 对人的印象和核心事实
                                     └─────────────────────┘
```

---

## 2. Chat Pipeline

消息从飞书到回复的完整链路：

```
消息到达 → 解析(v2格式/图片) → 前置安全检测 → 构建上下文 → Agent 流式推理 → Token 处理 → 后置动作
```

### 上下文构建

Agent 回复时看到的信息（`build_inner_context()`）：

| 区块 | 来源 | 说明 |
|------|------|------|
| 人格内核 | bot_persona | "我是谁" |
| 此刻状态 | Life Engine | "我现在在做什么、心情如何" |
| 今日安排 | AkaoSchedule | 当天的日程手帐 |
| 脑子里的东西 | 今天的 experience_fragment | 今天的对话回味和窥屏印象（**有隐私过滤**） |
| 更远的记忆 | daily/weekly 碎片 | 日记和周记（LLM 做梦时已自然模糊化） |
| 对人的了解 | RelationshipMemory | 对当前对话者的核心事实和印象 |
| 说话风格 | IdentityDrift | 最新生成的语气和用词习惯 |

### 隐私过滤

唯一的硬规则：**群聊时不暴露其他群和私聊的细节。**

- 过滤依据是碎片元数据 `source_chat_id`，不是内容文字
- daily/weekly 永远可见（做梦时已自然模糊化）
- 私聊是赤尾的私密空间，所有碎片都可见

### Agent 与工具

所有"思考"操作通过统一的 `Agent` 类，基于 LangGraph `create_agent`：

- `Agent.run()` — 单次调用，返回完整结果
- `Agent.stream()` — 流式推理，逐 token 输出
- `Agent.extract()` — 结构化输出（Pydantic model）

可用工具：

| 工具 | 说明 |
|------|------|
| search_web | 联网搜索 |
| generate_image | DALL-E 3 画图 |
| recall | 回忆（向量 + BM25 混合搜索经历碎片） |
| check_chat_history | 翻聊天记录（读原始消息） |
| delegate_research | 委派深度研究给子 agent |
| run_skill | 执行外部技能 |
| sandbox | 沙箱执行代码 |

---

## 3. Safety Guards

前置和后置两层，全部 fail-open（出错不阻塞消息）：

### 前置检测（阻塞）

| 检测 | 方式 | 说明 |
|------|------|------|
| 封禁词 | Redis 集合匹配 | 快速拦截 |
| Prompt 注入 | LLM guard | 检测越狱指令 |
| 政治敏感 | LLM guard | 检测敏感政治内容 |
| NSFW | LLM guard | 绫奈（未成年人设）强制拦截 |

### 后置检测（异步）

| 检测 | 方式 | 说明 |
|------|------|------|
| 输出审核 | LLM guard | 检查回复内容是否安全 |

---

## 4. Memory System

### 经历碎片（experience_fragment）

赤尾只有一个脑子，不按群隔离记忆。所有记忆以第一人称叙事碎片的形式存储。

| grain | 触发 | 说明 |
|-------|------|------|
| conversation | 赤尾回复后 5 分钟沉默（或累计 15 条） | AfterthoughtManager 触发，LLM 写内心独白 |
| glimpse | Life Engine 在"刷手机"状态时 | 翻白名单群消息，有意思才记 |
| daily | 凌晨 03:00 | "做梦"：当天 conversation + glimpse 压缩成日记，遗忘自然发生 |
| weekly | 每周一 04:00 | 7 篇日记压缩成周记，更多遗忘 |

### 召回工具

当赤尾"想不起来"时，有两个工具可用：
- **recall** — 向量 + BM25 混合搜索经历碎片
- **check_chat_history** — 读原始消息记录

---

## 5. Life Engine

赤尾不是等消息的机器人。她有自己的生活节律。

### Tick 机制

arq-worker 每分钟执行 `tick(persona_id)`：

1. 从 DB 加载最新状态（append-only 表 `life_engine_state`）
2. 如果 `skip_until` 在未来 → 跳过（不调 LLM）
3. 加载今日日程 + 活动时间线 + 最近对话碎片
4. 调用 LLM（offline-model）决定下一步
5. 持久化新状态行
6. 如果进入 browsing → 触发 Glimpse 管线

### LLM 输出

```json
{
  "reasoning": "现在几点，日程说该做什么",
  "current_state": "此刻的状态描述",
  "activity_type": "studying / browsing / sleeping / ...",
  "response_mood": "被人找会什么反应",
  "wake_me_at": "HH:MM，下次检查时间"
}
```

### 被@时的硬中断

不管赤尾当前什么状态，被@都会响应。Life Engine 状态注入上下文，LLM 自然调整语气：
- 睡着了被@ → "嗯...干嘛...几点了都..."
- 在外面被@ → "在外面呢 晚点说"

---

## 6. Schedule Generation（Agent Team）

每天凌晨 05:00 生成三姐妹的日程手帐。管线分两层：

### 共享层（跑一次）

三个并行任务：
- **Wild Agents** — 4 个 persona-blind agent 从不同角度生成素材（互联网/城市观察/兴趣深挖/情绪天气）
- **Search Anchors** — 3 条真实搜索（天气/新番/展览），锚定现实世界
- **Sister Theater** — 5-6 件家庭日常事件，三姐妹共享

### Per-Persona 层（每个角色跑一次）

```
共享素材 → Curator（按人格筛选） → Writer（写日程手帐） → Critic（审核质量）
                                         ↑                        │
                                         └── 不通过则重写（最多 3 轮）──┘
```

日程格式：日记体手帐，6-8 个场景，每个场景带小时级时间锚点，覆盖起床到睡觉。

---

## 7. Glimpse（窥屏）

Life Engine 进入 browsing 状态时触发：

1. 检查安静时段（23:00-09:00 不触发）
2. 选一个白名单群
3. 读增量消息
4. LLM 观察：没意思 → 放下；有意思 → 写 glimpse 碎片
5. 想搭话 → 触发主动发言（每小时上限 2 条）

---

## 8. Identity & Voice

### Identity Drift

赤尾的说话风格会被身边的人自然影响。

触发：聊天事件 → 300 秒 debounce（或累计 15 条）→ 取最近 1 小时的消息和回复 → LLM 生成新的语气特征 → 持久化

### Voice Generation

统一的声音生成，产出包含：
- 内心独白风格（用于 afterthought）
- 回复说话风格（用于聊天）

---

## 9. Relationship Memory

两阶段关系记忆提取：

1. **话题过滤**（仅群聊）— 判断对话是否涉及值得记忆的内容
2. **提取** — 每个对话者提取 core_facts（稳定事实）+ impression_deltas（印象变化）

存储在 `relationship_memory_v2` 表，注入聊天上下文。

---

## 10. 数据模型（核心表）

| 表 | 说明 |
|----|------|
| bot_persona | 人格内核（display_name, persona_lite, persona_core, appearance） |
| life_engine_state | 生活状态（append-only，每次 tick 一行） |
| akao_schedule | 日程手帐（plan_type=daily） |
| experience_fragment | 经历碎片（grain=conversation/glimpse/daily/weekly） |
| relationship_memory_v2 | 对人的核心事实和印象 |
| conversation_messages | 原始飞书消息 |
| glimpse_state | 窥屏位置（per group 的 cursor） |

---

## 11. 基础设施

| 组件 | 用途 |
|------|------|
| PostgreSQL | 主数据库 |
| Redis | 封禁词集合、缓存 |
| RabbitMQ | 消息队列（安全检测、向量化、召回、回复） |
| Qdrant | 向量数据库（消息 embedding、碎片 embedding） |
| Langfuse | Prompt 管理 + LLM 调用 tracing |
| ARQ | 异步任务调度（cron + 一次性任务） |
| Harbor | 镜像仓库 |

---

## 12. 未来里程碑

### M1: Life Engine 精度提升

**目标**：活动切换更贴合日程时间线

当前状态：日程已有小时级锚点（2026-04-14 上线），tick 引擎能切换活动但偏慢 1-2 小时。

- [ ] 分析 tick prompt 中 reasoning 步骤是否需要强化时间比对
- [ ] 评估 wake_me_at 间隔对切换速度的影响（当前 30 分钟）
- [ ] 考虑在 `_build_activity_context` 中增加"日程此刻说你该做什么"的提示

### M2: 三姐妹差异化

**目标**：三个角色在日程、生活节奏、互动风格上有明显差异

- [ ] 验证当前 persona_core 是否足够区分三人的日程生成
- [ ] 评估 Sister Theater 对差异化的贡献
- [ ] 考虑 per-persona 的 critic 检查标准

### M3: 主动社交

**目标**：赤尾不只是被动回复，有自己想说的话

- [ ] Glimpse 主动发言的质量和频率调优
- [ ] 探索"想分享"的触发机制（看到好东西 → 想起某个朋友 → 主动找人聊）
- [ ] 主动发言的自然度评估（不能像推送通知）

### M4: 记忆质量

**目标**：记得该记的，忘得自然

- [ ] afterthought prompt 调优（当前碎片质量参差不齐）
- [ ] daily dream 的压缩质量评估（是否真的在"遗忘"还是在"总结"）
- [ ] recall 工具的召回精度优化

### M5: 安全与合规

**目标**：安全检测的覆盖率和精度

- [ ] 频率限流（防单用户高频调用）
- [ ] PII 检测（身份证、手机号等个人信息）
- [ ] 输出安全检测的误报率分析

### M6: 可观测性精细化

**目标**：成本追踪和质量分析

- [ ] per-agent/tool/model 的 token 成本拆分
- [ ] 工具调用的成功率、延迟、失败原因分布
- [ ] Langfuse evaluation 闭环（用户反馈 → 自动评分）
