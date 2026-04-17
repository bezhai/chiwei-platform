# Memory System v4 设计大纲

> 状态：**草案（产品形态第三版，技术方案待讨论）**
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

### 2. 记忆的内容类型

| 类型 | 形态 | 示例 |
|------|------|------|
| A 关于人 | 抽象 + 近期事实 | "他是工程师" / "昨天刚换工作" |
| B 关于事件 | 事实起步，淡化或抽象化 | "浩任艾特我三次" / "浩任老是烦我" |
| C 关于话题/偏好 | 抽象 | "我讨厌辣的" |
| D 关于群组 | 抽象 | "ka 群平时很吵" |
| E 关于自我 | 抽象 | "我最近变温柔了" |
| F 关于承诺/约定 | 事实 + 活跃状态 | "等浩南送冰淇淋过来" |
| G 共同回忆 | 事实/抽象都有 | "我和 A 哥一起吵过架" |

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

- **对话中即时抽象**（赤尾自己）——"当场顿悟"
- **反思生成**（reviewer）——需要时间才能看出来的规律

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

Schedule 作为唯一的"当下和接下来要做什么"的 source of truth，但 schedule 本身是活的。

```
┌──────────────────────────────────────────────────────┐
│ 粗日程（骨架，凌晨生成）                             │
│   基于 E 类自我认知 + 昨日实际执行 + 最近 fragments  │
│   大时段划分: "8-16 学校" / "17-22 家"               │
│   稳定，很少改                                       │
└──────────────────────────────────────────────────────┘
                         │  扩展
                         ↓
┌──────────────────────────────────────────────────────┐
│ 细日程（血肉，白天动态填充）                         │
│   在粗日程 slot 里填具体活动                         │
│   按"活动"分，有自然起止但不严格对时                 │
│   来源：                                              │
│   - Life Engine tick 自动填充未来 1-2h               │
│   - 对话 tool call（赤尾自己 update_schedule）       │
│   - afterthought 补漏（发现她没记的承诺）            │
└──────────────────────────────────────────────────────┘
```

#### 9.3 Life Engine tick 的双输出

Tick 从"只生成 state"升级为"同时生成 state 和细日程填充"：

```
输入:
  - 粗日程（当前 slot）
  - 已填充的细日程（当天到目前为止）
  - 最近 fragments（新鲜层）
  - 活跃 F 类承诺
  - E 类自我认知
  - previous Life State

输出:
  - 当前 Life State（started_at / expected_duration / mood / activity / reasoning）
  - 未来 1-2h 的细日程填充或更新
```

#### 9.4 Schedule change → state sync（关键）

**任何 schedule 写入（tool call / 重生成 / afterthought 补漏）后，立即触发一次轻量一致性检查**：

- 读当前 state + 新细日程
- 如果**当前时间应该做的事**和 **running state** 不一致 → 立即触发 Life Engine 的 **state-only refresh**（不重算细日程，只重算 state）
- 如果一致 → 不动

避免"下次 tick 还有 50min，冰淇淋已送到但 state 没反应"的割裂感。

#### 9.5 `skip_until` 语义修正

当前混淆了"调度"和"内容"两个概念。v4 明确分开：

| 字段 | 语义 | 作用 |
|------|------|------|
| `next_tick_at` | 下次触发 tick 的时间 | 调度（什么时候醒过来看一眼） |
| `state.started_at` | 当前 state 开始时间 | 内容（持续多久了） |
| `state.expected_duration` | 预期持续时长（LLM 提示性估计） | 内容（她觉得大概多长时间） |

`next_tick_at` 默认 = `started_at + expected_duration`，但可以被外部事件调整（承诺、冲突、对话事件都会重算）。"状态结束了吗"由 LLM 每次 tick 时判断，不是 `next_tick_at` 到了就自动结束。

#### 9.6 细日程重生成机制

几种触发路径：

- Life Engine tick 时发现"当前细日程和 state 严重不符" → 自动重生当前时间段往后
- 对话 tool call（"我改主意了"） → 显式重生
- 重生**只覆盖当前时间往后**，保留已发生时段的记录（供 reviewer 分析"计划 vs 实际"）

重生后同样触发 9.4 的 state sync。

#### 9.7 承诺的处理链路

```
对话产生承诺 ("我给你带冰淇淋")
  ├─ 赤尾 tool call update_schedule
  │  └─ 细日程新增 "等冰淇淋" 活动
  │     └─ 触发 state sync → 立即 state-only refresh
  ├─ afterthought 兜底
  │  └─ fragment 产出时检测承诺，补漏到细日程
  └─ 长期承诺（未具体安排到今日的）
     └─ F 类抽象记忆，active 状态
        └─ Life Engine tick 读取活跃 F 类作为输入

承诺履约后
  └─ reviewer 检测（下次对话提到"冰淇淋到了"）
     └─ F 类状态 active → fulfilled
        或 active → expired（超时未履约）
```

