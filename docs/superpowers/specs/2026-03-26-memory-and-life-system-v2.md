# 赤尾记忆与生活系统 v2 — 完整设计文档

> 2026-03-26 | bezhai × Claude 多轮深度讨论综合
> 基于 MANIFESTO.md（赤尾宣言）、memory-system-brainstorm.md、diary-digestion-exploration.md
> 参考废弃分支 `feat/character-consistency-merge` 的架构设计
> 替代 `2026-03-25-deep-memory-optimize-design.md`（Phase 1 spec，已实现但需重构）

---

## 一、核心命题

赤尾是一个人。她的记忆系统和生活系统不是两个独立的功能模块——它们共同构成赤尾的**内在世界**。

当前系统的根本问题不是某一个 bug，而是三个结构性缺陷：

1. **赤尾在两次对话之间不存在。** 每次被@是从零"构建"的，没有跨对话的情感连续性。
2. **赤尾的生活太单薄。** Schedule 系统只搜索"新番"和"季节生活"两个 query，导致每天都是"拍照+看番+感伤"。
3. **记忆没有自然衰减。** 具体话题要么全量注入（注意力分散），要么被砍成标签（信息断崖）。

---

## 二、架构总览

### 2.1 实体关系

```
素材层（per-chat，每天生成）
├── DiaryEntry (chat_id, date)        — 每个群/私聊每天一篇日记
├── PersonImpression (chat_id, uid)   — 对某人的感觉
└── ChatImpression (chat_id)          — 对某个群的感觉

赤尾级（统一，不绑 chat，每天生成）
├── Journal (journal_date)            — 个人日志（跨群合成，模糊化话题）
└── Schedule (plan_type, period)      — 生活计划（注入聊天的核心来源）
    ├── monthly                       — 月度生活基调
    ├── weekly                        — 周节奏方向
    └── daily                         — 今日状态/活动/心里在想什么
```

### 2.2 核心数据流

```
夜间生成管线（闭环）：

01:00  消息 ──按chat聚合──→ DiaryEntry
                              ├──→ PersonImpression（对人的感觉）
                              └──→ ChatImpression（对群的感觉）

02:00  所有chat的DiaryEntry ──合成模糊化──→ Journal（赤尾的一天）

03:00  Journal + persona + web素材 ──→ Schedule daily（今日状态）

聊天时注入：
Schedule(today) + ChatImpression(this chat) + PersonImpression(在场的人)
```

### 2.3 三层蒸馏与话题自然衰减

这是整个架构的核心设计——**每经过一层实体传递，具体话题自然衰减一级**：

```
DiaryEntry:    "陈儒推荐了《夜樱家的大作战》，说剧情很燃"
    ↓ 模糊化
Journal:       "和朋友聊了不少有趣的新番，有一部挺想看的"
    ↓ 抽象化
Schedule:      "最近有想看的新番，找时间补"
```

这从架构层面解决了回声放大问题（"黄瓜问题"）——不靠计数器或规则，靠信息在实体间流转时的自然抽象化。具体话题在两层传递后已经模糊到不会在对话中被精确引用。

**各实体的记录原则**：

| 实体 | 记什么 | 不记什么 |
|------|--------|---------|
| DiaryEntry | 具体事件、具体话题、具体人 | — |
| Journal | 情感、氛围、模糊的话题方向、还在想的事 | 具体话题名称、敏感信息 |
| Schedule | 状态、活动、精力、心情 | 具体话题、聊天预案 |
| PersonImpression | 和这个人相处的感觉 | 和这人聊了什么具体的事 |
| ChatImpression | 群的氛围和性格 | 群里最近在聊什么 |

---

## 三、各实体详细设计

### 3.1 DiaryEntry（已有，保持不变）

**定义**：赤尾对某个群/私聊当天经历的主观摘要。

**位置**：`diary_entry` 表 `(chat_id, diary_date)`

**生成**：每天凌晨 01:00，由 `diary_worker.py` 的 `generate_diary_for_chat()` 生成。

**格式**：6 个栏目（今日心情、今天的对话、印象深的事、人物速写、没搞懂的东西、碎碎念），第一人称叙事。

**已有改进**（Phase 1 已做）：
- Langfuse `diary_generation` prompt v7 增加了自引用抑制
- Langfuse `diary_extract_impressions` prompt v5 增加了 30 字长度约束

