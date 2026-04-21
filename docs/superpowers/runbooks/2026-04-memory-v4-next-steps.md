# Memory v4 Cutover 下一步行动文档

**日期**：2026-04-21
**当前 prod 版本**：agent-service / arq-worker / vectorize-worker = 1.0.0.292
**当前 main**：PR #188 已 merge，PR #190 revert 了 PR #189

---

## 当前状态

### 生产环境实际行为

- 新对话走 Memory v4：afterthought / glimpse 写入 `fragment` 表（source=afterthought / glimpse），enqueue 后 vectorize-worker 写 Qdrant
- `build_inner_context` 用 Plan C 的 7 个 section 组装（`self_abstracts` / `user_abstracts` / `active_notes` / `recall_index` / `short_term_fragments` / `schedule` / `cross_chat`），空 section 自动跳过
- Life Engine tick 使用 `commit_life_state` tool + `life_engine_tick` v14（包含 §9.5 硬校验）
- `update_schedule` 触发 `sync_life_state_after_schedule` arq job，调 `state_only_refresh`
- 轻档 reviewer（每 30min 白天 / 每 1h 夜间）/ 重档 reviewer（03:00 每日）cron 已注册，**轻档 reviewer 在 prod 首次真实跑通**（2026-04-21 CST 11:30，产出 2 条 abstract_memory：subject=self / subject=智鸿）
- Langfuse 5 个 prompt production label 已切：`life_engine_tick` v14 / `life_engine_state_refresh` v1 / `memory_reviewer_light` v1 / `memory_reviewer_heavy` v1 / `afterthought_conversation` v5
- voice.py / engine.py 读路径已切 v4 fragment（F-0a 完成）

### 数据层状态

- **`fragment` 表**：afterthought / glimpse 新产出持续入表；**过去 7 天 232 条历史碎片已通过 ops-db Mutation #125 迁入**（id 前缀 `f_mig_`，source=afterthought），voice/engine 的 context 里今天之前有内容
- **`abstract_memory` 表**：**159 条历史印象已通过 ops-db Mutation #126 迁入**（id 前缀 `a_mig_`，created_by=migration，subject=`user:{user_id}`），user_abstracts section 立即可用；reviewer 新产出的 abstract 会正常累积
- **`experience_fragment` / `relationship_memory_v2` 旧表**：`relationships.py` 还在写入 `relationship_memory_v2`（死写入，待 P2 清理）；旧表保留直到 P3 drop
- **Qdrant**：`memory_fragment` / `memory_abstract` collection 有新增 afterthought/glimpse/reviewer 产出的 point；**迁移数据不在 Qdrant**（voice/engine/user_abstracts/self_abstracts 全走 SQL，recall 暂不命中迁移数据，是已接受代价）

### 泳道验证覆盖面（泳道 mem-v4 已关停）

✅ 已在 prod 真实验证：
- Plan C inner_context 的 section 渲染（走真实流量）
- afterthought → PG fragment → MQ → vectorize-worker → Qdrant 全链路
- Plan D commit_life_state §9.5 校验 + state_sync
- **轻档 reviewer 在 prod 首跑通过**（2026-04-21 CST 11:30，8.7s / 9 obs，LLM 真实调 `commit_abstract_memory` 写入 2 条 abstract）
- Langfuse trace 可见：`memory-reviewer-light` trace（04-21 03:30 UTC）

❌ 仍未验证（等 prod 真实触发）：
- 重档 reviewer 03:00 触发（等今晚）
- `recall` 带 query 真实 qdrant 搜索
- `write_note` / `resolve_note` 真实调用
- `detect_conflict` 真实触发
- `update_schedule` + 后续 state_sync 全链路（泳道只验过一次手动的）

---

## 下一步行动清单（按优先级）

### P0 — 观察现状是否稳定（不做任何变更）

每日过一遍，至少 2–3 天：

