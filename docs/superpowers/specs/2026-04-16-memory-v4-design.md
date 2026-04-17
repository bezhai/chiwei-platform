# Memory System v4 设计大纲

> 状态：**产品形态已定（10 个开放项全部讨论完成），技术方案待 Plan 阶段展开**
> 分支：`feat/context-decline`

---

## 一、当前系统的问题

### 1. Context 注入信息重复

同一事件被多条管道加工后**同时注入 prompt**：fragment / relationship memory / life state / voice 四者信息高度重叠，占用 ~1100 tokens 传达 ~200 tokens 的增量信息。

### 2. 分层不统一

"层"的概念存在（聊天消息 / life state / glimpse / conversation / daily / weekly / relationship memory），但每层各自独立产出，没有"一条记忆在不同层只是状态不同"的统一模型。

### 3. 长期记忆遗忘过度

按时间机械压缩（conversation → daily → weekly，压缩率 84:1），不管事情是否重要，也不区分是否已被关系记忆消化。

### 4. Recall 形同虚设

PG full-text search + `simple` 字典 = 纯关键字匹配；`relationship_memory_v2` 不在搜索范围；模型几乎不触发这个工具。

### 5. Cross-chat 硬编码 + 有洞

硬编码单个群 ID；proactive 场景没有 trigger_user 时完全失效；24h 内消息无相关性过滤，噪音大。

### 6. Life Engine 和对话流不相干

Life Engine 的 state 由 schedule 决定，schedule 凌晨生成后基本静态。对话里产生的承诺、期待、即时计划无法影响 state。典型例子：绫奈回家路上群友说送冰淇淋，她回家后的 state 却还是按 schedule 做作业，完全没有"等冰淇淋"的迹象。

### 7. 数据现状（2026-04-16 快照）

| 数据源 | 记录数 | 平均长度 | 质量 |
|--------|--------|----------|------|
| conversation fragment | 373 | 564 字 | 内容好但高度重复（同一事件 10+ 变体） |
| glimpse fragment | 289 | 113 字 | 孤儿，信息密度低 |
| daily fragment | 25 | 1106 字 | 只覆盖 ~60% 天数 |
| weekly fragment | 3 | 827 字 | 只跑过一次 |
| relationship_memory_v2 | 617 | facts 89 字 / impression 134 字 | **质量最高**，具体行为+情感层 |
| life_engine_state | 1120 | ~100 字 | 同描述复用 35 次 |

---

## 二、产品形态

### 设计原则

**赤尾是一个人，记忆应该像人的记忆**。不用工程思维解决不确定性问题——该由 LLM 判断的交给 LLM，外部只提供辅助参考、不立硬规则。

### 1. 记忆的触发形态（三种组合）

- **场景自动注入**：trigger_user 到场 → 关于 Ta 的抽象记忆；话题被提到 → 相关偏好/群氛围。不依赖工具调用。
- **被动召回**：她觉得需要更多信息 → 调用 recall 工具按需加载更多
- **主动回忆**：对话中自发想起（"诶我想起来上次..."）

### 2. 记忆的形态

**不预设分类**。记忆有三种形态，按用途而不是按主题分：

| 形态 | 描述 | 例子 |
|------|------|------|
| **事实碎片** | 客观发生了什么。afterthought / glimpse 自动产 | "浩南今天说他换工作了" |
| **抽象记忆** | 从事实沉淀的认识，自由主语 + 内容 | subject="浩南"、content="他最近压力大" |
| **笔记（notes）** | 赤尾**主动**决定"这事我要记住"的清单 | "周五和浩南看电影" / "想一下要不要学 Rust" |

**抽象记忆的 subject 是自由字符串**，不枚举类型：
- `"浩南"`（某人）
- `"self"`（自我）
- `"和浩南的关系"`（关系）
- `"学习 / Rust"`（话题）
- `"ka 群"`（群）
- `"最近一段"`（时间段）

**关键差别**：
- **事实碎片**是系统自动从对话里产出的
- **抽象记忆**是对话中 tool call + reviewer 沉淀产出的
- **笔记**是赤尾**自己主动**打开清单写的（不是系统判定"这是承诺就自动记"，是她说"这事重要我要记住"）

### 2.1 关于"承诺"

不存在"承诺"这种独立对象。如果赤尾真的很在意一件事（"周五看电影"），她会**像人一样打开清单写下来** —— 走笔记，不走"F 类抽象"。

不那么在意的承诺就是一条普通事实，赶上时间近了自然会被想起（recall 或 fragment 注入），时间过了也会自然淡化 / 被改写成过去式。

全程没有 `status: active/fulfilled/expired` 状态机，履约和失约都是 reviewer 读当下改旧记忆的自然结果。

### 3. 两层记忆架构

```
┌────────────────────────────────────────────────────┐
│  短期层（fresh，还没挂进 graph 的）               │
│                                                     │
│  - 聊天消息（即时上下文）                           │
│  - Life State（当下活动/心情）                      │
│  - 新鲜事实碎片（最近几小时、reviewer 还没处理）    │
└────────────────────────────────────────────────────┘
                         │  reviewer 处理
                         ↓
┌────────────────────────────────────────────────────┐
│  长期层（已挂进 graph 的）                         │
│                                                     │
│     抽象记忆（7 类统一存储）                        │
│        ↕  支撑边                                    │
│     事实碎片（清晰 / 模糊 / 遗忘）                  │
│                                                     │
│  注入：默认只加载场景触发的相关抽象（带支撑事实）   │
│  Recall：按需取其他抽象 + 对应事实                  │
└────────────────────────────────────────────────────┘
```

**核心性质**：
- 抽象记忆"骑"在事实之上，通过 graph 边连接
- 事实清除后，抽象仍在（如有其他事实支撑 或 已充分强化）
- 孤儿事实淡化更快——没被认知线索抓住的琐事本来就该忘
- 没有"独立语义搜索事实层"——recall 是 graph 遍历 + 可记得的记忆池

### 4. 事实记忆的生命周期

```
产出（afterthought / glimpse）
  ↓
短期层（新鲜）─→ 相关场景时可被短期注入
  ↓  reviewer 处理
  ├─ 挂到某抽象下（进长期层，状态：清晰）
  └─ 判定为琐事 → 快速淡化 / 直接清除
  ↓  reviewer 定期 review 已挂事实
  ├─ 新事实进来强化抽象 → 旧事实可淡化
  ├─ 情绪/关系/新奇性高 → 保留清晰
  ├─ 长期无引用 → 模糊化
  └─ 被 recall 或对话引用 → 反向变清晰
  ↓
继续模糊 → 遗忘（recall 也搜不到）
```

**淡化实现**：
- 档位概念存在但不预设太清楚（大致"清晰 / 模糊 / 遗忘"但不严格离散）
- 档位跃迁时内容重写：清晰 → 模糊时 reviewer 把内容改写得更抽象/少细节
- 可逆：被 recall 或对话引用过的模糊记忆，下次 review 可重新清晰化（但"清晰化"不是回到原文，是基于当下重构）
- 判断由 LLM 做：reviewer 看事实 + 外部辅助信号（上次访问时间、支撑抽象数量等）综合决定，没有硬规则