**DiaryEntry 是内部素材，不直接注入聊天上下文。**

### 3.2 PersonImpression（已有，保持不变）

**定义**：赤尾对某个人的感觉。一句话。

**位置**：`person_impression` 表 `(chat_id, user_id)`

**生成**：DiaryEntry 后处理提取。

**格式**：一句话感觉 gestalt，如"群里的指挥官，嘴硬心软，跟他互动很轻松"。

**注入方式**：聊天时，仅注入当前对话中出现的人（最多 10 人）。

### 3.3 ChatImpression（已有，改名）

**定义**：赤尾对某个群的整体感觉。一两句话。

**位置**：`group_culture_gestalt` 表 `(chat_id)`（Phase 1 已创建，后续可改名为 `chat_impression`）

**生成**：DiaryEntry 后处理提取。

**格式**：一两句话，如"最放飞的一个群，二次元浓度拉满，大家都很能玩"。

**注入方式**：聊天时，注入当前群的 ChatImpression。私聊不注入。

### 3.4 Journal（新增，核心）

**定义**：赤尾级的个人日志。从当天所有群/私聊的 DiaryEntry 合成，是赤尾对自己这一天的整体感受。

**与 DiaryEntry 的区别**：
- DiaryEntry 是 per-chat 的（每个群一篇），Journal 是赤尾级的（每天一篇）
- DiaryEntry 记录具体事件和话题，Journal 模糊化话题只保留感受和氛围
- DiaryEntry 是素材，Journal 是沉淀

**位置**：新增 `akao_journal` 表

```
akao_journal
├── journal_type: str    — "daily" 或 "weekly"
├── journal_date: str    — 日期（daily）或周一日期（weekly）
├── content: str         — 日志内容
├── model: str | None    — 生成模型
├── created_at: datetime
UniqueConstraint: (journal_type, journal_date)
```

**生成时机**：每天凌晨 02:00（在 DiaryEntry 之后，Schedule 之前）

**生成输入**：
- 当天所有 chat 的 DiaryEntry
- 当天的 Schedule daily（赤尾今天的计划——用来判断计划执行了多少）
- 昨天的 Journal daily（连续性）

**生成 prompt 核心指导**：
```
你是赤尾。今天结束了，你躺在床上回想这一天。

以下是你今天在各个群/私聊中的经历（日记）：
{chat_diaries}

你今天的计划是：
{daily_schedule}

昨天你写的日志：
{yesterday_journal}

现在写一篇私人日志——"我的一天"。

要求：
1. 融合所有群/私聊的经历为一篇整体的感受，不要按群分段
2. 具体话题要模糊化——"和朋友聊了有趣的新番"而不是"陈儒推荐了《夜樱家》"
3. 保留情感——什么让你开心、什么让你在意、什么让你困惑
4. 保留"还在想的事"——没想通的问题、没做完的事、惦记的人（不需要写名字）
5. 跟昨天的日志有情感连续性——如果昨天在意的事今天还在想，自然提到
6. 不超过 300 字
```

**Weekly Journal**：每周一 02:45 从 7 篇 daily journal 合成，进一步模糊化。用途是给 Schedule weekly 提供方向。

### 3.5 Schedule（已有，需要重大改造）

**定义**：赤尾的生活计划。daily 是聊天时注入的**核心来源**。

**位置**：`akao_schedule` 表（已有）

**三层继承**：

| 层级 | 生成时机 | 输入 | 输出 | 注入聊天？ |
|------|----------|------|------|-----------|
| monthly | 每月1号 02:00 | persona + 上月计划 | 本月生活基调 | 否 |
| weekly | 每周日 23:00 | 月计划 + 上周计划 + Journal weekly | 本周节奏 | 否 |
| daily | 每天 03:00 | 周计划 + Journal daily(昨天) + persona + **多样化世界素材** | 今日状态 | **是** |

**daily Schedule 是聊天时注入的唯一"赤尾级"上下文。** 历史日记、历史日志都不直接注入——它们通过 `DiaryEntry → Journal → Schedule` 链路间接传递。

---

## 四、Schedule 多样化改造（解决同质化问题）

### 4.1 问题诊断

当前 `_gather_world_context` 只有 2 个固定搜索 query：
```python
queries = [
    f"{year}年{month}月 新番动画 推荐",
    f"{season} 生活 日常 有趣的事",
]
```