- [ ] `make logs APP=agent-service KEYWORD=error SINCE=24h | head` — 无新 traceback
- [ ] `make logs APP=arq-worker KEYWORD="reviewer\|state_sync\|migrate" SINCE=24h` — 观察 reviewer 跑起来的行为
- [ ] `make logs APP=vectorize-worker KEYWORD="ok\|failed" SINCE=24h` — 成功率
- [ ] Langfuse 采样 5 条 trace 看 inner_context 结构 + tool call 成功率
- [ ] `/ops-db @chiwei` 每日 count 趋势：
  ```sql
  SELECT
    (SELECT count(*) FROM fragment WHERE created_at > now() - interval '1 day') AS fragments_today,
    (SELECT count(*) FROM abstract_memory WHERE created_at > now() - interval '1 day') AS abstracts_today,
    (SELECT count(*) FROM notes) AS notes_total,
    (SELECT count(*) FROM schedule_revision) AS revisions_total,
    (SELECT count(*) FROM life_engine_state WHERE created_at > now() - interval '1 day') AS states_today;
  ```

观察重点：
- 轻档 reviewer 跑的时候有没有 error
- 03:00 重档 reviewer 跑完后 `abstract_memory` 是不是开始有数据（reviewer 会从 fragment 里抽象）
- Langfuse trace 里有没有 LLM 调 `commit_abstract_memory` / `recall` 的真实记录
- 是否有没见过的 error（prod 真实流量暴露的第 N 个 bug）

**如果第 1 天出现新 bug** → 停，评估是否回滚（见 P3）。
**如果稳定 2–3 天** → 进 P1。

### P1 — 补迁移数据 ✅ 已完成（2026-04-21）

通过 `/ops-db submit` 一次性 SQL 完成。未新增任何代码、未加反代、未 kubectl exec、未向量化（voice / engine / self_abstracts / user_abstracts 读路径都走 SQL，无需 Qdrant）。

**Mutation #125**（fragment 迁移，232 条）：
```sql
INSERT INTO fragment (id, persona_id, content, source, chat_id, clarity, created_at, last_touched_at)
SELECT 'f_mig_' || id::text, persona_id::text, content, 'afterthought',
       source_chat_id::text, 'clear', COALESCE(created_at, now()), COALESCE(created_at, now())
FROM experience_fragment
WHERE grain = 'conversation' AND created_at > now() - interval '7 days'
ON CONFLICT (id) DO NOTHING;
```
验证：akao 126 / ayana 68 / chinagi 38 = 232，全部 content_match=true。

**Mutation #126**（relationship 最小迁移，159 条，每 (persona, user) 取 MAX(version)，impression 原文即 abstract content）：
```sql
INSERT INTO abstract_memory (id, persona_id, subject, content, created_by, created_at, last_touched_at)
SELECT 'a_mig_' || md5(r.persona_id || ':' || r.user_id),
       r.persona_id::text, 'user:' || r.user_id::text, r.impression,
       'migration', COALESCE(r.created_at, now()), COALESCE(r.created_at, now())
FROM relationship_memory_v2 r
WHERE r.version = (SELECT MAX(version) FROM relationship_memory_v2 r2
                   WHERE r2.persona_id = r.persona_id AND r2.user_id = r.user_id)
ON CONFLICT (id) DO NOTHING;
```
验证：akao 77 / chinagi 45 / ayana 37 = 159，subject 全为 `user:{user_id}` 格式。

**不做的事**（最小迁移取舍）：
- 不拆 `core_facts` 成 fragment / 不建 `supports` edge（没有向量化，堆过去也是死数据；heavy reviewer 会从新 fragment 增量产出真正可搜的 abstract）
- 不调 LLM 重写 impression（impression 本就是赤尾原话，avg 105 / p95 179 字符完全合理）
- 不补 recall 命中（recall 搜这批数据会 miss，是可接受代价）

**幂等清理**（如需回滚迁移数据）：
```sql
DELETE FROM fragment WHERE id LIKE 'f_mig_%';
DELETE FROM abstract_memory WHERE created_by = 'migration';
```

### P2 — 废弃 relationships.py 死写入

**前提**：P1 迁移完成 + 一周真实流量观察 `abstract_memory` 增长正常。