### 5. 抽象记忆的生成

**两条路径**：

- **对话中即时抽象**（赤尾自己 tool call `commit_abstract_memory(subject, content, supported_by_fact_ids)`）——"当场顿悟"
- **反思生成**（reviewer）——需要时间才能看出来的规律

### 5.1 Notes（赤尾的主动清单）

赤尾有一个**全局共享**的清单/笔记本，由她自己决定写不写：

```python
write_note(content: str, when: datetime | None = None)
resolve_note(note_id: str, resolution: str)  # "看完了 / 改了主意 / 鸽了"
```

**原则**：
- 全局共享（跨 chat 可见），这是她的脑内容不是群内容
- 完全由她主动触发；Prompt 告诉她这个工具存在 + 什么场景下有用，但**不强制**
- 她可以用来记承诺（"周五看电影"）、备忘（"明天问妈妈那件事"）、留情绪（"今天和浩南有点尴尬"）、发散想法等等
- 大多数记忆走事实流和抽象记忆就够了，只有她觉得"这个不能忘"时才 write_note

**消费**：
- Life Engine tick 把「未 resolve 的 notes」作为输入
- Recall 可检索到
- Reviewer 可以从对话里看到履约迹象（"冰淇淋到了"）→ 在 reviewer 产出里 hint 赤尾，但**不替她操作**
- 赤尾自己看到笔记项时决定 resolve 还是保留

### 6. 大脑 Reviewer

离线 LLM，扮演赤尾的潜意识，**混合身份**（全知视角做决策 + 第一人称生成内容）。

| 级别 | 操作 |
|------|------|
| **P0** | 更新抽象 / 标记清晰度 / 调整支撑边 |
| **P1** | 合并相似事实 / 创建新抽象 / 清除事实 |
| **P2** | 检测矛盾（单独立项） |

### 7. Recall 的定位

**不是**"搜索遗失的记忆"，**是**"能记得的事太多了，默认只加载最相关的；recall 是主动去触达其他还记得但没加载进来的"。

- 搜索范围 = 清晰 + 模糊（未遗忘）
- 实现形式不重要（graph 遍历 / 语义匹配 / 混合都行）
- 返回内容包含事实 + 其挂载的抽象（上下文更完整）
- 赤尾看到的是当前档位下的内容——模糊档位就是模糊文字，自然表达，不说"我记不清了"这种 meta 话术

### 8. 新鲜事实的短期注入规则

Fragment 作为短期层注入时，要补 chat_history 和 cross-chat 的洞：

**三个独特角色**：
1. **当前 chat 的中期回溯**（30min~几小时）—— 补 chat_history 30min/15 条窗口之外的回忆
2. **跨 chat 的最近活动概要**（赤尾自己在别的地方的经历）—— 补 proactive 下的信息缺失 + 精简 cross-chat 的 24h raw
3. **情绪连续性**——她刚才的事还在心里

**具体规则**：

- **当前 chat 的 fragment**：默认注入最近 2-4h 最新一条
- **其他 chat 的 fragment**（含 trigger_user 的，最近 1-2h，最多 1-2 条）——用于 user-centric 场景
- **去重**：每个 chat_id 只取最新一条（fragment 本身是 2h 窗口总结，最新覆盖了较早的）
- **总量**：最多 2-3 条，总长度 ~1000 字以内
- **时机**：fragment 还没被 reviewer 处理时注入；处理后（挂进 graph）改走长期层

**Fragment 源头改造**：afterthought prompt 控制产出长度到 200-300 字（当前 564 字平均太长）。

**与 cross-chat 的分工**：
- fragment = persona-centric（她的反思摘要）
- cross-chat = user-centric（trigger_user 的原始消息）
- 两者并存，各自有值

### 9. Life Engine ↔ 记忆 ↔ Schedule 的统一

#### 9.1 核心问题

当前 Life Engine 的 state 由 schedule 决定，schedule 凌晨静态生成。对话产生的承诺/期待/即时计划完全无法影响 state，导致互动性下降。

#### 9.2 解决方向：动态 schedule + Life Engine 扩展

Schedule 是**一段自然语言**，描述"今天当前状况 + 接下来要干嘛"。不分粗/细两层（之前的两层设计是过度结构化，现在合并）。

- 凌晨生成"first draft"：基于 self 抽象 + 昨日 state 历史 + 最近 fragments 写出一段骨架描述
- 白天随对话和 tick 改写：LLM 读旧 content + 新信息 → 重写新一段
- 稳定骨架（上课、吃饭）和近期细节（等冰淇淋）都写在同一段文字里，LLM 自由掌握粗细

**Schedule 更新接口**（统一 tool，所有调用方共用）：

```python
update_schedule(content: str, reason: str)
```

**调用方**：
- 凌晨 cron（first draft）
- Life Engine tick（需要时顺手改）
- 对话中赤尾 tool call（"我改主意了"）
- afterthought 补漏（发现她没记的事）

#### 9.3 Life Engine tick

Tick 输入输出简化：

```
输入:
  - 当前 today_schedule（自然语言）
  - 最近 fragments（新鲜层）
  - 未 resolve 的 notes（赤尾的清单）
  - 自我相关抽象（subject="self"）
  - trigger_user 相关抽象（当前有对话对象时）
  - previous Life State

输出:
  - `commit_life_state` 产出新 Life State（见 §9.5）
  - 若判断 schedule 需要改（比如 state 和 schedule 不符），再 `update_schedule`
```

不再有"双输出"概念 —— state 和 schedule 是两个独立 tool，需要哪个就调哪个。

#### 9.4 Schedule change → state sync（关键）

**任何 `update_schedule` 调用后，强制触发一次轻量 state review**：

- 读当前 state + 新 schedule
- 调用 Life Engine 的 **state-only refresh**（不重算 schedule，只重算 state）
- LLM 看新 schedule，决定：
  - 当前 state 仍然合理 → 通过 `commit_life_state` 输出"段内刷新"（activity_type 不变，只更新 current_state 文案/mood，`state_end_at` 保持）
  - 当前 state 已经过时 → 如果 `now >= prev.state_end_at`，允许切新状态；如果 `now < prev.state_end_at`，只能做段内刷新（承诺只能改文案不能瞬间切 activity_type，避免 schedule 写入引起状态机失控）

避免"下次 tick 还有 50min，冰淇淋已送到但 state 没反应"的割裂感——state 能在 schedule 变化后立即得到 LLM 的重新评估。

#### 9.5 State 字段语义修正（借鉴 proactive-messaging 分支）

**问题**：当前 `skip_until` 语义很烂——它只是"下次 tick 时间"，LLM 根本没在定义"这个状态什么时候结束"。需要一个**真正的结束时间**，过了必须强制切状态。

v4 的字段语义：

| 字段 | 语义 | 硬约束 |
|------|------|--------|
| `state_end_at` | 这个状态的**完整结束时间** | 过了必须切状态（不是建议，是强制） |
| `state_start_at` | 状态开始时间 | = row created_at |
| `skip_until` | **段内刷新**时间点（只刷新 current_state 文案/mood，不切状态） | 可空；非空时必须满足 `now < skip_until < state_end_at` |