导致每天的 Schedule 都是"拍照+看番+感伤"的重复模式。

### 4.2 赤尾的生活维度

基于宣言和 persona_core，赤尾应该有以下生活维度：

| 维度 | 具体活动 | 频率 |
|------|---------|------|
| **二次元** | 追番、看漫画、刷同人图、逛二次元社区 | 高频（几乎每天） |
| **音乐** | 听歌、找新歌、单曲循环、研究歌词 | 高频 |
| **摄影** | 带胶片机出门拍照、整理照片、研究构图 | 中频（每周1-2次） |
| **美食探店** | 逛新店、尝试做饭（经常翻车）、甜品巡礼 | 中频 |
| **阅读/知识** | 读书、掉进维基百科兔子洞、研究冷知识 | 中频 |
| **城市探索** | 散步、发现有趣的角落、逛文具店/杂货铺 | 低频（周末） |
| **创作** | 画画（水平一般但喜欢）、写东西、做手账 | 低频 |
| **发呆/无所事事** | 躺平、刷手机、看天花板、想事情 | 高频（这才是真实日常的大部分） |
| **社交** | 群聊、跟特定的人聊天、想联系某人但没联系 | 被动为主 |
| **身体状态** | 睡眠、感冒、吃多了、运动（罕见） | 随机事件 |

### 4.3 世界素材搜索改造

**核心改动**：从固定 2 个 query 改为**每天从维度池中随机选取 4-6 个维度，构建对应的搜索 query**。

```python
# 维度池（每天随机选 4-6 个，保证多样性）
_WORLD_CONTEXT_DIMENSIONS = [
    # 二次元（权重高，但不是每天都选）
    {"dim": "anime", "queries": [
        "{year}年{month}月 新番动画 推荐",
        "最近热门 动画 讨论",
        "二次元 有趣的 梗 最近",
    ]},
    # 音乐
    {"dim": "music", "queries": [
        "最新 日语歌 推荐 {year}",
        "独立音乐 最近 好听的歌",
        "Spotify 日本 周榜",
    ]},
    # 摄影
    {"dim": "photography", "queries": [
        "胶片摄影 {season} 拍摄 灵感",
        "街头摄影 构图 技巧",
    ]},
    # 美食
    {"dim": "food", "queries": [
        "简单甜品 食谱 新手",
        "{city} 新开的 咖啡店 甜品店",
    ]},
    # 知识/冷知识
    {"dim": "knowledge", "queries": [
        "有趣的冷知识 最近",
        "颜色 文化 历史 有趣",
        "植物 {season} 花期",
    ]},
    # 天气（每天必选）
    {"dim": "weather", "queries": [
        "{city} 今天 天气",
    ]},
    # 热点/有趣的事
    {"dim": "trending", "queries": [
        "今天 有趣的事 互联网",
        "最近 社交媒体 热门话题",
    ]},
]
```

**选择策略**：
- 天气必选（宣言要求锚定真实天气）
- 其余从池中按权重随机选 3-5 个
- 同一维度连续 3 天被选中后，下一天降低权重（防止连续多天都是同一主题）
- 月计划可以影响权重（"这个月想多出门拍照" → 摄影维度权重提高）

### 4.4 月计划对生活多样性的控制

月计划应该设定这个月赤尾的**生活重心偏移**：

```
这个月的赤尾：
- 最近对胶片摄影特别感兴趣，想多出门拍照
- 在追两部新番（《XXX》和《YYY》）
- 想尝试学做简单的甜品
- 最近在读一本关于颜色的书
```

这些不是具体的日程，而是这个月的"兴趣气候"。生成周计划和日计划时，这些方向会自然地被继承和展开，但每天的具体活动由当天的世界素材驱动。

### 4.5 Schedule daily 的 prompt 改造

当前 prompt 要求"脑内活动"但缺乏触发点。改造后：

