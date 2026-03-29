# Schedule 多 Agent 管线

> 2026-03-29 | bezhai × Claude 基于 Phase 2 spec §3 的详细设计
> 前置文档：`2026-03-28-context-v3-phase2-design.md` §三

---

## 一、背景与问题

当前 `schedule_worker.py::generate_daily_plan()` 是单次 LLM 调用。搜索策略依赖固定维度池 `_WORLD_CONTEXT_DIMENSIONS`（8 个维度）+ 确定性随机选择（`_select_dimensions()`），导致：

- **主题严重趋同**：最近 3 天的 daily schedule 几乎是同一天的变体（赖床+胶片机+抹茶+虐心番+某人的消息等待）
- **素材缺乏真实感**：固定 query 模板搜到的结果千篇一律
- **无质量把关**：生成即采用，没有审查环节

## 二、管线架构

```
generate_daily_plan()
  │
  ├── 1. 收集上下文
  │     persona_core, weekly_plan, yesterday_journal, 前3天 schedule
  │
  ├── 2. Ideation Agent (SubAgent + search_web 工具)
  │     LLM 自主决定搜什么，tool-use loop
  │     输出: 2-3 个生活片段灵感 + 真实素材
  │
  ├── 3. Writer Agent (SubAgent, 无工具)
  │     输入: Ideation 输出 + weekly_plan + persona_core
  │     输出: 手帐式日程叙事
  │
  └── 4. Critic Agent (SubAgent, 无工具)
        输入: Writer 输出 + 前3天 schedule
        输出: PASS 或修改建议
        不通过 → Writer 重写（最多 2 轮）
```

### 2.1 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| Ideation 搜索方式 | tool-use agentic loop | LLM 自主决定搜什么，可根据搜索结果迭代，比固定维度池灵活 |
| Agent 框架 | LangGraph (SubAgent) | 复用已有 SubAgent + AgentRegistry 基础设施，后续方便统一切自研框架 |
| search_web 调用限制 | `recursion_limit` | LangGraph 原生机制，不需要手写计数器 |
| 模型 | offline-model (gpt-5.4) | 异步任务标准选择，跨模型类型避免与主模型表达趋同 |
| 存储 | 不动 DB schema | Schedule 是底色，不需要结构化拆分 |

## 三、Ideation Agent

**AgentRegistry**：`schedule-ideation`，prompt = `schedule_daily_ideation`，model = `offline-model`，tools = `[search_web]`

**Prompt 方向**：

```
你是赤尾的"灵感收集员"。你的任务是为赤尾今天的日程手帐搜集真实的生活素材。

赤尾是谁：
{persona_core}

昨天她经历了什么：
{yesterday_journal}

她前 3 天的日程（避免雷同）：
{recent_schedules}

今天是 {date}（{weekday}），{season}。

---

用 search_web 工具主动搜索你觉得今天赤尾可能会接触到的东西。
比如：最近有什么新番上线？她住的城市今天天气怎么样？有没有什么展览/活动？
她喜欢的领域有什么新鲜事？

搜什么完全由你决定，但要注意：
- 搜到的东西要能自然融入一个 19 岁女生的日常，不要硬塞
- 看看前 3 天用过什么素材，别重复
- 不需要面面俱到，2-3 个有质感的素材就够了

最后输出你搜集到的素材和灵感，给写作者用。
```

**search_web 限制**：通过 `recursion_limit=42`（约 20 次 tool call）控制，LangGraph 原生机制。

**输出**：自然语言，不要求 JSON。直接作为 Writer 的输入上下文。

## 四、Writer Agent

**AgentRegistry**：`schedule-writer`，prompt = `schedule_daily_writer`，model = `offline-model`，无工具

**输入**：
- Ideation 输出（素材+灵感）
- weekly_plan（本周方向）
- yesterday_journal（昨天感受）
- persona_core
- date / weekday / is_weekend

**输出**：完整手帐式日程叙事。职责与现有 `schedule_daily` prompt 相同，但输入质量更高。

**重写时**：额外接收 Critic 的修改建议，针对性修改。

## 五、Critic Agent

**AgentRegistry**：`schedule-critic`，prompt = `schedule_daily_critic`，model = `offline-model`，无工具

**输入**：Writer 输出 + 前 3 天 schedule

**Prompt 方向**：

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

## 六、编排逻辑

在 `generate_daily_plan()` 中：

```python
# 1. 收集上下文（不变）
persona_core = ...
weekly_plan = ...
yesterday_journal = ...
recent_schedules = get_recent_schedules(3)  # 前 3 天

# 2. Ideation
ideation_output = await ideation_agent.run(
    ideation_prompt.format(...),
    config={"recursion_limit": 42}
)

# 3. Writer → Critic 循环
feedback = None
schedule_text = None
for attempt in range(3):
    writer_input = build_writer_input(ideation_output, feedback)
    schedule_text = await writer_agent.run(writer_input)

    critic_result = await critic_agent.run(
        critic_prompt.format(today_schedule=schedule_text, recent_schedules=recent_schedules)
    )

    if "PASS" in critic_result:
        break
    feedback = critic_result

# 4. 存储（不变）
upsert_schedule(schedule_text)
```

## 七、代码改动

### 修改

| 文件 | 改动 |
|------|------|
| `app/workers/schedule_worker.py` | 重构 `generate_daily_plan()`：删除 `_gather_world_context()`、`_select_dimensions()`、`_WORLD_CONTEXT_DIMENSIONS`（~115 行），替换为三 Agent 管线编排 |
| `app/agents/core/config.py` | 注册 3 个新 AgentConfig |

### 新增

| 内容 | 说明 |
|------|------|
| Langfuse `schedule_daily_ideation` | 创意 Agent prompt |
| Langfuse `schedule_daily_writer` | 写作 Agent prompt（替代 `schedule_daily`） |
| Langfuse `schedule_daily_critic` | 审查 Agent prompt |

### 删除

| 内容 | 行数 |
|------|------|
| `_WORLD_CONTEXT_DIMENSIONS` | ~65 行 |
| `_select_dimensions()` | ~15 行 |
| `_gather_world_context()` | ~35 行 |

### 不动

- DB schema / models.py
- search_web 工具本身
- memory_context.py（读取方式不变）
- 月计划/周计划生成
- unified_worker.py（cron 入口不变）

## 八、容错

| 场景 | 处理 |
|------|------|
| Ideation 失败（模型超时/搜索全挂） | 降级为无素材，Writer 纯靠 persona + weekly plan 写 |
| Writer 3 轮都没 PASS | 用最后一版，不阻塞管线 |
| 整个管线异常 | log error，当天无 daily schedule，identity drift fallback 到 weekly plan |

## 九、成功标准

连续读一周 Schedule：
- 不觉得雷同
- 每天都有来自真实世界的具体细节
- 分时段的情绪有自然变化
- 没有 ins 网红感/散文诗感
