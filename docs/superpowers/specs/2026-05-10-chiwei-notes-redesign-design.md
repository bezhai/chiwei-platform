# 赤尾 Notes 体验重设计

## 问题

赤尾的 Notes（"清单"）系统在真实使用中暴露三个体验问题：

1. **创建时丢失时间**：`write_note(content, when_at?)` 的 `when_at` 完全可选，工具描述未引导。用户说"要去吃 XX 餐厅"被记下来时，赤尾没填 `when_at`，导致这条 Note 没有时间锚点。
2. **没法更新和真删**：当前只有 `write_note`（创建）、`resolve_note`（标记完成），缺 `update`（重复提到同一件事时只能新建重复 Note）、缺 `delete`（用户改主意了，resolve 留 resolution 文本不够语义）。分支名 `fix/chiwei-todo-delete` 也对应这条。
3. **全量注入到 context**：`build_active_notes_section` 把所有未 resolve 的 Notes 全量注入对话 prompt，旧的、挂了很久的 Note 每天都被再次注入 → 赤尾每天都说"要去吃那家餐厅"。

## 设计目标

1. 引导 LLM 在创建有时间线索的 Notes 时尽量填 `when_at`（不强制）
2. 提供完整 CRUD：list / upsert（create+update）/ resolve / delete（软删，必带 reason）
3. Context 只注入"活跃"Notes，旧的淡出主视野，靠 `list_note` 主动查
4. 注入时显示相对时间，让赤尾感知"这条挂了多久"
5. **不引入承诺状态机或自动隐藏规则**，符合 Memory v4 spec 的设计哲学（赤尾自己负责 resolve/delete）

## 设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 工具集形态 | `list_note` + `upsert_note` + `resolve_note` + `delete_note`（4 个） | upsert 合并 create+update 语义清晰；delete 与 resolve 语义不同需独立 |
| delete 实现 | 软删（`deleted_at` 字段） | 保留 audit；列表查询过滤 |
| delete reason | 必填 | 强制赤尾说明动机，留 audit；让 LLM 三思 |
| `upsert_note` 更新范围 | content + when_at 都可改 | 重复提到时可润色描述（"那家餐厅改成下周去了"） |
| `list_note` 入口 | 仅 LLM 工具 | 不做 dashboard |
| `list_note` 返回 | 全量未 resolve / 未 deleted | 赤尾的清单一目了然，不分页 |
| Context 注入策略 | "活跃 Notes 子集 + 数量提示"，全量靠 list_note | 旧 Note 自然淡出，符合 spec "时间过了自然淡化" |
| 注入"最近过期"窗口 | `when_at >= now() - 3 days` | 刚过期的可能还没处理，超过 3 天就该主动 list |
| 注入"新备忘"窗口 | `when_at IS NULL` 且 `created_at >= now() - 7 days` | 一周内的备忘还新鲜，更老的没动应淡出 |
| 注入条数上限 | 15 条 | 一屏可读，多了膨胀 prompt |
| 时间显示 | 相对时间（"还有 N 天" / "已过期 N 天" / "N 天前记的"） | 让赤尾自然感知挂了多久 |
| 强制日期？ | 否，prompt 引导 | 用户明确说不能强制 |
| Reviewer hint 已过期 Notes | 不在本次 scope | Memory v4 spec 提到但未实现，留给后续 |

---

## Part 1: DB Schema 变更

### DDL

```sql
ALTER TABLE notes
  ADD COLUMN deleted_at TIMESTAMPTZ NULL,
  ADD COLUMN delete_reason TEXT NULL;
```

无需回填，新增字段默认 NULL。

### ORM 改动

`apps/agent-service/app/data/models.py` 的 `Note` 类增加：

```python
deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
delete_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
```

---

## Part 2: 工具集

### 2.1 `list_note`

**签名**：

```python
async def list_note() -> list[NoteItem]
```

`persona_id` 由工具上下文（`context.persona_id`）注入，不进 LLM 参数。