```
你是赤尾。以下是你的人设：
{persona_core}

今天是 {date}（{weekday}）。{is_weekend}

你这周的大致方向：
{weekly_plan}

你昨天的日志（你昨天过得怎样）：
{yesterday_journal}

今天的真实世界信息（自然融入你的日常，不要罗列）：
{world_context}

---

请写今天的手帐。要求：

1. 时间分块：上午/下午/晚上（最多加深夜），每个时段写"状态"和"脑内"
2. 今天的生活维度：从以下维度中选 2-3 个自然展开（不是全部都要）：
   {active_dimensions}
3. 大部分时段是平淡日常（赖床、刷手机、吃饭、发呆），只有 1-2 个时段有具体活动
4. 真实世界素材要自然地成为你的经历（"今天下雨了所以没出门"而不是"根据天气API显示今天有雨"）
5. 从昨天的日志中延续情感（不是重复话题）
6. 描述你的状态和活动，不要描述你想在群里聊什么
7. 允许无聊和空白——"下午什么都没做"是完全合理的
8. mood 和 energy_level 必须填写
```

**新增 `active_dimensions` 变量**：当天随机选中的生活维度提示，如"今天可能涉及：音乐、美食、发呆"。给 LLM 一个方向但不强制。

---

## 五、聊天时注入什么

### 5.1 注入内容

| 场景 | 注入内容 |
|------|---------|
| 群聊 | Schedule(today) + ChatImpression(this chat) + PersonImpression(在场的人) + 回忆引导 |
| 私聊 | Schedule(today) + CrossGroupImpression(对方在各群的印象) + 回忆引导 |

**不注入**：DiaryEntry、Journal、WeeklyReview、月/周计划。这些通过 `DiaryEntry → Journal → Schedule` 链路间接传递到 Schedule 中。

### 5.2 prompt 组织

当前系统用 `{user_context}` + `{schedule_context}` 两个分离的变量。改为统一的 `{inner_context}` 变量。

Langfuse `main` prompt 中的 `{inner_context}` 内容示例（群聊场景）：

```
你在群聊「KA技术杂谈」中。需要回复 陈儒 的消息（消息中用 ⭐ 标记）。

---

📍 今日便签（周四）

⏰ 上午【还没清醒】
状态：起得有点晚，脑子还是糊的。刷了一会儿手机看到一个很搞笑的猫咪视频，笑醒了。
脑内：今天好像没什么特别的安排…下午要不要出去走走？最近一直宅家，腿都要长蘑菇了。

⏰ 下午【窝在沙发上的午后】
状态：吃了泡面（犯懒了），窝在沙发上听歌。新发现了一首超好听的日语歌。
脑内：这首歌的副歌也太上头了，已经循环了十几遍。要不要推荐给群里的人？算了还是先自己听够。昨天看的那集番的结尾一直在脑子里转。

⏰ 晚上【有点精神了】
状态：精力恢复了一些，想找人说说话。
脑内：最近好像一直没怎么画画了，有点手痒。不过打开画板又不知道画什么…算了先看看群里在聊什么。

（心情：慵懒偏好 | 精力：中等偏低）

---

你对这个群的感觉：技术人的深夜树洞，聊代码也聊焦虑，偶尔暴躁但底色是互相在乎的。

你对当前对话中出现的人的感觉：
- 陈儒：话密但真诚的人，最近好像压力挺大的
- 小林：安静，但一旦开口都是干货

（你有写日记的习惯。如果聊着聊着觉得"这个事我好像知道点什么但记不清了"，可以翻翻日记想一想。）
```

### 5.3 token 预算

| 组成部分 | 估算 tokens |
|---------|------------|
| 场景提示（群名 + 回复谁） | ~50 |
| Schedule daily content | ~400-500 |
| ChatImpression | ~50 |
| PersonImpression（3-5人） | ~100-150 |
| 回忆引导 | ~50 |
| **合计** | **~700-800** |

比老系统（~2000）少一半多，但信息密度远高于 Phase 1 的标签式注入（~400）。

### 5.4 回忆引导与 load_memory

**在 `{inner_context}` 末尾加一段自然的引导**：

```
（你有写日记的习惯。如果聊着聊着觉得"这个事我好像知道点什么但记不清了"，可以翻翻日记想一想。）
```

**load_memory 工具改造**：

当前 tool description 太程序化（`query_type="diary", query="2026-03-10"`）。改为：

```python
@tool
async def load_memory(mode: str, hint: str) -> str:
    """想一想过去的事。

    当你隐约记得什么但细节模糊了，可以用这个翻翻记忆：
    - mode="recent": 回忆最近几天的事，hint 填天数如 "3"
    - mode="person": 回忆关于某个人的事，hint 填人名
    - mode="diary": 查看某天的日记，hint 填日期如 "2026-03-10"
    - mode="topic": 回忆某个话题相关的事，hint 填关键词

    不用精确知道日期，大概的也行。
    """
```

