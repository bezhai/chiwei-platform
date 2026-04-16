# Memory System v4 设计大纲

> 状态：**草案（待讨论）**
> 分支：`feat/context-decline`

## 问题

### 1. Context 注入信息重复

同一事件（如"浩任艾特赤尾三次"）被 3 条管道加工后**同时注入 prompt**：

| 管道 | 产出 | 注入位置 |
|------|------|----------|
| afterthought | conversation fragment（~500 字内心独白） | `inner_context` |
| relationship extraction | core_facts + impression（~200 字） | `inner_context` |
| Life Engine | current_state + mood（~100 字） | `inner_context` |
| voice generator | reply_style（~300 字） | `voice_content` |

四者信息高度重叠，占用 prompt ~1100 tokens 却只传达 ~200 tokens 的增量信息。

### 2. Fragment 角色混乱

conversation fragment 同时充当：
- **原材料**：喂给 Life Engine / voice / daily dream
- **成品**：直接注入 prompt

且因 afterthought 每 5 分钟 debounce + 2h 滑动窗口，同一事件被重复生成 10+ 次。

### 3. 长期记忆遗忘过度

压缩链路信息损失严重：

```
373 条 conversation（~210k 字）
  → 25 条 daily（~28k 字）    压缩率 7.5:1
  → 3 条 weekly（~2.5k 字）   压缩率 84:1
```

具体事件（谁、什么时候、什么语境）在 daily 阶段已大量丢失。

### 4. Recall 形同虚设

- 搜索方式：PG full-text search（`simple` 字典）= 纯关键字匹配
- "上次聊新番" 搜不到 "讨论了鬼灭之刃"
- relationship_memory_v2 不在搜索范围内
- 实际几乎不被模型触发

### 5. Cross-chat 硬编码

`CROSS_CHAT_GROUP_IDS` 写死单个群 ID，其他群的跨群互动完全被忽略。

---

## 数据现状（2026-04-16 快照）

| 数据源 | 记录数 | 平均长度 | 数据质量 |
|--------|--------|----------|----------|
| conversation fragment | 373 | 564 字 | 内容丰富但高度重复（同一事件 10+ 变体） |
| glimpse fragment | 289 | 113 字 | 碎片化观察，信息密度低 |
| daily fragment | 25 | 1106 字 | 质量可以，但只覆盖 ~60% 的天数 |
| weekly fragment | 3 | 827 字 | 只生成过一次 |
| relationship_memory_v2 | 617 | facts 89 字 / impression 134 字 | **质量最高**，具体行为模式+情感层 |
| life_engine_state | 1120 | ~100 字 | 状态重复率高（同描述复用 35 次） |

---

## 方案：三层记忆架构

### 总览

```
Layer 1: 即时上下文（每次对话注入 prompt）
  ├─ Scene
  ├─ Life State
  ├─ Relationship Memory
  └─ Cross-chat

Layer 2: 情景记忆（recall 按需检索）
  ├─ conversation fragments（去重存储 + embedding）
  ├─ relationship_memory 历史版本
  └─ 语义搜索（pgvector）

Layer 3: 沉淀记忆（后台管道消费，不直接注入也不直接检索）
  ├─ daily / weekly dream
  └─ 喂给 life engine / voice / schedule
```

**核心原则**：每条数据只在一个层被消费，不跨层重复注入。

### Layer 1：即时上下文

每次对话都注入 prompt 的信息。目标：**精准、不冗余、高信息密度。**

| Section | 内容 | 改动 |
|---------|------|------|
| Scene | 场景描述（群聊/私聊/主动扫描） | 保持不变 |
| Life State | 此刻状态 + 心情 | 保持不变 |
| Relationship Memory | trigger user 的 core_facts + impression | 保持不变 |
| Cross-chat | 与 trigger user 在其他群的最近互动 | 去掉硬编码群 ID，改为查所有群 |
| ~~Fragments~~ | ~~最近 2 条 conversation fragment~~ | **移除** |
| ~~Recall Hint~~ | ~~固定提示文案~~ | **移除**（recall 工具描述已足够） |

**预期效果**：inner_context 从 ~700 tokens 降至 ~400 tokens，信息密度提升。

### Layer 2：情景记忆

recall 工具按需检索的记忆池。目标：**语义可达、覆盖面广。**