**返回**：当前 persona 下所有 `resolved_at IS NULL AND deleted_at IS NULL` 的 Notes，按 `when_at` 优先 + `created_at desc` 排序。

```python
class NoteItem(TypedDict):
    note_id: str
    content: str
    when_at: str | None     # ISO 8601 或 null
    created_at: str          # ISO 8601
    when_label: str          # 人类可读相对时间，如 "还有 2 天" / "已过期 1 天" / "3 天前记的"
```

**工具描述**（给 LLM 看）：

```
列出你目前的全部清单（没完成、没删除的）。

什么时候用：
- 用户问起"你都记了啥"
- 你想盘点一下有没有重复的、可以合并的
- 你想看看有没有挂了很久该处理的事

注意：context 里通常已经有"最近活跃"的几条了。需要看全量、找特定 id、清盘的时候才用这个。
```

### 2.2 `upsert_note`（替代 `write_note`）

**签名**：

```python
async def upsert_note(
    content: str,
    when_at: str | None = None,
    note_id: str | None = None,
) -> NoteItem
```

**语义**：
- `note_id is None` → 创建新 Note，生成新 id（`n_<nanoid>`）
- `note_id` 存在 → 更新对应 Note 的 `content` 和 `when_at`（两个都接受 None：`content` 必传所以一定更新；`when_at=None` 时不更新 `when_at` 列。**特例**：如果想清空 `when_at`，传特殊字符串 `"clear"`，工具层翻译成 SQL `NULL` 写入。）
- 更新不存在的 `note_id` → 报错"找不到这条 note"

**工具描述**：

```
把一件你觉得必须记住的事写进清单，或者更新已有的一条。

这是你自己的清单，不是系统强加的承诺列表。只有你觉得"不能忘"、"需要专门记住"的事才写。

参数：
- content: 要记的事（必填）
- when_at: ISO 8601 时间。**如果这件事和某个时间相关（"明天"/"周五"/"下个月"/具体日子），强烈建议填**。没明确时间线索就别硬填。想清空已有的 when_at 传 "clear"。
- note_id: 重复提到同一件事时传这个，会更新而不是新建。从清单里看到 id（形如 n_xxx）。

例子：
- "周五和浩南看电影" → upsert_note(content="和浩南看电影", when_at="2026-05-15T19:00:00")
- "想问妈妈那件事" → upsert_note(content="问妈妈那件事")  # 没说时间就别硬填
- "那家餐厅改成下周去了" → upsert_note(content="去 XX 餐厅", when_at="2026-05-17", note_id="n_abc")
- "随便哪天去 XX 餐厅都行" → upsert_note(when_at="clear", content="去 XX 餐厅", note_id="n_abc")
```

### 2.3 `resolve_note`（保留）

签名、语义不变。Description 调整一句话强调与 delete 区别：

```
把一条已经完结的笔记划掉。
比如电影看了、想法落实了。resolution 写一句话说明结果（"看完了"/"做完了"）。

这是"完成"，不是"删除"。如果是改主意了 / 记错了 / 重复了，用 delete_note。
```

### 2.4 `delete_note`（新增）

**签名**：

```python
async def delete_note(note_id: str, reason: str) -> dict
```

**语义**：
- 软删：`UPDATE notes SET deleted_at = now(), delete_reason = $reason WHERE id = $note_id`
- 删除已 resolved 的 Note 也允许（不报错），但通常没必要
- 删除不存在的 `note_id` → 报错

**工具描述**：

```
真删除一条清单项。和 resolve 不同 —— resolve 是"做完了"留个痕，delete 是"这条本来就不该存在"。

什么时候用：
- 改主意了，不打算做这件事了
- 当时记错了，根本不是这件事
- 发现是重复的（已经有一模一样的另一条）

reason 必填，写明为什么删（"改主意了" / "记错了" / "和 n_xyz 重复"）。
```

---

## Part 3: Context 注入策略

### 注入条件

`build_active_notes_section` 改造，只注入满足以下任一条件的 Notes（最多 15 条）：