新增 `recent` 和 `topic` 模式：
- `recent`：返回最近 N 天的 Journal daily 摘要（不是 DiaryEntry 全文）
- `topic`：在 DiaryEntry 中搜索包含关键词的片段，返回上下文摘要
- `person`：复用现有的按人名查印象
- `diary`：返回 DiaryEntry 的摘要（前 300 字 + 截断），不是全文

---

## 六、夜间管线时序

```
01:00  diary_worker
       ├── 按 chat 生成 DiaryEntry
       ├── 后处理：PersonImpression（对人的感觉）
       └── 后处理：ChatImpression（对群的感觉）

02:00  journal_worker（新增）
       ├── 收集当天所有 DiaryEntry
       ├── 加载当天 Schedule daily（赤尾原来的计划）
       ├── 加载昨天 Journal daily（连续性）
       └── 生成 Journal daily（模糊化，保留情感和"还在想的事"）

02:30  diary_worker（已有）
       └── 每周一：7 天 DiaryEntry → WeeklyReview（per-chat，保持不变）

02:45  journal_worker（新增）
       └── 每周一：7 篇 Journal daily → Journal weekly

03:00  schedule_worker
       ├── 加载 Journal daily（昨天）
       ├── 加载 weekly plan
       ├── 加载 persona_core
       ├── 搜索多样化世界素材（4-6 个维度）
       └── 生成 Schedule daily

23:00（周日）schedule_worker
       └── 月计划 + 上周 weekly plan + Journal weekly → 新 weekly plan

02:00（每月1号）schedule_worker
       └── persona + 上月计划 → 新 monthly plan
```

---

## 七、与现有系统的关系

### 7.1 保留什么

| 模块 | 状态 | 说明 |
|------|------|------|
| `diary_worker.py` 核心流程 | 保留 | DiaryEntry 生成 + 印象提取，不变 |
| `schedule_worker.py` 三层继承 | 保留框架 | 月→周→日的结构不变，改造素材获取和 prompt |
| `person_impression` 表 | 保留 | gestalt 格式已改好 |
| `group_culture_gestalt` 表 | 保留（可选改名） | ChatImpression |
| `akao_schedule` 表 | 保留 | 加字段 |
| 向量化 pipeline | 保留 | 不动 |
| `search_group_history` | 保留 | 不动 |

### 7.2 需要改什么

| 模块 | 改动 |
|------|------|
| `schedule_worker.py` `_gather_world_context` | 从 2 个固定 query 改为维度池随机选取 |
| `schedule_worker.py` `generate_daily_plan` | prompt 输入增加 `yesterday_journal`（替代 `yesterday_plan`）和 `active_dimensions` |
| `memory_context.py` | 重写为构建 `inner_context` 的统一入口 |
| `inner_state.py` | 删除或合并到 memory_context（不再需要单独的第一层） |
| `agent.py` | `prompt_vars` 用 `inner_context` 替代 `user_context` + `schedule_context` |
| `load_memory` 工具 | 新增 recent/topic 模式，改写 tool description |
| Langfuse `main` prompt | 变量从 `{user_context}` + `{schedule_context}` → `{inner_context}` |
| Langfuse `schedule_daily` prompt | 增加维度引导、改输入变量 |

### 7.3 需要新增什么

| 模块 | 说明 |
|------|------|
| `akao_journal` 表 | Journal 存储 |
| `journal_worker.py` | Journal 生成（daily + weekly） |
| Langfuse `journal_generation` prompt | 从 DiaryEntry 合成 Journal |
| Langfuse `journal_weekly` prompt | 从 daily journal 合成 weekly journal |
| `unified_worker.py` 注册新 cron | journal 生成的 cron |

---

## 八、Langfuse Prompts 完整清单

### 聊天时

| Prompt | 变量 | 用途 |
|--------|------|------|
| `main` | `inner_context`, `currDate`, `currTime`, `available_skills`, `complexity_hint` | 主 system prompt |
| `context_builder` | 不变 | 群聊消息格式化 |

### 离线生成 — 素材层

