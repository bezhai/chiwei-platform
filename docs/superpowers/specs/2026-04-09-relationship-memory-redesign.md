# 关系记忆重设计

## 问题

relationship_memory 对所有人的记忆全是极端负面的（8 人 0 正面），导致赤尾/千凪亲和度极低。

**根因：**
1. `relationship_extract` prompt 缺 `persona_lite`，用第三方系统视角提取，字面理解互怼为冲突
2. 旧记忆作为输入 → LLM 在负面基调上加码 → 负面飞轮
3. afterthought debounce 300s/15 条，只有高强度互怼场景触发，低频正面互动被忽略

## 方案总览

| 项 | 内容 |
|----|------|
| P0-1 | DB schema 拆分 `core_facts` + `impression` 两个字段 |
| P0-2 | 修复 `relationship_extract` prompt：加 persona_lite、第一人称视角、打破负面飞轮 |
| P0-3 | rebuild 端点 + 本地脚本批量回溯重建 |
| P1（不在本次 scope） | 补路径 B（daily dream 提取慢热关系变化） |

## 设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| core_facts 与 impression 的存储 | 两个独立 DB 字段 | 可独立更新、查询灵活 |
| 更新策略 | append-only + version 自增 | 完整审计历史，可追溯每一次变更 |
| 用进废退机制 | 纯靠 prompt 自然衰减 | 不加代码逻辑，先解决内容质量 |
| 批量回溯数据源 | conversation_messages 原始消息 | experience_fragment 可能继承旧 prompt 偏见 |
| 回溯分批策略 | 按时间窗口渐进式提取 | 模拟逐渐认识一个人的过程，不受 context window 限制 |
| 回溯执行方式 | agent-service 暴露 API 端点，本地脚本循环调 | 端点可复用，脚本灵活可随时改 |
| 清空历史记忆 | 提交 DDL（通过 ops-db） | 不加代码端点 |
| inner_context 注入范围 | 保持现状（只注入 trigger_user） | 本次聚焦离线提取链路 |

---

## Part 1: DB Schema 变更

### DDL

```sql
ALTER TABLE relationship_memory
  ALTER COLUMN memory_text SET DEFAULT '',
  ADD COLUMN version INT NOT NULL DEFAULT 1,
  ADD COLUMN core_facts TEXT NOT NULL DEFAULT '',
  ADD COLUMN impression TEXT NOT NULL DEFAULT '';
```

- `memory_text` 加 `DEFAULT ''`：停止写入后新 INSERT 不会因 NOT NULL 失败

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | INT | 服务端自增，每次写入查 `(persona_id, user_id)` 当前最大 version + 1 |
| `core_facts` | TEXT | 事实性知识（昵称映射、城市、习惯、工作等），更新频率低 |
| `impression` | TEXT | 情感印象（对这个人的感觉、关系亲疏），随互动变化 |

### 过渡兼容

- `memory_text` 保留不删，历史数据仍可查
- 新记录写 `core_facts` + `impression`，不再写 `memory_text`
- 读取时优先读新字段；若两个新字段都为空，fallback 读 `memory_text`

### ORM 改动

`app/orm/memory_models.py` — `RelationshipMemory` 类加三个字段：

```python
version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
core_facts: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
impression: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
```

### CRUD 改动

`app/orm/memory_crud.py`：

- `save_relationship_memory()`：签名改为接收 `core_facts` + `impression`，写入前查当前最大 version + 1
- `get_latest_relationship_memory()`：返回 `(core_facts, impression)` 元组；若新字段为空 fallback `memory_text`
- `get_relationship_memories_for_users()`：同上，返回 `{user_id: (core_facts, impression)}`

---

## Part 2: relationship_extract prompt 修复

### 当前问题

- prompt 以"关系记忆管理系统"的第三方视角提取
- 没有 `persona_lite`，不知道角色性格
- 互怼被字面理解为冲突
- 旧记忆输入形成负面飞轮

### 改动

#### 函数签名

`app/services/relationship_memory.py` — `extract_relationship_updates()`：