#### 9.8 Life State 写入记忆流

每次 tick 的 state 作为特殊类型的事实碎片写入短期层（source="life_state"），由 reviewer 消化：

- 发现 pattern ("她经常在下午两点左右犯困") → 提炼成 E 类抽象
- 不是"用完即弃"的实时数据，而是自我认知的原材料

当前 1120 条 life_engine_state 其实是宝贵的长期信号，但完全没被消化。

#### 9.9 Reviewer 在 schedule 里的角色（晚上整理）

reviewer 晚上 review 当天时额外产出：

- 细日程实际执行 vs 计划 → 喂给 E 类抽象提炼（"今天又拖延了作业"）
- 承诺状态转移（F 类：active → fulfilled / expired / withdrawn）
- 给明日粗日程生成提供信号（未完成项延续、明天避开某些坑等）

---

## 三、现有管道的调整

| 管道/数据 | v3（现状） | v4 调整 |
|-----------|-----------|---------|
| conversation_messages | 直接注入 | 保持不变 |
| Life Engine state | 直接注入 | 保持注入；输入扩展（见 §9.3）；历史写入记忆流（§9.8） |
| conversation fragment | 直接注入 + 喂下游多个管道 | **只产出、不直接注入**；按 §8 规则短期注入；reviewer 整理后进长期层；源头产出长度控制到 200-300 字 |
| glimpse fragment | 只喂 Life Engine / daily dream | **并入事实碎片层**（打 tag 区分来源：观察 vs 对话） |
| daily fragment | 每日 cron 产出 | **废弃** |
| weekly fragment | 每周 cron 产出 | **废弃** |
| relationship_memory_v2 | 直接注入（per-user） | **扩展为统一抽象记忆层**，覆盖 7 类 |
| schedule | 凌晨静态生成 | **两层（粗骨架 + 细血肉）**；细日程白天动态更新；粗日程生成输入改为 E 类抽象 + 最近 fragments + 昨日实际执行 |
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
| 抽象记忆 | 对话中即时抽象 + reviewer | **场景触发时注入 inner_context** / recall / Life Engine tick 输入（E、F 类） |
| 粗日程 | 凌晨生成器 | Life Engine tick 输入 |
| 细日程 | Life Engine tick + 对话 tool call + afterthought 补漏 | Life Engine tick 输入 / **注入 inner_context** |
| reply_style | voice generator | **prompt voice_content** |
| cross-chat | 原始对话记录 | **inner_context**（按 trigger_user 过滤） |

**原则**：每条数据只在**加粗**的位置被注入，其他管道只消费不注入。

---

## 五、待讨论的产品细节

1. **场景触发的触发规则**：什么场景信号（用户/话题/群/时间）触发哪类抽象记忆的注入
2. **In-conversation abstraction 的实现机制**：tool call / 后置提取 / 混合
3. **Proactive recall 的时机**：赤尾在什么情况下自发调用 recall
4. **冲突处理**（F 操作）：新事实和旧抽象矛盾时的反应
5. **Graph 边的丰富度**：只有 abstract↔factual / 还要 factual↔factual（因果/时序）/ 还要 abstract↔abstract（层级）
6. **Reviewer 运行频率**：每日 / 每几小时 / 事件触发 / 混合
7. **Reviewer 对新鲜事实的处理延迟**：新鲜事实多快会被 reviewer 摸一次
8. **proactive 链路的 trigger_user 语义**（先 park，后续处理）：是否把 proactive_target 的发送者设为有效 trigger_user
9. **历史数据迁移**：617 条 relationship_memory_v2 + 373 条 conversation + 289 条 glimpse + 1120 条 life_state 怎么平滑过渡
10. **细日程的结构化程度**：按活动 vs 按时段；tool call 是结构化还是自然语言

---

## 六、待讨论的技术方案

产品形态确定后再讨论：

- 抽象记忆的统一存储（替代/扩展 relationship_memory_v2，支持 type + subject 字段）
- Graph 边的存储方式（独立 edges 表 / 内嵌 fact_id 列表 / 其他）
- 事实碎片淡化的数据表示（状态字段 / 内容重写留历史 / 单一当前版本）
- Recall 的实现（graph 遍历 / 语义检索 / 混合）及技术选型（pgvector / 外部向量库 / 其他）
- Reviewer 的 prompt 设计、调度方式、模型选型
- 两层 Schedule 的存储结构（粗/细分表 vs 同表打标）
- state sync 的技术实现（事件总线 / 直接调用 Life Engine 的轻量接口）
- 现有 afterthought / voice / Life Engine 管道的改造路径
- Cross-chat 去硬编码后的过滤策略
- 废弃 daily/weekly 后 schedule 的改造