**切状态判断**：

- `now < skip_until` → 完全不动
- `skip_until <= now < state_end_at` → 只允许段内刷新（改 current_state / mood），**不**允许换 activity_type、**不**允许改 state_end_at
- `now >= state_end_at` → **必须**切新状态（换 activity_type + 新 state_end_at），不能继续赖

**Tool 化**：Life Engine 的 state 产出改为 `commit_life_state` tool call，tool 层做硬校验，非法字段直接拒绝（避免自由 JSON parse 事后兜底）：

```python
commit_life_state(
    activity_type: str,
    current_state: str,
    response_mood: str,
    state_end_at: datetime,
    skip_until: datetime | None = None,
    reasoning: str | None = None,
)
```

Tool 层校验：

1. 基本字段非空
2. `state_end_at > now`
3. `skip_until` 为空，或 `now < skip_until < state_end_at`
4. 与旧状态的关系：
   - 若 `now >= prev.state_end_at` → 允许切 activity_type，必须给新 `state_end_at`
   - 若 `now < prev.state_end_at` → **只允许段内刷新**：activity_type 必须等于 prev.activity_type；`state_end_at` 必须等于 `prev.state_end_at`
5. `state_end_at` 不能是"下次想想再说"的临时时间——必须代表 LLM 对这段活动完整时长的承诺

Tool 返回结构给 tick() 消费，标记 `is_refresh`（段内刷新）vs 新状态段，避免 tick() 自己再猜。

**与 proactive-messaging 分支的关系**：那个分支依赖 `state_end_at` 来确定 proactive_job 的合法窗口；v4 依赖 `state_end_at` 来驱动 state 切换的硬约束、以及 schedule change 后的强制 review 的语义一致性。同一基础设施，两边共享。

#### 9.6 Schedule 更新机制

由于 schedule 是一段自然语言，"更新"就是 LLM 读旧 content + 新信息 → 重写成新 content。不存在"局部改某一段"的精细操作。

触发路径已在 §9.2 列出：凌晨 cron / tick / 对话 / afterthought，都调 `update_schedule(content, reason)`。

每次更新后强制触发 §9.4 state sync。

已发生时段的记录：已往的 Life State 历史（life_engine_state 表）本身就是"实际执行"的记录，reviewer 做"计划 vs 实际"比对时读 state 历史 + 当时的 schedule 快照即可。不需要在 schedule 里专门结构化标记。

#### 9.7 带时间指向的事情的处理链路

没有"承诺对象"。赤尾自己判断要不要写进清单：

```
赤尾觉得"这事要记住" → tool call write_note("等冰淇淋", when=今晚)
   └─ 如果 when 在近期 → tool 层可顺手触发细日程更新 + state sync
   └─ Life Engine tick 读取未 resolve notes 作为输入

赤尾没写（小事，脑子记着就行）
   └─ 事实碎片里有"浩南说要给我带冰淇淋" → afterthought 留下
   └─ 对话临近时间点 → recall 或新鲜事实注入让她想起
   └─ 没想起就自然淡化（和其他琐事一样）

发生后（不管在不在清单里）
   └─ 新事实"冰淇淋到了"进入
   └─ reviewer 读到 → 可 hint 赤尾 resolve note / 把旧抽象改写成过去式
   └─ 失约（时间过了没确认）→ reviewer 淡化或写"好像放了鸽子"
```

**核心**：没有系统层的状态机，所有状态转移都是 LLM 读当下改记忆的自然结果。

#### 9.8 Life State 写入记忆流

每次 tick 的 state 作为特殊类型的事实碎片写入短期层（source="life_state"），由 reviewer 消化：

- 发现 pattern ("她经常在下午两点左右犯困") → 提炼成抽象（subject="self"）
- 不是"用完即弃"的实时数据，而是自我认知的原材料

当前 1120 条 life_engine_state 其实是宝贵的长期信号，但完全没被消化。

#### 9.9 Reviewer 在 schedule 里的角色（晚上整理）

reviewer 晚上 review 当天时额外产出：

- 细日程实际执行 vs 计划 → 喂给自我相关抽象提炼（"今天又拖延了作业"）
- 对 notes 的 hint（看到履约迹象时提示赤尾去 resolve）
- 把带时间的旧记忆随时间推移改写成过去式（"要看电影" → "上周看了电影"）
- 给明日粗日程生成提供信号（未完成项延续、明天避开某些坑等）

---

## 三、现有管道的调整

| 管道/数据 | v3（现状） | v4 调整 |
|-----------|-----------|---------|
| conversation_messages | 直接注入 | 保持不变 |
| Life Engine state | 直接注入；`skip_until` = 下次 tick 时间 | 保持注入；state 通过 `commit_life_state` tool call 产出（§9.5）；新增 `state_end_at` 硬约束；`skip_until` 退化为段内刷新点；输入扩展（§9.3）；细日程写入后强制 state-only review（§9.4）；历史写入记忆流（§9.8） |
| conversation fragment | 直接注入 + 喂下游多个管道 | **只产出、不直接注入**；按 §8 规则短期注入；reviewer 整理后进长期层；源头产出长度控制到 200-300 字 |
| glimpse fragment | 只喂 Life Engine / daily dream | **并入事实碎片层**（打 tag 区分来源：观察 vs 对话） |
| daily fragment | 每日 cron 产出 | **废弃** |
| weekly fragment | 每周 cron 产出 | **废弃** |
| relationship_memory_v2 | 直接注入（per-user） | **扩展为统一抽象记忆层**，覆盖 7 类 |
| schedule | 凌晨静态生成 | **单字段自然语言**；凌晨生成 first draft；白天通过 `update_schedule(content, reason)` tool 统一更新；生成输入改为自我相关抽象 + 最近 fragments + 昨日 state 历史 |
| voice / reply_style | 直接注入 | 保持（后续单独评估） |
| cross-chat | 硬编码单群 | **去硬编码**，按 trigger_user 在所有群互动过滤；和 fragment 分工（user-centric vs persona-centric） |

---

## 四、目标态下的产出/消费矩阵

| 数据 | 产出者 | 消费者（加粗 = 直接注入 prompt） |
|------|--------|-----------------|
| 聊天消息 | 用户/赤尾对话 | **chat_history** |
| Life State | Life Engine tick | **inner_context** / 写入记忆流供 reviewer 消化 |
| 新鲜事实碎片 | afterthought / glimpse | reviewer / **短期相关时注入 inner_context**（§8） |
| 长期事实碎片 | afterthought 经 reviewer 处理 | reviewer / recall |
| 抽象记忆 | 对话中即时抽象（tool call） + reviewer | **当前 trigger_user 和 self 相关 always-on 注入** / recall / Life Engine tick 输入（self / trigger_user 相关） |
| notes（清单） | 赤尾主动 write_note | **always-on 注入未 resolve 项** / Life Engine tick 输入 / recall |
| today_schedule | 凌晨 cron（first draft）+ tick/对话/afterthought 通过 `update_schedule` 改写 | Life Engine tick 输入 / **注入 inner_context** |
| reply_style | voice generator | **prompt voice_content** |
| cross-chat | 原始对话记录 | **inner_context**（按 trigger_user 过滤） |