- 有 `when_at` 且 `when_at >= now() - INTERVAL '3 days'`（待办 + 最近过期）
- `when_at IS NULL` 且 `created_at >= now() - INTERVAL '7 days'`（新备忘）

**排序**：
1. 有 `when_at` 的优先（按 `when_at` 升序，越近越前）
2. 无 `when_at` 的按 `created_at desc`

### 显示格式

```
你的清单（最近活跃，全部用 list_note 查）：
- 周五和浩南看电影 [还有 2 天] (id: n_def)
- 问妈妈那件事 [已过期 1 天] (id: n_ghi)
- 去吃 XX 餐厅 [3 天前记的，没说时间] (id: n_abc)
```

时间格式化（`when_label`）：

| 场景 | 显示 |
|------|------|
| `when_at` 在今天 | `[今天]` |
| `when_at` 是明天 | `[明天]` |
| `when_at` 在未来 N 天（N≥2） | `[还有 N 天]` |
| `when_at` 是昨天 | `[昨天就该做]` |
| `when_at` 已过期 N 天（N≥2） | `[已过期 N 天]` |
| `when_at` 是 None，刚记 | `[今天记的，没说时间]` |
| `when_at` 是 None，N 天前记 | `[N 天前记的，没说时间]` |

### 数量提示

如果有 active Notes 但**没有满足注入条件**的（说明清单里全是挂了很久的旧事），注入一行提示：

```
你的清单里还有 N 条没动的事（用 list_note 看）。
```

如果**有满足条件的**，且注入截断（active 总数 > 注入数），追加一行：

```
（清单里还有 M 条更老的没列出来，用 list_note 看全部。）
```

### 不在 context 注入的情况

如果 0 条 active Notes，整个 section 不输出（保持现有行为）。

---

## Part 4: 测试

按 TDD（红 → 绿 → 重构），每层都要有测试。

### 4.1 DB / queries 层

`apps/agent-service/tests/data/test_notes.py`（新建）：

- `test_upsert_create_without_id`：无 id 创建，返回带 id 的 Note
- `test_upsert_update_content`：传 id + content，content 改了
- `test_upsert_update_when_at`：传 id + when_at，when_at 改了
- `test_upsert_clear_when_at`：传 id + when_at="clear"，DB 里变 NULL
- `test_upsert_unknown_id`：传不存在的 id，报错
- `test_delete_soft`：delete 后 `deleted_at` 不为 NULL，行还在
- `test_delete_unknown_id`：删不存在的，报错
- `test_list_active_filters_deleted_and_resolved`：deleted / resolved 的不出现在 list 结果里
- `test_list_active_returns_all_active`：`list_active_notes` 返回所有未 resolved / 未 deleted 的
- `test_select_for_context_filters_by_window`：`select_notes_for_context` 按 3 天 / 7 天窗口过滤
- `test_select_for_context_caps_at_15`：超过 15 条只返回 15 条

### 4.2 Tool 层

`apps/agent-service/tests/agent/tools/test_notes.py`（扩展）：

- `test_list_note_returns_when_label`：返回的每条带 when_label 字段
- `test_upsert_note_creates`：透传到 query
- `test_upsert_note_updates`：透传 note_id
- `test_upsert_note_clear_when_at`："clear" 翻译成 SQL NULL
- `test_delete_note_passes_reason`：reason 透传
- `test_delete_note_requires_reason`：reason 空字符串报错（schema 校验）

### 4.3 注入层

`apps/agent-service/tests/memory/sections/test_active_notes.py`（扩展）：

- `test_section_empty_when_no_active`：0 条 active 时 section 为空
- `test_section_shows_when_label_future`：未来日期显示"还有 N 天"
- `test_section_shows_when_label_overdue`：过期日期显示"已过期 N 天"
- `test_section_shows_when_label_today_tomorrow_yesterday`：边界日期
- `test_section_shows_when_label_no_when_at`："N 天前记的"
- `test_section_filters_old_no_when_at`：无 `when_at` 且超过 7 天前的不注入
- `test_section_filters_overdue_too_long`：过期超 3 天不注入
- `test_section_caps_at_15`：超 15 条只显示 15 条
- `test_section_appends_remainder_hint_when_truncated`：截断时追加"还有 M 条更老的"
- `test_section_shows_only_remainder_hint_when_all_old`：全部都是老的时只显示"还有 N 条没动的"