- 新增查询 persona 拿 `persona_lite` 和 `persona_name`（同 afterthought 的做法）
- prompt 编译变量从 `{messages, current_memories}` 改为 `{persona_name, persona_lite, messages, current_core_facts, current_impression}`

#### prompt 调性

Langfuse `relationship_extract` v2：

- 角色：~~"你是关系记忆管理系统"~~ → "你是 {{persona_name}}，在回忆刚才和大家聊天的感觉"
- 注入 `{{persona_lite}}` 让 LLM 以角色视角解读对话
- 打破负面飞轮："如果旧印象和实际互动对不上，按你真实的感觉重写，不要在旧印象上加码"
- 互动解读："互怼是亲近的表现，经常来找你的人心里知道是在闹着玩"
- 用进废退："如果最近没什么互动，印象可以变模糊、淡化"
- 输出拆为 `core_facts` + `impression` 两段

#### 输出格式

```json
[
  {
    "user_id": "xxx",
    "user_name": "名字",
    "core_facts": "事实性知识...",
    "impression": "情感印象..."
  }
]
```

去掉 `action` 字段——每个出现在输出里的 user 都是 UPDATE。

---

## Part 3: rebuild 端点

### 端点

`POST /admin/rebuild-relationship-memory`

### 输入

```json
{
  "persona_ids": ["chiwei", "ayane", "chichi"],
  "chat_ids": ["oc_xxx"],
  "start_time": "2026-01-01T00:00:00Z",
  "end_time": "2026-04-09T00:00:00Z"
}
```

### 内部逻辑

1. 查 `conversation_messages` where `chat_id in chat_ids` and `created_at in [start_time, end_time]`，按 `user_id` 分组
2. 每个 `(persona_id, user_id)` 组合：
   - 按时间窗口分批（每 50 条消息一批）
   - 第一批：无已有记忆，从零开始提取
   - 后续批次：把上一批结果作为 `current_core_facts` + `current_impression` 传入
   - 每批调用核心提取函数，结果存库（version 自增）
3. 最终每个 user 的最新一条记录就是渐进收敛的结果

### 返回

```json
{
  "results": [
    {
      "persona_id": "chiwei",
      "user_id": "xxx",
      "user_name": "crgg",
      "batches": 5,
      "final_version": 5,
      "core_facts": "最终事实...",
      "impression": "最终印象..."
    }
  ]
}
```

### 注意事项

- 端点耗时较长（每个 user 多轮 LLM 调用），需要较长超时
- afterthought 日常提取复用同一个核心提取函数

---

## Part 4: 代码改动范围

| 文件 | 改动 |
|------|------|
| `app/orm/memory_models.py` | RelationshipMemory 加 `version`, `core_facts`, `impression` 三个字段 |
| `app/orm/memory_crud.py` | `save_relationship_memory` 写新字段 + 自增 version；`get_latest_*` 读新字段（fallback memory_text） |
| `app/services/relationship_memory.py` | 注入 `persona_lite`，解析新输出格式（core_facts + impression），调用新 save 签名 |
| `app/services/memory_context.py` | 注入格式改为 `[事实]` + `[印象]` 分段 |
| `app/services/afterthought.py` | 不改（已传 persona_id） |
| 新文件或已有 admin 路由 | `POST /admin/rebuild-relationship-memory` 端点 |
| Langfuse | `relationship_extract` prompt v2 |
| DDL（通过 ops-db） | `ALTER TABLE` 加三列 |

### 不改的

- afterthought 的 debounce 逻辑
- inner_context 注入范围（只注入 trigger_user）
- daily dream（P1 后续做）

---

## 执行顺序

1. 提交 DDL 加列
2. 改 ORM model + CRUD
3. 改 relationship_extract prompt（Langfuse）+ 提取函数
4. 改 memory_context 注入格式
5. 部署 agent-service
6. 通过 ops-db 清空历史负面记忆
7. 加 rebuild 端点，部署
8. 本地脚本调端点批量回溯