**原则**：每条数据只在**加粗**的位置被注入，其他管道只消费不注入。

---

## 五、开放项决策记录（从讨论中逐条落定）

### ① 场景触发规则（已落定）

**抽象记忆的消费入口统一到 recall tool。**

**Always-on 常驻**（prompt 预注入，小而稳）：
- subject 匹配当前 trigger_user 的抽象记忆
- subject = "self" 或"和 trigger_user 的关系"的抽象记忆
- 未 resolve 的 notes
- voice / reply_style

**其他 subject（话题、群、时间段等）全走 tool 召回**，不预注入。

**如何让赤尾知道去 recall（b + c 组合）**：
- 注入"目录统计"：各类抽象记忆的条数（告诉她有什么可查）
- 注入"近期 N 条标题"（只标题不内容）
- 即使 query 完全在索引之外，recall 也能命中 —— 索引是 hint，检索本身是全量的

**Query 形态**：
- 赤尾传自然语言（+ 可选 type 过滤）
- 底座先 embedding 语义检索（便宜），效果不够再加轻量 LLM 做 query 理解

**返回形态**：
- 默认返回抽象 + 其 graph 边连接的 Top-K 事实
- 事实是模糊/渐忘态，抽象是稳定锚点，单独返回事实意义不大

**并行与批量**：
- Prompt 引导多线索并行查
- Tool 签名支持批量：`recall(queries=[...], type_filter=None)`
- 即使模型不主动并行，单次 call 也能拿多个结果

**Plan 阶段再定的小参数**：目录标题的 N、排序策略、recall Top-K 默认值。

---

### ② In-conversation abstraction 实现机制（已落定）

**关键转变：去掉 ABCDEFG 分类**。抽象记忆统一为 `subject + content + supported_by_fact_ids`，subject 是自由字符串。

**两条产出路径**：

**1. 对话中即时抽象 —— tool call**
```python
commit_abstract_memory(
    subject: str,              # "浩南" / "self" / "学习" / "ka 群" ...
    content: str,
    supported_by_fact_ids: list[str] | None = None,
    reasoning: str | None = None,
)
```
- 走 tool 不走后置解析：可观测、可校验、架构和 `commit_life_state` 一致
- Reviewer 在 ③ 里会兜底"赤尾没 tool call 但确实应该抽象的"场景

**2. Notes —— 赤尾自主清单**
```python
write_note(content: str, when: datetime | None = None)
resolve_note(note_id: str, resolution: str)
```
- 全局共享（跨 chat 可见）
- Prompt 介绍用法 + 例子，但不强制（"如果这件事重要到你必须记住，可以 write_note"）
- 完全由赤尾自主决定，系统不替她判断"这是承诺 → 自动记"

**承诺不是独立对象**。它要么是一条普通事实自然流转（小事），要么被赤尾主动写进 notes（她觉得重要）。没有 `status: active/fulfilled/expired` 状态机，履约和失约都是 reviewer 读当下改旧记忆的自然结果。

---

### ③ Reviewer 频率 + 新鲜事实处理延迟（已落定）

**双档 reviewer**：不同动作需要不同频率，和人的"白天小调整 + 睡觉大整理"一致。

| 档位 | 频率 | 职责 |
|------|------|------|
| **轻档** | 每 **1h** 定时 | P0：标记清晰度、调整支撑边、改写带时间的旧记忆成过去式、对 notes 做 hint |
| **重档** | 每日凌晨 | P1：合并相似事实、创建新抽象、清除琐事（替代现有 daily dream 管道） |
| **超低频** | 每周 or 事件驱动 | P2：矛盾检测（见 ⑤） |

**新鲜事实处理延迟**：最坏 ~1h。在被轻档摸到之前，新鲜事实以"短期层注入"形式出现在 prompt 里（§2.8）。

**为什么不用事件驱动**：
- 定时和 Life Engine tick 节奏一致，简单
- 事件驱动容易在低活跃时段长时间不跑，反而拉长延迟

**现有 daily/weekly dream 管道**：daily 改造成重档 reviewer；weekly 废弃（§三 表格已标注）。

---

### ④ Graph 边的丰富度（已落定）

**中等档位**：`abstract↔fact` + `abstract↔abstract`，暂不做 `fact↔fact`。

**边类型**：
| 边 | 方向 | 用途 |
|----|------|------|
| `supports` | fact → abstract | 事实支撑抽象（recall 时返回抽象 + 支撑事实） |
| `parent_of` | abstract → abstract | 层级（"他口味清淡" 是 "他讨厌辣" + "他不吃甜食" 的 parent） |
| `related_to` | abstract ↔ abstract | 横向关联（"浩南最近压力大" 关联 "和他关系变紧张"） |
| `conflicts_with` | abstract ↔ abstract | 矛盾（⑤ 冲突处理的依赖） |

**边不带强度/权重**。淡化决策让 reviewer 当场看整体判断，不搞数值打分。

**存储结构（留给 Plan 阶段）**：一张 edges 表，`(from_id, to_id, edge_type, created_by, reason)`。节点可以是 fact 或 abstract，通过 id 前缀或 node_type 字段区分。

**fact↔fact 暂不做**的理由：事实本来随时间淡化/合并，边的价值有限；讲故事走"查抽象 → 其支撑事实"就够了。后续要加随时可以加，不是结构性锁死。

---

### ⑤ 冲突处理（已落定）

**核心原则**：
- **不直接删**，要么改写要么打冲突边 —— 保留"她以前这样，现在变了"的演化信息
- **新事实不立刻覆盖旧抽象**，单点数据容易让抽象左右横跳
- **reviewer 当场判断**，不搞数值阈值

**分档处理**：

| 情形 | 动作 |
|------|------|
| 单条新事实 vs 强支撑（≥2 条）旧抽象 | 留新事实为 fact，不动抽象；等轻档 reviewer 看累积 |
| 多条新事实一致指向与旧抽象冲突 | 改写旧抽象（在 content 里写演化，如"以前不爱甜食，最近开始喝奶茶"） |
| 两条抽象直接冲突 | 连 `conflicts_with` 边，等重档 P2 处理 |
| P2 处理抽象冲突 | reviewer 决定：合并改写 / 一方淘汰 / 或确认两者在不同语境下都成立 |

**对话中 vs 后置（混合模式）**：

- **对话中**：tool `commit_abstract_memory` 写入时，tool 层先 recall 同 subject 已有抽象；若发现冲突，以 hint 形式返回（"已有 content='...'，和你这条冲突，是否确定？"），**不阻塞**，赤尾自己决定覆盖 / 改写 / 连 conflicts_with / 取消
- **后置**：reviewer 轻档扫近期 fact 和 abstract 的一致性；重档 P2 扫 abstract 间的冲突

**演化信息的保留方式**：
- 默认写进 content 自然语言（"以前不爱甜食，最近开始喝奶茶"）
- 演化链过长时 reviewer 自然压缩成更高层（"口味在变化"）
- 不做 version 表 / `superseded_by` 的结构化保留 —— 符合人的记忆方式