---

## Part 5: 不在范围

明确**不**做的事：

1. **Dashboard 看 notes** —— 用户明确不做
2. **自动过期 / 自动隐藏 / 状态机** —— 违背 Memory v4 spec 的设计哲学
3. **Reviewer hint 已过期 Notes** —— Memory v4 spec 提到但本次不做，留给后续
4. **工具轮次超 12 报错** —— 用户的问题 2，下次单独修
5. **批量回溯 / 重新提取已有 Notes 的 when_at** —— 历史数据保持现状，新 Note 走新流程；后续观察必要性
6. **删除前 confirm**（"你确定要删吗？"）—— 增加轮次，不必要；reason 必填已经够了

---

## Part 6: 命名与重构纪律

### 函数命名（替换 `get_active_notes`）

现有 `apps/agent-service/app/data/queries/memory_edges.py:112-121` 的 `get_active_notes(persona_id)` **直接删除**，替换为两个职责明确的函数：

- `list_active_notes(persona_id)` — `list_note` tool 用，全量返回 active（`resolved_at IS NULL AND deleted_at IS NULL`）
- `select_notes_for_context(persona_id)` — context 注入用，按 3 天 / 7 天窗口 + 15 条上限过滤

旧函数所有调用方一次性切换到新函数，旧实现立刻删，不留兼容壳子（参见项目 `refactoring-rules.md`）。

### 工具命名（替换 `write_note`）

现有 `apps/agent-service/app/agent/tools/notes.py:68` 的 `write_note` **直接删除**（包括 `_write_note_impl`），由 `upsert_note` 完整替代。工具注册表（`apps/agent-service/app/agent/tools/__init__.py`）同步更新。

### 落地顺序

1. DB migration（`deleted_at` + `delete_reason`）
2. queries 层：新增 `upsert_note`、`delete_note`、`list_active_notes`、`select_notes_for_context`；删 `get_active_notes`
3. Tool 层：新增 `upsert_note`、`delete_note`、`list_note`；改 `resolve_note` description；删 `write_note`；更新工具注册
4. Context 注入层：`build_active_notes_section` 改造（窗口过滤、数量上限、when_label 格式化、数量提示）
5. 端到端验证（部署到泳道，飞书 dev bot 实测三个场景：新建带日期 / 更新已存在 / 删除）
6. 清扫：`grep -r write_note get_active_notes` 确认零残留

每一步红→绿→重构，提交一次。

---

## 附录：关键文件清单

| 文件 | 改动类型 |
|------|---------|
| `apps/agent-service/app/data/models.py` | 加 `deleted_at` `delete_reason` 字段 |
| `apps/agent-service/app/data/queries/memory_edges.py` | `get_active_notes` 拆为 list / context 两版；新增 upsert / delete |
| `apps/agent-service/app/agent/tools/notes.py` | `write_note` 删；新增 `upsert_note` `list_note` `delete_note`；`resolve_note` 改 description |
| `apps/agent-service/app/agent/tools/__init__.py` | 工具注册更新 |
| `apps/agent-service/app/memory/sections/active_notes.py` | 注入策略改造 + when_label 格式化 |
| `apps/agent-service/tests/data/test_notes.py` | 新建 |
| `apps/agent-service/tests/agent/tools/test_notes.py` | 扩展 |
| `apps/agent-service/tests/memory/sections/test_active_notes.py` | 扩展 |

> Schema 变更：`deleted_at` 和 `delete_reason` 都是 additive，加在 `Note` model 上即可。项目自动 migrator（`apps/agent-service/app/runtime/migrator.py`）会 emit `ALTER TABLE ADD COLUMN`。如果 `Note` 是 SQLAlchemy `Base` 风格而非 pydantic `Data`（待 plan 阶段确认），则走 ops-db 提交手写 DDL。