`app/memory/relationships.py` 仍在写 `relationship_memory_v2`，但 v2 表的读路径已经切到 v4。这是死写入，浪费。

新建 PR：
1. 停掉 `extract_relationship_updates` 在 afterthought 的调用（`app/memory/afterthought.py` L132-153）
2. 停掉 `extract_relationship_updates` 在 `/admin/rebuild-relationship-memory` 的调用（`app/api/routes.py`）
3. 或者：整个 `app/memory/relationships.py` + `insert_relationship_memory` / `find_relationship_memories_batch` 删除

优先选**整个删掉**而不是停调用 — 留着代码但断逻辑会产生"这个文件干啥的"的困惑。但删之前要确认 `/admin/rebuild-relationship-memory` 没人在用。

### P3 — 旧表 drop

**前提**：P2 完成 + 迁移数据连续 1 周被 recall 正常使用。

```sql
DROP TABLE experience_fragment;
DROP TABLE relationship_memory_v2;
```

同步删：
- `app/data/models.py`：`ExperienceFragment`、`RelationshipMemoryV2` ORM class
- `app/data/queries.py`：`find_today_fragments`、`find_fragments_in_date_range`、`insert_experience_fragment`、`insert_relationship_memory`、`find_relationship_memories_batch`（检查 grep 无残留 caller 再删）
- Qdrant 如有旧 collection（`experience_fragment_*` / `relationship_memory_v2_*`）一并 drop

### 随时可做的 cleanup（与以上顺序无关）

- **`cron_generate_dreams` 重命名**：函数体已经是 `run_heavy_review`，名字还叫 dreams。改名 + 同步 `arq_settings.py`。一个独立 PR。
- **`_make_commit_tool` 去重**：`app/life/engine.py` + `app/life/state_sync.py` 重复 ~77 LOC，抽到 `app/life/_commit_tool.py`。
- **`CST` 常量集中**：`light.py` / `heavy.py` / `cross_chat.py` 等多处重复定义，统一用 `app/life/_date_utils.CST`。

---

## 回滚条件（如果 P0 观察出问题）

### 什么情况下回滚 PR #188

- prod 出现 Memory v4 相关的 traceback 导致对话功能受损（赤尾无法回复、回复乱码）
- reviewer 开始跑之后写入明显错误数据（compared to expected behavior）
- 用户对体验下降不能接受

### 回滚步骤

1. **Langfuse label 退回**：
   - `life_engine_tick` production → v12（旧版）
   - `afterthought_conversation` production → v4
   - `life_engine_state_refresh` / `memory_reviewer_light` / `memory_reviewer_heavy` 的 production label 无所谓（旧代码不调）
2. **prod 三服务回退**：
   ```bash
   make release APP=agent-service LANE=prod VERSION=1.0.0.285 GIT_REF=main
   make release APP=arq-worker LANE=prod VERSION=1.0.0.285 GIT_REF=main
   make release APP=vectorize-worker LANE=prod VERSION=1.0.0.285 GIT_REF=main
   ```
3. **main 上 revert PR #188**（通过 revert PR，走 ship 流程）
4. **v4 表孤岛数据 DELETE**（按 `created_at > 今天 cutover 时间点` 筛）

回滚后的重新上线计划：
- 先在新泳道（比如 `mem-v4-v2`）跑**完整的 Plan E 覆盖验证**：
  - 手动 trigger 轻档 reviewer 一次（加 admin endpoint）
  - 模拟 03:00 触发重档 reviewer
  - 故意让赤尾调 `commit_abstract_memory` / `recall` / `write_note` / `resolve_note`
  - 故意触发 conflict detection（写一条冲突抽象）
  - 多次 `update_schedule` 观察 state_sync 稳定性
- 所有路径都有证据跑通后，再一次性 cutover

---

## 本文档的使用

- 任何推进 Memory v4 cutover 的动作，**先读本文档判断当前处于哪个 Phase**。
- 每个 Phase 的"前提"必须满足才能进入下一 Phase。
- 回滚条件是**观察**得出的判断，不是假设 — 没有具体证据就不回滚。