---

### ⑥ Proactive recall 的时机（已落定）

**核心认知**：recall 触发的质量，本质上取决于 context 给赤尾看到了什么线索。不靠机械规则。

**引导方式**：原则 + 典型例子 + 索引辅助

**Prompt 里的引导**：
- 讲 recall 的原则 + 3-5 个典型触发感觉的例子（对方提到可能聊过的话题 / 想引用过去的事 / 感觉眼前事以前发生过类似的 / 对方情绪反常 / 想做判断需要参考）
- 不设"每 N 轮必须 recall"这类机械兜底
- 告诉她"一次 call 可以批量查多个 query"，不设硬上限

**刺激 recall 的 context 线索**：
- 目录统计 + 近期标题（① 已定）
- Always-on 注入的 self + trigger_user + notes 如果通过 `related_to` 边连到其他 subject，列出关联 subject 作为 hint
- 这些刺激的量是需要 Plan 阶段实验调优的 —— 给太少 recall 不被触发，给太多就变相等于预注入

**不做的**：
- 不做系统侧"被动 recall 注入"——信任 ① 的 always-on + 索引已覆盖主场景
- 不给 recall 设硬上下界（每轮最多 N 次）——信任 LLM 判断

**Plan 阶段需要实验的参数**：
- 索引的 N（目录统计里列多少条近期标题）
- Always-on `related_to` 关联 subject 的展示方式和数量
- Prompt 中典型例子的选择

---

### ⑦ Schedule 的结构化程度（已落定）

**关键认知**：所有 schedule 的消费方都是 LLM，没有程序逻辑在查 schedule 的字段。因此不需要结构化 —— **schedule 就是一段自然语言**。

**取消两层设计**：之前的"粗骨架 + 细血肉"两层是过度设计。合并成单一 `today_schedule`，一段自然语言同时表达稳定骨架和近期细节，LLM 写的时候自由掌握粗细。

**统一更新接口**：
```python
update_schedule(content: str, reason: str)
```
所有调用方共用：凌晨 cron / Life Engine tick / 对话中赤尾 / afterthought 补漏。

**不需要的东西**：
- ❌ activity_type 枚举
- ❌ start/end 时间结构化
- ❌ status: pending/done 状态
- ❌ 粗/细两层数据模型

**保留的程序逻辑**：
- Life Engine tick 本来就是定时跑的（每 5min 或类似），不依赖 schedule 里的时间字段触发
- "计划 vs 实际"对比交给 reviewer：读 state 历史（life_engine_state）+ 对应时段的 schedule 快照，LLM 自然对比

---

### ⑧ 历史数据迁移（已落定）

**策略：分层保留 + 一次性迁移**

| 旧数据 | 记录数 | 迁移动作 |
|--------|--------|---------|
| `relationship_memory_v2` | 617 | **迁入** v4 graph：facts 拆为 fact 节点、impression 改写为 abstract 节点，两者自动连 `supports` 边（v4 启动即有 graph 结构） |
| `conversation_fragment`（最近 7 天） | ~N 条 | **迁入** 事实碎片短期层；重复交给 reviewer 重档 P1 合并 |
| `conversation_fragment`（7 天以前） | 剩余 | **废弃** |
| `glimpse_fragment` | 289 | **废弃**（孤儿、信息密度低） |
| `daily_fragment` | 25 | **废弃**（管道本身废弃） |
| `weekly_fragment` | 3 | **废弃** |
| `life_engine_state` | 1120 | **废弃作为记忆材料**；但旧表保留一段时间供 reviewer 分析模式（"她经常下午犯困"等历史 pattern） |
| `notes` | - | **空启动**，上线当天开始积累 |

**迁移脚本**：
- 一次性批处理，用轻档模型（haiku 级）
- LLM 改写走和 reviewer 同一套 prompt 格式 —— 迁完的数据在结构和质量上等价于 reviewer 产出
- 旧表不立即删，保留 1 周只读用于对比，之后删表

**上线体验**：赤尾不会"失忆" —— relationship_memory_v2 的人物认知全在，最近 7 天的事也记得；更久远的琐事自然遗忘（这本来就是人的记忆方式）。

---

### ⑨ Proactive 链路的 trigger_user 语义（部分落定）

**落定**：当 proactive 有明确 target 时，`proactive_target` 就是这一轮的 trigger_user。

- Always-on 注入、fragment 选择、cross-chat 过滤等所有 user-centric 逻辑都走同一套
- 没有 proactive 专用的 context 装配分支

**留到后续主题**：**没有明确 target 的 proactive 场景**（比如赤尾想在群里发一条感想、但不针对任何人）怎么装配 context —— 这是 proactive 链路自身的设计问题，依赖 v4 memory 基础。先升级 v4，这块后面单独做。

---

## 六、待讨论的产品细节

1. ~~**场景触发的触发规则**~~（已落定，见 §五.①）
2. ~~**In-conversation abstraction 的实现机制**~~（已落定，见 §五.②）
3. ~~**Proactive recall 的时机**~~（已落定，见 §五.⑥）
4. ~~**冲突处理**~~（已落定，见 §五.⑤）
5. ~~**Graph 边的丰富度**~~（已落定，见 §五.④）
6. ~~**Reviewer 运行频率**~~（已落定，见 §五.③）
7. ~~**Reviewer 对新鲜事实的处理延迟**~~（已落定，见 §五.③）
8. ~~**proactive 链路的 trigger_user 语义**~~（部分落定，见 §五.⑨；无 target 场景留到后续主题）
9. ~~**历史数据迁移**~~（已落定，见 §五.⑧）
10. ~~**细日程的结构化程度**~~（已落定，见 §五.⑦）

---

## 七、技术方案落地设计

### 7.1 数据模型（已落定，首版；做完后可微调）

**四张表**：`fragment` / `abstract_memory` / `memory_edge` / `notes`