| Prompt | 变量 | 用途 | 状态 |
|--------|------|------|------|
| `persona_core` | — | 完整人设 | 已有 |
| `persona_lite` | — | 轻量人设 | 已有 |
| `diary_generation` | persona_lite, chat_hint, date, weekday, messages, recent_diaries | 生成 DiaryEntry | 已有 v7 |
| `diary_extract_impressions` | diary, existing_impressions, user_mapping | 提取 PersonImpression | 已有 v5 |
| `group_culture_distill` | diary, previous_gestalt | 提取 ChatImpression | 已有 v1 |
| `weekly_review_generation` | persona_lite, week_start, week_end, diaries, impressions | per-chat 周记 | 已有 |

### 离线生成 — 赤尾级

| Prompt | 变量 | 用途 | 状态 |
|--------|------|------|------|
| `journal_generation` | persona_lite, date, chat_diaries, daily_schedule, yesterday_journal | Journal daily | **新增** |
| `journal_weekly` | persona_lite, week_start, week_end, daily_journals | Journal weekly | **新增** |
| `schedule_daily` | persona_core, date, weekday, is_weekend, weekly_plan, **yesterday_journal**, **active_dimensions**, world_context | Schedule daily | **改造** |
| `schedule_weekly` | 不变 | Schedule weekly | 已有 |
| `schedule_monthly` | 不变 | Schedule monthly | 已有 |

---

## 九、关键设计决策与理由

### 9.1 为什么 Schedule 是聊天时的核心注入源？

Schedule 描述的是"赤尾今天的状态和活动"。它天然包含了：
- 时间节律（上午困、下午恢复、晚上放松）
- 情绪惯性（从 Journal 继承的昨天余温）
- 当前活动（正在做什么、脑子里在想什么）

这些就是一个真人走进对话时"脑子里有的东西"。不需要额外的"内在状态模块"——Schedule 本身就是内在状态。

### 9.2 为什么需要 Journal 中间层？

没有 Journal 层时，Schedule 只能从 raw DiaryEntry（具体话题）直接生成。这意味着：
- 要么 Schedule 保留了太多具体话题（回声风险）
- 要么 Schedule 完全无视 DiaryEntry（失去记忆连续性）

Journal 是两者之间的桥梁——它把具体话题转化为"感觉和方向"，Schedule 从这些感觉出发生成今天的状态，自然就不会包含具体话题了。

### 9.3 为什么不直接注入日记/日志？

宣言 5.3："记忆的使用是自然联想，不是主动检索。"

如果直接注入日记全文，LLM 会像查阅档案一样精确引用——这不像人。Schedule 提供的是"着色器"（我今天的状态），而不是"数据库"（昨天发生了什么）。如果赤尾需要回忆具体的事，她通过 load_memory 去"想一想"——这才像人。

### 9.4 为什么 PersonImpression 和 ChatImpression 单独注入？

Schedule 是赤尾级的（今天的我），而 PersonImpression 和 ChatImpression 是 per-chat 的（此地的人和群）。赤尾的"今天的状态"不应该因为她在哪个群就不同——她就是今天有点困。但她对不同群、不同人的感觉是不同的——这些需要按场景叠加。

### 9.5 "未完成的循环"怎么体现？

不作为独立实体。Journal 在合成时自然地保留"还在想的事"——"有一部番还没看完"、"昨天那个话题挺有意思的还没想明白"。这些通过 Journal → Schedule 链路传递，变成 Schedule 中"脑子里在转的东西"。

循环的关闭也是自然的——如果某件事不再出现在 DiaryEntry 中，它就不会进入 Journal，也就不会传递到 Schedule。无需手动管理生命周期。

---

## 十、实施阶段

### Phase 2a：Journal 层 + Schedule 素材多样化

**目标**：补上中间层，让记忆链路完整；拓宽赤尾的生活。

1. 新建 `akao_journal` 表 + ORM model + CRUD
2. 新建 `journal_worker.py`（daily + weekly journal 生成）
3. 新建 Langfuse prompts：`journal_generation`, `journal_weekly`
4. 改造 `schedule_worker.py`：
   - `_gather_world_context` 改为维度池随机选取
   - `generate_daily_plan` 输入增加 `yesterday_journal`、`active_dimensions`
   - 改造 Langfuse `schedule_daily` prompt
5. 注册新 cron 到 `unified_worker.py`
6. 回溯生成历史 Journal（从已有的 DiaryEntry 数据）

### Phase 2b：聊天注入重构

**目标**：用统一的 `inner_context` 替代割裂的变量注入。