#### 2.1 存储

在 `experience_fragment` 表增加 embedding 列（pgvector `vector(1536)` 或适配所用 embedding 模型的维度）。

写入时：
1. afterthought 生成 fragment 后，计算 embedding
2. 与最近一条同 `(persona_id, source_chat_id)` 的 fragment 做余弦相似度检查
3. 相似度 > 阈值（如 0.92）则跳过写入（去重）
4. 否则写入 fragment + embedding

#### 2.2 检索

recall 工具改造：

```python
async def recall(what: str) -> str:
    # 1. 对 what 计算 embedding
    # 2. 在 experience_fragment 做 vector similarity search (top 5)
    # 3. 在 relationship_memory_v2 做 vector similarity search (top 3)
    #    （需要给 relationship_memory_v2 也加 embedding 列）
    # 4. 合并结果，按相关度排序返回
```

#### 2.3 搜索范围

| 数据源 | 搜索内容 | 用途 |
|--------|----------|------|
| experience_fragment (conversation) | 过去的对话经历 | "上次聊新番是什么时候" |
| experience_fragment (daily) | 某天的总结 | "上周三发生了什么" |
| relationship_memory_v2 | 对某人的印象变化 | "我以前觉得A怎么样" |

### Layer 3：沉淀记忆

后台管道消费的产物。目标：**给其他管道提供压缩后的输入，不直接面向用户。**

- **daily dream**：读当天 conversation + glimpse fragments → 生成日记
- **weekly dream**：读最近 daily fragments → 生成周记
- **Life Engine**：读最近 conversation fragments → 更新状态
- **voice generator**：读 life state + fragments → 生成语气

这一层不变，但因为 Layer 2 的去重，fragments 的输入质量会提升。

---

## 改动清单

### P0：Context 注入优化

1. `build_inner_context()` 移除 fragment 注入（删除 `# === Recent fragments ===` 段）
2. `build_inner_context()` 移除 recall hint 固定文案
3. `cross_chat.py` 去掉 `CROSS_CHAT_GROUP_IDS` 硬编码，改为查 trigger_user 参与的所有群

### P1：Fragment 去重

4. afterthought 写入前，与最近一条 fragment 做文本相似度检查（先用简单方案：Jaccard 或编辑距离，不依赖 embedding）
5. 相似度超阈值则跳过写入

### P2：语义召回

6. 数据库：`experience_fragment` 表加 `embedding vector(N)` 列（pgvector）
7. 数据库：`relationship_memory_v2` 表加 `embedding vector(N)` 列
8. afterthought 写入 fragment 时，异步计算并存储 embedding
9. relationship extraction 写入时，异步计算并存储 embedding
10. recall 工具：从 PG FTS 改为 vector similarity search，搜索范围扩展到 relationship_memory_v2
11. 历史数据回填 embedding（一次性任务）

### P3：数据质量

12. 排查 daily dream cron 为什么部分天数没生成
13. 排查 weekly dream 为什么只跑过一次

---

## 待讨论

- [ ] embedding 模型选择（OpenAI ada-002 / 本地模型 / 其他）
- [ ] pgvector 是否已安装，或需要替代方案
- [ ] fragment 去重的相似度阈值怎么定
- [ ] cross-chat 去掉硬编码后，是否需要加群组白名单（dynamic config）
- [ ] relationship_memory 历史版本是否全部纳入 recall，还是只保留最近 N 个版本
- [ ] recall 返回结果的格式优化（当前只截断 300 字）
- [ ] 是否需要给 recall 增加时间范围过滤参数

---

## 产出/消费矩阵（目标态）

| 数据 | 产出者 | 消费者 |
|------|--------|--------|
| conversation fragment | afterthought | Life Engine, voice, daily dream, **recall (Layer 2)** |
| relationship_memory | relationship extraction | **inner_context (Layer 1)**, **recall (Layer 2)** |
| life_engine_state | Life Engine | **inner_context (Layer 1)**, voice |
| daily fragment | daily dream | weekly dream, schedule |
| weekly fragment | weekly dream | （当前无消费者，待定） |
| reply_style | voice generator | **prompt voice_content** |
| cross-chat messages | 原始对话记录 | **inner_context (Layer 1)** |

每条数据只在标粗的位置被注入/检索，不跨层重复。