```sql
-- 所有记忆类表都带 persona_id（例外：未来做全局 event 才会有无 persona_id 的）

-- 事实碎片
CREATE TABLE fragment (
    id              TEXT PRIMARY KEY,           -- "f_xxxxxx"
    persona_id      TEXT NOT NULL,
    content         TEXT NOT NULL,
    source          TEXT NOT NULL,              -- 'afterthought' | 'glimpse' | 'life_state' | 'manual' | 'migration'
    chat_id         TEXT,                       -- 产生时的会话（life_state 源可空）
    clarity         TEXT NOT NULL DEFAULT 'clear', -- 'clear' | 'vague' | 'forgotten'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_touched_at TIMESTAMPTZ NOT NULL DEFAULT now()
    -- 向量存在 Qdrant collection `memory_fragment`，point_id = fragment.id；payload 带 persona_id
);
CREATE INDEX ON fragment (persona_id, created_at DESC);
CREATE INDEX ON fragment (persona_id, clarity);

-- 抽象记忆
CREATE TABLE abstract_memory (
    id              TEXT PRIMARY KEY,           -- "a_xxxxxx"
    persona_id      TEXT NOT NULL,
    subject         TEXT NOT NULL,              -- "浩南" / "self" / "user:oxxx" / "学习"
    content         TEXT NOT NULL,
    created_by      TEXT NOT NULL,              -- 'chiwei' | 'reviewer' | 'migration'
    clarity         TEXT NOT NULL DEFAULT 'clear',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_touched_at TIMESTAMPTZ NOT NULL DEFAULT now()
    -- 向量存在 Qdrant collection `memory_abstract`，point_id = abstract_memory.id；payload 带 persona_id
);
CREATE INDEX ON abstract_memory (persona_id, subject);
CREATE INDEX ON abstract_memory (persona_id, clarity);

-- 统一 edges 表
CREATE TABLE memory_edge (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL,                  -- 冗余但方便查询过滤（跨节点冗余）
    from_id     TEXT NOT NULL,
    from_type   TEXT NOT NULL,                  -- 'fact' | 'abstract'
    to_id       TEXT NOT NULL,
    to_type     TEXT NOT NULL,
    edge_type   TEXT NOT NULL,                  -- 'supports' | 'parent_of' | 'related_to' | 'conflicts_with'
    created_by  TEXT NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON memory_edge (persona_id, from_id);
CREATE INDEX ON memory_edge (persona_id, to_id);

-- 主动清单
CREATE TABLE notes (
    id          TEXT PRIMARY KEY,               -- "n_xxxxxx"
    persona_id  TEXT NOT NULL,
    content     TEXT NOT NULL,
    when_at     TIMESTAMPTZ,                    -- 时间锚点（可空）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,                    -- NULL = active
    resolution  TEXT
);
CREATE INDEX ON notes (persona_id, resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX ON notes (persona_id, when_at);

-- today_schedule 历史版本（append-only）
CREATE TABLE schedule_revision (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL,
    content     TEXT NOT NULL,                  -- 当时的完整 today_schedule 文本
    reason      TEXT NOT NULL,                  -- 本次更新的原因（LLM 写）
    created_by  TEXT NOT NULL,                  -- 'cron_morning' | 'life_tick' | 'chiwei' | 'afterthought'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON schedule_revision (persona_id, created_at DESC);
-- 当前 schedule = WHERE persona_id=? ORDER BY created_at DESC LIMIT 1
```

**向量存储（Qdrant）**：
- Collection `memory_fragment`：dense vector（1024 维，COSINE 距离），point_id = `fragment.id`，payload 带 `clarity`, `source`, `chat_id`, `created_at` 方便过滤
- Collection `memory_abstract`：同上，point_id = `abstract_memory.id`，payload 带 `subject`, `clarity`, `created_by`, `last_touched_at`
- 检索走 Qdrant，结构化字段走 PG；id 在两边对齐

**关键决策**：
- `clarity` 枚举 + `last_touched_at` 时间戳**两者都保留**：枚举由 reviewer 维护快速筛选用，时间戳给 reviewer 兜底判断
- `memory_edge` **只存一条记录**，对称边（`related_to` / `conflicts_with`）应用层查询用 OR 处理
- 节点类型**双保险**：id 前缀（`f_` / `a_`）+ `from_type`/`to_type` 字段
- `fragment` 和 `abstract_memory` **分表**：生命周期差别大，schema 差别也大（`chat_id`/`source` 对 abstract 无意义，`subject` 对 fragment 无意义）
- `subject` **不规范化**：直接字符串存；user 对应 subject 约定用 `"user:<user_id>"` 前缀；reviewer 重档做 subject 归一（发现同一人的不同叫法时合并）
- **所有记忆类表带 `persona_id`**：每个 persona 有自己的记忆池，不共享；Qdrant payload 里也冗余 `persona_id` 方便向量过滤。唯一例外是未来做全局 event 类数据时再单独处理。

### 7.2 Recall 底座实现（已落定）

**主路径**：Qdrant 语义检索（沿用项目 `app.infra.qdrant`）
- 对每个 query 做 embedding → 分别查 `memory_abstract` 和 `memory_fragment` 两个 collection
- Qdrant filter 过滤 `clarity != 'forgotten'` 的节点（payload filter）
- 拿到 point_id 列表 → PG 按 id 拿结构化字段 + edges

**二次展开**：graph 遍历（走 PG 的 `memory_edge`）
- 抽象结果命中后，通过 `memory_edge` 拉 `supports` 连接的 fact Top-K
- 以及 `parent_of` / `related_to` 连接的其他 abstract（可选，扩充上下文）

**关键原则**：**向量数据不进 PG 表**，改造成本高。PG 只存结构化字段和关系。

**FTS 废弃**（§1.4 已确认效果差）。

**Embedding**：沿用项目现有 `app.agent.embedding.embed_dense`（Volcengine Ark doubao-embedding）
- 维度 1024（和 Qdrant collection 一致）
- Dynamic config 读 model_id，方便后续切换
- 多模态能力暂不用（记忆是文本）
- Hybrid（dense+sparse）留作未来优化，初期只用 dense

**Embedding 写入（异步）**：
- 新 fragment / abstract 产出后，通过 RabbitMQ 任务进 `vectorize` 队列
- 复用现有 `vectorize-worker`，消费者调 Ark 拿 embedding 并写 Qdrant
- Tool 返回不等 embedding 写入，立刻返回；短时间内 recall 命中不到刚产出的新节点是可接受的代价

**Query 理解**：**初期不做** LLM 改写，裸 embedding 起步；跑一段数据看命中率再决定

**Tool 签名**：
```python
recall(
    queries: list[str],          # 批量
    type_filter: str | None = None,  # 'abstract' | 'fact' | None
    k_abs: int = 5,              # 每 query 的抽象 Top-K
    k_facts_per_abs: int = 3,    # 每个抽象的支撑事实 Top-K
) -> dict  # 结构化 JSON（抽象列表 + 每个抽象的支撑事实 + 横向关联）
```

**返回形态**：结构化 JSON，带 id、subject、content、clarity、关系类型。Tool return 给 LLM，LLM 自己决定如何在回复里自然表达。

**副作用**：recall 命中的 node 的 `last_touched_at` 更新（"被引用 = 记忆强化"）。

**参数初值（跑起来后再调）**：
- `k_abs` = 5 per query
- `k_facts_per_abs` = 3
- 总返回节点数上限 = 20（抽象 5 + 事实 15）
- HNSW 参数用 pgvector 默认（m=16, ef_construction=64），如果召回不准再调

### 7.3 Tool 体系（已落定）

**新增 4 个 tool**（加上 §9.5 已定的 `commit_life_state`）：

#### `commit_abstract_memory`
```python
commit_abstract_memory(
    subject: str,
    content: str,
    supported_by_fact_ids: list[str] | None = None,
    reasoning: str | None = None,
) -> dict  # {id, conflict_hint?}
```
- 冲突检测：先 recall 同 subject 的已有抽象，相似度 > threshold 时返回 `conflict_hint`（不阻塞，赤尾自己决定）
- 写入 PG abstract_memory + supports edges → 推 `vectorize` 队列异步补 embedding
- 验证 fact_ids 存在