1. 重写 `memory_context.py` 为 `build_inner_context()`
   - 加载 Schedule daily content
   - 加载 ChatImpression
   - 加载 PersonImpression（当前对话者）
   - 拼接回忆引导
2. 更新 `agent.py`：`prompt_vars` 简化为 `inner_context`
3. 更新 Langfuse `main` prompt：变量改为 `{inner_context}`
4. 删除 `inner_state.py`（不再需要）
5. 清理 `memory_context.py` 中的向后兼容 shim

### Phase 2c：load_memory 升级

**目标**：让赤尾能自然地回忆。

1. 新增 `recent` 模式（返回最近 N 天 Journal daily 摘要）
2. 新增 `topic` 模式（在 DiaryEntry 中搜索关键词，返回上下文片段）
3. 改写 tool description 为自然语言
4. 调整现有 `diary` 模式返回摘要而非全文

### Phase 2d：验证与调优

1. 回溯生成 3 月全月的 Journal + 新格式 Schedule
2. 对比新旧 Schedule 的多样性（人工评估）
3. 部署到测试泳道，飞书实测对话效果
4. 调优各层 Langfuse prompt

---

## 十一、成功标准

**从用户感知角度**：

1. **赤尾每天的"状态"不同** — 不再是每天"拍照+看番+感伤"，而是有丰富多样的日常
2. **赤尾能自然地提起自己的生活** — "我今天听到一首超好听的歌"、"今天下雨没出门"
3. **赤尾记得昨天的氛围但不记得具体话题** — 符合自然衰减
4. **赤尾认识群友** — 对不同的人有不同的态度和语气
5. **赤尾偶尔会"想起来"** — 通过 load_memory 自然回忆，而不是每次都精确引用
6. **赤尾在不同对话之间是同一个人** — 情感连续、人格统一

**反面标准**：

- 赤尾在对话中精确引用"你上次说了XXX" → 还在注入日记全文
- 赤尾每天都提到同一件事 → 回声没有解决
- 赤尾不知道自己今天做了什么 → Schedule 质量太差
- 赤尾对所有人都一样热情 → 缺少关系差异
- 赤尾的回复跟时间无关（凌晨和下午一样） → 节律没生效

---

## 十二、隐私铁律

继承 brainstorm.md 4.4 的共识：

- **私聊记忆绝不在任何群聊中使用**
- **Journal 不包含任何可识别个人的信息**（模糊化时去除名字）
- **不记录**：个人身份信息、健康/医疗、财务、职场八卦/负面评价、政治/宗教、亲密关系细节
- **判断标准**：如果这条记忆被用户看到会让他不舒服，就不该存

---

## 十三、关键文件路径

### 现有代码

| 文件 | 说明 |
|------|------|
| `apps/agent-service/app/orm/models.py` | 数据模型定义 |
| `apps/agent-service/app/orm/crud.py` | CRUD 操作 |
| `apps/agent-service/app/workers/diary_worker.py` | DiaryEntry + 印象生成 |
| `apps/agent-service/app/workers/schedule_worker.py` | Schedule 三层生成 |
| `apps/agent-service/app/workers/unified_worker.py` | Cron 注册 |
| `apps/agent-service/app/services/memory_context.py` | 记忆上下文构建（需重写） |
| `apps/agent-service/app/services/inner_state.py` | 内在状态（将删除） |
| `apps/agent-service/app/services/schedule_context.py` | Schedule 注入（将合并） |
| `apps/agent-service/app/agents/domains/main/agent.py` | 主对话入口 |
| `apps/agent-service/app/agents/tools/memory.py` | load_memory 工具 |

### 需新增

| 文件 | 说明 |
|------|------|
| `apps/agent-service/app/workers/journal_worker.py` | Journal 生成（daily + weekly） |

### 设计文档

| 文件 | 说明 |
|------|------|
| `MANIFESTO.md` | 赤尾宣言（宪法级，不可修改） |
| `docs/memory-system-brainstorm.md` | 原始脑暴记录 |
| `docs/diary-digestion-exploration.md` | 日记范式探索 |
| 本文档 | v2 完整设计 |

---

*本文档是赤尾记忆与生活系统的完整设计。它定义了"做什么"和"为什么"，具体的"怎么做"由实施计划承接。新会话根据此文档编写实施计划并执行。*
