# Context Architecture Redesign — 设计文档

## 问题总结

当前 system prompt 8000+ 字符，62% 是 inner-context 噪音。核心架构问题：

1. **冗余注入**：schedule 和 conversation 碎片同时喂给 Life Engine（作为 tick 输入）和直接裸塞到 system prompt，信息重复且密度低
2. **缺乏 per-user 上下文**：没有关系记忆系统，赤尾对所有人说话一个样
3. **reply_style 纯 Redis**：核心行为状态无审计、不可追溯
4. **主 agent 工具过重**：search_group_history / check_chat_history / recall 导致 12 轮工具调用死循环，找不到时编造答案
5. **示例式行为锚点**：reply_style 的 few-shot 示例压制 per-user 差异

## 目标架构

```
离线 (T+1d):
  schedule ──→ Life Engine tick（唯一消费者，不再直接注入）
  daily dream ──→ 关系记忆提取（按人拆分）

准实时 (T+5min):
  afterthought ──→ 关系记忆更新（事件驱动）

system prompt (~3000 字):
  identity + appearance           ~650   静态
  rules + 回复习惯               ~800   静态，极简约束
  Life Engine state（扩充版）     ~400   赤尾此刻
  关系记忆(当前用户)             ~200   和这个人
  tools                          ~400   精简后
```

## 设计决策

### D1: Schedule 不直接注入

Life Engine tick 已经读 schedule 作为决策输入，schedule 的信息已融入 current_state + response_mood。直接注入是冗余的，且 1500 字散文噪音远大于信号。

### D2: Conversation 碎片不直接注入

Life Engine tick 已经读最近 5 条碎片（每条截取 100 字）。3000 字裸塞的碎片大部分跟当前对话者无关。

### D3: Daily dream 不直接注入

日记是全天所有人的混合叙事，无法按人提取。应该在离线 pipeline 中拆分成 per-user 关系记忆。

### D4: Life Engine state 需要扩充

去掉 schedule + 碎片 + daily dream 后，Life Engine state 是唯一的状态来源。当前 ~200 字可能不够。扩充方向：
- current_state 允许更长（当前 76-162 字，可以到 200-300 字）
- 增加 `recent_context` 字段：最近一次有意义的互动摘要

### D5: 搜索工具从主 agent 移除

主 agent 不应该能搜索历史。相关上下文应该预注入。search_group_history / check_chat_history / recall 全部移除。deep_research 的 BASE_TOOLS 也要同步精简。

### D6: reply_style 落库 + 维度修正

- 从 Redis 迁到 DB（append-only 审计）
- 维度从 per-chat × per-persona 改为 per-persona only
- 去掉 chat_id 维度（赤尾的状态不因群而异）

### D7: 关系记忆系统（新建）

- per-user × per-persona 的自然语言记忆
- 存 DB（append-only）
- 更新频率：T+5min（复用 afterthought 触发）+ T+1d（daily dream 提取）
- 注入时只取当前对话者的最新记忆

## 分期

### Phase 1: 瘦身 + 落库（低风险，立即可做）

1. reply_style 从 Redis 迁到 DB
2. 移除 schedule 直接注入
3. 移除 conversation 碎片直接注入
4. 移除 daily dream 直接注入
5. 移除主 agent 搜索工具
6. 添加工具调用次数限制

### Phase 2: 扩充 + 新建（需要更多设计）

7. Life Engine state 扩充
8. 关系记忆系统
9. reply_style 从示例式改为内心独白式
10. afterthought 碎片格式改造（聚焦 per-user 提取）