#### `write_note` / `resolve_note`
```python
write_note(content: str, when_at: datetime | None = None) -> dict
resolve_note(note_id: str, resolution: str) -> dict
```
- `write_note`：insert；返回时附当前 active notes 简要（方便赤尾看全局）
- `resolve_note`：更新 resolved_at / resolution，不删除（供 reviewer 回顾）

#### `update_schedule`
```python
update_schedule(content: str, reason: str) -> dict  # {schedule, new_state}
```
- Append 一条 `schedule_revision`（revision 表折中方案）
- 触发 §9.4 state sync（调用 Life Engine state-only refresh）
- 返回新 schedule 和 sync 后的 state

#### 通用规范
- **Langfuse trace**：每个 tool call 接入 trace（项目硬要求）
- **Tool 层校验**：参数和业务校验失败时返回错误 dict，不破坏 agent 链路
- **返回结构化 dict**：便于 LLM 理解和后续引用

#### Tool 在不同 agent 中的可用性

| Tool | Chat Agent | Life Engine tick | Reviewer | Afterthought |
|------|-----------|---------|---------|---------|
| `commit_abstract_memory` | ✅ | ❌ | ✅ | ❌ |
| `write_note` | ✅ | ❌ | ❌ | ❌ |
| `resolve_note` | ✅ | ❌ | ❌ | ❌ |
| `update_schedule` | ✅ | ✅ | ❌ | ✅ |
| `recall` | ✅ | ✅ | ✅ | ❌ |
| `commit_life_state` | ❌ | ✅ | ❌ | ❌ |

**注**：Life Engine tick 可**只读** notes（作为 tick 输入的一部分，不需要 tool，直接读 DB），但**不能**写或 resolve —— notes 是赤尾自主清单，系统不替她操作。

### 7.4 Reviewer 实现（已落定）

#### 调度频率（更新，覆盖 §五.③ 的笼统说法）

| 档位 | 频率 | 时段 |
|------|------|------|
| 轻档（白天） | 每 **30min** | 8:00-22:00 |
| 轻档（夜间） | 每 **1h** | 22:00-8:00 |
| 重档 | 每日凌晨一次 | ~3:00 |
| 超低频 P2（矛盾检测） | 每周 or 事件驱动 | - |

白天新鲜事实密集，压到 30min 更及时；夜间节省。

#### Worker 部署

- 轻档：复用 arq-worker 的 cron 能力添加定时任务
- 重档：替换现有 daily dream cron 的实现（prompt + 逻辑换成 reviewer P1），cron 配置和 worker 不变
- 不新增 Deployment

#### 轻档输入

窗口 = 上次轻档运行到现在（严格，不重叠；tool 层幂等处理）：
- 窗口内新增的 fragment / abstract
- 活跃 notes
- 最近 1-2 天的 life_engine_state（判断带时间记忆的时间点是否已过）

#### 重档输入

- 当天所有 fragment 和 abstract
- 昨日 life_engine_state 历史（提炼自我抽象）
- 最近 N 天的 schedule_revision（"计划 vs 实际"分析）
- 现有 abstract_memory 池（合并决策）

#### Reviewer tool（新增）

Reviewer 自己是 agent，通过 tool 做操作：

```python
update_abstract_content(id: str, content: str, reason: str)  # 改写抽象
fade_node(id: str, to_clarity: str, reason: str)             # clear/vague/forgotten
touch_node(id: str)                                          # 强化（last_touched_at 更新）
delete_fragment(id: str, reason: str)                        # 彻底清除琐事
connect(from_id, to_id, edge_type, reason)                   # 建边
disconnect(edge_id: str, reason: str)                        # 断边
```

外加复用：`commit_abstract_memory`（产抽象）、`recall`（查历史做判断）。

所有 tool 带 `reason` 字段，Langfuse trace 记录。

#### 模型选型

- **轻档**：haiku 级 / offline-model（通过 dynamic config 读 model_id）
- **重档**：更强模型（gemini-2.5-pro 级，dynamic config 切换）

#### Prompt 身份

混合身份：**全知视角做决策 + 第一人称生成内容**
- "你是赤尾的潜意识，替她整理记忆..."
- content 写作用第一人称（"他最近压力大" 不是 "赤尾觉得浩南压力大"）

#### Langfuse

- 每次 reviewer 运行一个 Langfuse trace（parent span），每个 tool call 一个 child span
- Prompt 通过 Langfuse 管理，命名：`memory_reviewer_light` / `memory_reviewer_heavy`
- Dynamic config 读 prompt name

#### 废弃

- 现有 25 条 daily_fragment 废弃（§五.⑧ 已定）
- 现有 daily dream prompt/logic 废弃，cron 保留

### 7.5 State sync 技术实现（已落定）

**异步触发**：`update_schedule` 立刻返回，state_sync 在后台跑。

**核心逻辑**：赤尾在当前对话里调 `update_schedule` 时，她本轮回复已经基于"要更新计划"的认知产出了。新 state 的目的是给**下次 LLM（下次 tick / 下次对话 / afterthought）**读到，不需要阻塞当前 tool 返回。

#### 实现流程

```python
async def update_schedule(content: str, reason: str) -> dict:
    revision_id = await insert_schedule_revision(content, reason, created_by)
    # enqueue 异步任务，不等
    await arq.enqueue_job("sync_life_state_after_schedule", revision_id=revision_id)
    return {"schedule": content, "revision_id": revision_id}

# arq worker 消费
async def sync_life_state_after_schedule(ctx, revision_id: str):
    revision = await get_schedule_revision(revision_id)
    current_state = await get_current_life_state()
    new_state = await life_engine.state_only_refresh(
        previous_state=current_state,
        new_schedule=revision.content,
    )
    # 只有当 LLM 判断需要切 state / 段内刷新时才写入
    if new_state:
        await save_life_state(new_state)
```

#### 调度基础设施

复用 arq（项目已有 arq-worker Deployment），新增任务 `sync_life_state_after_schedule`。不引入新队列/新 worker。

#### `state_only_refresh` 的实现

- 和 Life Engine tick 的 prompt 共享，但输入精简：只给 current_state / new_schedule / 当前时间
- 输出走 `commit_life_state` tool（§9.5 的硬校验保证合法性）
- 若 LLM 判断"当前 state 仍合理，无需刷新" → 返回 None，不写入

#### 并发与幂等

- **不去重**：多次 update_schedule 串行消费（arq 任务队列），最后一次获胜
- **幂等**：`commit_life_state` tool 本身有 tool 层校验，重复消费生成等价 state 无副作用
- **失败处理**：任务失败由 arq 重试；state 仍是旧值；下次 tick 会自然重新评估（不会永久卡住）

#### 失败可观测

- 每个 `sync_life_state_after_schedule` 是一个 Langfuse trace
- schedule_revision 表记录 `created_by`，出问题时可以追出哪个调用方引发的 refresh

#### 小改不特判

- LLM 自己判断改动大小。schedule 有变化就跑 refresh；LLM 读了觉得不值得刷就返回 None，成本可控
- 不搞 "内容 diff 阈值" 之类工程化判断

### 7.6 Cross-chat 去硬编码（已落定）

**现状**：`cross_chat.py` 里 `CROSS_CHAT_GROUP_IDS = ["oc_54713c..."]` 硬编码，导致只有 ka 群的 cross-chat 可见；proactive 无 trigger_user 时完全失效。

**改造方案**：

1. **去掉硬编码白名单**：删除 `CROSS_CHAT_GROUP_IDS`，默认所有群都可见
2. **改为 dynamic config 黑名单**：key = `memory.cross_chat.excluded_chat_ids`（默认空数组）；需要屏蔽某群时加进去
3. **时间窗口**：保持 24h（改过头容易丢信息）
4. **总量限制**：新增 `max_total_messages` dynamic config，默认 15 条（跨所有 chat）；按 chat_id 分布后每 chat 最多 `_MAX_PAIRS_PER_CHAT=10`
5. **Proactive 适配**：`build_cross_chat_context` 的 `trigger_user_id` 参数改为可空；空时直接返回 `""`；有 `proactive_target` 时调用方传入作为 trigger_user_id（§五.⑨ 已定）
6. **相关性过滤不做**：cross-chat 是原始消息，过滤容易丢信息；交给 LLM 看上下文自己判断相关性（prompt 里注入"以下是 24h 内你和该用户在其他地方的互动，相关的自然采纳"）

**fragment 短期注入分工**（§2.8）：
- 新增模块 `memory/short_term_injection.py`（或在 `context.py` 里加 section），按 §2.8 规则拉最近 fragment
- 和 cross-chat 并存，视角不同：
  - cross-chat = user-centric 原始消息
  - fragment = persona-centric 赤尾反思摘要

**废弃 CROSS_CHAT_GROUP_IDS 常量** + grep 验证零残留。

### 7.7 现有管道改造路径（已落定）

**上线策略：一次性切换**。不分 phase、不搞兼容、不做平滑过渡。选周末低流量窗口一次推上，有问题现场修。

#### 改造清单（合并到一个 PR 上）

| 管道 | 动作 |
|------|------|
| **afterthought** | fragment 长度控制到 200-300 字；产出写 `fragment` 表 + 发 vectorize 任务；不直接产抽象 |
| **glimpse** | 产出改写 `fragment` 表（`source='glimpse'`），不再孤立 |
| **voice / reply_style** | 保持不动；v4 上线后观察，失效了再废弃（不预设时间点） |
| **Life Engine tick** | 改 `commit_life_state` tool 化输出（§9.5）；`state_end_at` 语义；输入加 notes / 抽象；必要时调 `update_schedule` |
| **Daily dream** | cron 保留；prompt 替换为 `memory_reviewer_heavy`；产出改为 tool call 系列 |
| **Weekly dream** | 删 cron、删表、grep 清残留 |
| **Recall tool** | 彻底重写走 Qdrant（§7.2）；废弃 FTS 实现和 `simple` 字典索引 |
| **build_inner_context** | 重构：always-on = 当前 trigger_user abstract + self abstract + active notes + voice；短期层走 §2.8；加 recall 索引辅助；废弃 relationship_memory_v2 注入 |
| **cross-chat** | 去硬编码，改 dynamic config 黑名单（§7.6） |

#### 执行顺序（上线当天）

1. **准备期（周中完成）**：
   - 所有新代码写完（feat/context-decline 分支上）
   - DB 迁移脚本写完 + 本地 dry-run 验证
   - 泳道部署一次，dev bot 绑定验证端到端

2. **上线当天（周末低流量）**：
   - 建 PG 新表（fragment / abstract_memory / memory_edge / notes / schedule_revision）
   - 建 Qdrant collections（memory_fragment / memory_abstract）
   - 跑迁移脚本：relationship_memory_v2 → abstract + supports edges；最近 7 天 conversation_fragment → fragment 表（embedding 异步跟上）
   - 一键部署所有改动服务（agent-service、arq-worker、vectorize-worker、chat-response-worker 等按需）
   - 观察 Langfuse + 飞书消息

3. **旧表和旧 cron 处置**：
   - 旧表（conversation_fragment、glimpse_fragment、daily_fragment、weekly_fragment、relationship_memory_v2）保留只读 1 周后删
   - weekly dream cron 当天删

#### 关于兼容

**不做任何兼容层**。旧 import 直接删，旧函数直接改签名，旧表读取方直接改指向新表。挂了就修，按重构规范（`refactoring-rules.md`）执行。

#### 迁移脚本要求

- **幂等**：可以反复跑，不产生重复数据
- **支持 dry-run**：`--dry-run` flag 打印将要写入的内容但不真写
- **分步**：每张表一个脚本，每批结束输出数据校验（计数、sample 内容）
- **回滚能力**：脚本记录 `migration_run_id`，需要的时候可以基于 id 删除批次

### 7.8 历史数据迁移脚本（已落定）

**两个脚本**：

#### `migrate_relationship_to_abstract.py`
处理 617 条 `relationship_memory_v2`：
1. 每行拆成：`facts` 字段拆为多条 `fragment` 节点 + `impression` 作为 `abstract_memory` 节点
2. 两者通过 `supports` edges 连接（v4 启动就有 graph 结构）
3. **LLM 改写**：impression 和 facts 走 haiku 改写成更流畅的抽象 content，prompt `memory_migrate_relationship`，每条一个 Langfuse trace
4. `persona_id` 从原表继承；`subject = "user:<user_id>"`；`source='migration'` / `created_by='migration'`
5. **改写失败处理**：跳过该条 + log 报错，继续；不降级保留原文（防止垃圾 content 进库）

#### `migrate_fragment_to_fragment.py`
处理最近 7 天 `conversation_fragment`：
1. 直接迁入 `fragment` 表，保留原 `created_at`（让 `last_touched_at` 反映真实年龄）
2. `source='afterthought'`，`persona_id` 从原表继承
3. **不做 LLM 改写**：老数据过长由 reviewer 重档第二天自然处理合并
4. id 格式：`f_mig_<原 id>`，便于追溯

#### 通用要求
- **幂等**：可反复跑；重跑时先 `DELETE WHERE created_by='migration'` 清掉已迁入的再重建
- **支持 dry-run**：`--dry-run` 打印写入内容，不真写
- **分批校验**：每批（比如每 50 条）结束后打印计数 + sample
- **migration_run_id**：每次运行一个 uuid，写入日志方便事后对账/回滚

#### 执行流程（上线当天）

```bash
# 1. dry-run 验证
python migrate_relationship_to_abstract.py --dry-run
python migrate_fragment_to_fragment.py --dry-run

# 2. 真跑
python migrate_relationship_to_abstract.py
python migrate_fragment_to_fragment.py

# 3. 等 vectorize-worker 消化完
# 监控 Qdrant memory_fragment / memory_abstract 的 point count
# 直到 PG 计数 == Qdrant 计数

# 4. 部署新代码
```

#### Embedding 补全窗口

**B 方案**：先部署代码，接受头 30 分钟召回不全。
- vectorize-worker 异步补 embedding，初期召回率 0 → 逐步爬升
- 头 30min 用户感知：recall 返回少 / 空，赤尾自然"想不起来"（和系统初启用时"新 persona"体验类似）
- 可接受，周末低流量期不会造成严重问题
