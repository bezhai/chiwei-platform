# Memory v4 Cutover Runbook

一次性切换 relationship_memory_v2 + experience_fragment → v4 (fragment / abstract_memory / memory_edge / notes / schedule_revision)。Plan A–E 全部完成后执行。

---

## 前置

- [ ] Plan A–E 所有 commit 都在 main（或即将合入 main 的分支上）
- [ ] 本文档以外的所有 commit 单测全绿（`uv run pytest tests/unit/ -q`）
- [ ] 泳道实测通过（dev bot 能跑完 chat → context 注入 → tool call → 回复全链路；update_schedule 触发 state_sync；write_note 写入 note 表；commit_abstract_memory 写入 abstract_memory 表）
- [ ] 周末或低流量窗口
- [ ] 用户明确说「上」
- [ ] 当前没有长跑的后台任务（rebuild / afterthought 正在消费；arq cron 不处于 03:00 重档/轻档 reviewer 执行窗口中）

---

## 时间轴（预计 60–90 min）

### T-0: Schema（上线前已就绪，只做检查）

已随 Plan A Task 1 + Plan D Task 1 落线上：

- [ ] `/ops-db @chiwei` 查 schema：5 张新表 `fragment` / `abstract_memory` / `memory_edge` / `notes` / `schedule_revision` + 14 个索引存在
- [ ] `life_engine_state.state_end_at` 列存在（`TIMESTAMPTZ`，允许 NULL）
- [ ] Qdrant 两个 collection 存在：`memory_fragment`、`memory_abstract`（1024 维 / COSINE）。第一次 agent-service 启动时 `init_collections` 会建，无需手动

### T+0: Langfuse prompt 切 label（必做）

泳道测试用的 `mem-v4` label 转为 `production`。操作通过 `/langfuse` skill，每个 prompt 逐个切换：

- [ ] `life_engine_tick` — 把 `production` label 挪到最新的 `mem-v4` 版本
- [ ] `life_engine_state_refresh` — `mem-v4` v1 → `production`
- [ ] `memory_reviewer_light` — `mem-v4` v1 → `production`
- [ ] `memory_reviewer_heavy` — `mem-v4` v1 → `production`
- [ ] `afterthought_conversation` — `mem-v4` v5 → `production`（替换 v4）
- [ ] `memory_migrate_relationship` — 确认存在且已是 `production`

**操作**（以 `life_engine_tick` 为例）：

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py update-labels life_engine_tick --labels production,latest --version <mem-v4 版本号>
```

完成后 `get-prompt <name>` 核对 `production` 指向新版本。

### T+5: 迁移 relationship_memory_v2 → abstract_memory

**dry-run**：
```bash
cd apps/agent-service
uv run python scripts/migrate_relationship_to_abstract.py --dry-run --limit 5
```
看输出：每条 `RelationshipMemoryV2` 应该拆成 N 个 fragment + 1 个 abstract + N 条 supports edge。确认 LLM 重写的抽象内容合理。

**真跑**：
```bash
uv run python scripts/migrate_relationship_to_abstract.py
```

**校验**：
- [ ] `/ops-db @chiwei` SELECT count(*) FROM abstract_memory WHERE created_by='migration' → 应约等于 `SELECT COUNT(DISTINCT (persona_id, user_id)) FROM relationship_memory_v2`
- [ ] `SELECT count(*) FROM fragment WHERE source='migration' AND id LIKE 'f_mig_%'` — fragment 数应约等于 `SUM(core_facts_line_count)` across v2 rows
- [ ] `SELECT count(*) FROM memory_edge WHERE from_type='fact' AND to_type='abstract' AND edge_type='supports' AND created_by='migration'`
- [ ] 抽 3–5 条随机 abstract_memory + 它的 supports edge 指向的 fragment，人工核对语义连贯

### T+15: 迁移 experience_fragment → fragment

**dry-run**：
```bash
uv run python scripts/migrate_fragment_to_fragment.py --dry-run --days 7 --limit 5
```

**真跑**（默认最近 7 天）：
```bash
uv run python scripts/migrate_fragment_to_fragment.py --days 7
```

**校验**：
- [ ] `SELECT count(*) FROM fragment WHERE id LIKE 'f_mig_%' AND source='afterthought'`（或 `glimpse`，取决于原 grain）约等于 `SELECT count(*) FROM experience_fragment WHERE grain IN ('conversation','glimpse') AND created_at > now()-interval '7 days'`
- [ ] 迁移脚本会 enqueue vectorize，下一步监控向量化追齐

### T+25: 等待 vectorize 追齐

- [ ] `make logs APP=vectorize-worker KEYWORD="vectorize" SINCE=30m` 观察消费速率
- [ ] Qdrant point count 接近 PG count：
  - `memory_fragment` point count ≈ `SELECT count(*) FROM fragment WHERE clarity != 'forgotten'`
  - `memory_abstract` point count ≈ `SELECT count(*) FROM abstract_memory WHERE clarity != 'forgotten'`
- [ ] `/ops status` 看 vectorize 消费队列积压是否归零

### T+35: 部署 main 到 prod（agent-service 一镜像三服务）

**前提**：PR 已 merge 到 main。

```bash
make deploy APP=agent-service GIT_REF=main
make deploy APP=arq-worker GIT_REF=main
make deploy APP=vectorize-worker GIT_REF=main
```

**注意**：`make deploy` 会杀旧 Pod。部署瞬间会中断正在跑的 afterthought debounce 计时器和任何正在 stream 的 chat。选窗口时避开。

### T+45: 观察 prod 正常

- [ ] `make logs APP=agent-service KEYWORD=error SINCE=10m` — 无 traceback
- [ ] `make logs APP=arq-worker KEYWORD="light\|heavy\|state_sync" SINCE=10m` — 新 reviewer 没报错
- [ ] 在飞书 prod 群发条消息，看 Langfuse trace：
  - inner context 出现 `self_abstracts` / `user_abstracts` / `active_notes` / `recall_index` 等新 section
  - 任何 tool call（recall / write_note / update_schedule / commit_abstract_memory）正常返回
- [ ] `/ops-db @chiwei` SELECT count(*) FROM fragment WHERE source='afterthought' AND created_at > now()-interval '30 min' — 新消息应该开始进新表

### T+60: 让用户实际用 30–60 min

- [ ] 用户发几轮消息，包括私聊 + 群聊
- [ ] 故意触发 `update_schedule`（让赤尾改日程），确认 state_sync 正常：`schedule_revision` 新行 + `life_engine_state` 新行
- [ ] 观察 03:00 当天的 heavy reviewer（或手动触发一次）是否跑完无错误

### T+日+1: 旧表处置

旧表不立即 drop，保留 1 周只读观察。相关写路径已断：

- 新 afterthought → 只写新 `fragment`
- 新 glimpse → 只写新 `fragment`
- ~~`insert_relationship_memory`~~ — 注意：`app/memory/relationships.py` 仍在写 `relationship_memory_v2`。这是 **已知的后续清理项**（见 Followups）。如果要彻底止血写入，下一个 PR 删 relationships.py 的写入或者改向 v4。

加日历提醒：cutover + 7 天 drop：
```sql
DROP TABLE experience_fragment;
DROP TABLE relationship_memory_v2;
```

（`life_engine_state` 保留 — `state_end_at` 只是加列，旧数据仍能用）

---

## 回滚（失败时）

### 代码回滚

```bash
# agent-service 一镜像多服务，全部回滚到上个稳定版本
make release APP=agent-service VERSION=<prev-version>
make release APP=arq-worker VERSION=<prev-version>
make release APP=vectorize-worker VERSION=<prev-version>
```

找 `<prev-version>`：
```bash
make latest-build APP=agent-service  # 或从 PaaS Dashboard 看 history
```

### 数据回滚

不需要回滚。新表可以保留（旧代码不读，存在不影响），迁移来的数据带 `id LIKE 'f_mig_%'` / `created_by='migration'`，必要时可 SELECT 清理：

```sql
DELETE FROM fragment WHERE id LIKE 'f_mig_%';
DELETE FROM abstract_memory WHERE created_by='migration';
DELETE FROM memory_edge WHERE created_by='migration';
```

重跑时迁移脚本自己会清理这些行（idempotent）。

### Langfuse label 回滚

如果 prompt 切换后发现问题，`update-labels` 把 `production` 挪回之前版本即可：

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py update-labels life_engine_tick --labels production --version <旧版本号>
```

---

## Followups（cutover 后 1–2 个 PR 内清理）

1. **重构 `app/memory/relationships.py`**：停止写 `relationship_memory_v2`，改为让 afterthought 之后的 reviewer 轻档直接从对话 fragment 里抽象。或者删 relationships.py 整个管道。
2. **删悬挂 code**：
   - `_make_commit_tool` 在 `app/life/engine.py` + `app/life/state_sync.py` 重复 ~35 LOC，抽公共
   - `tick()` 的 `dry_run` 参数在 tool-based 重写后变 no-op，删掉或实装
   - `short_term_fragments` section 用 `chat_id[:6]` 当标签没语义，换成 group name / "p2p"
   - `update_schedule` 每次 create/close arq pool；接 `app/infra/arq_pool.py` 共享 pool
3. **删旧 ORM**：
   - `ExperienceFragment` — 1 周后旧表 drop 时同步删 class（grep 确认无 caller）
   - `RelationshipMemoryV2` — 同上
   - `find_today_fragments` + `insert_experience_fragment` — 旧 experience_fragment 表相关 CRUD
4. **Qdrant 旧 collection**：如果有 `relationship_memory_v2` 的 Qdrant collection 和 `experience_fragment` 的，1 周后 drop collection

---

## 相关文档

- Spec: `docs/superpowers/specs/2026-04-16-memory-v4-design.md`
- Plan A (data layer): `docs/superpowers/plans/2026-04-18-memory-v4-a-data-layer.md`
- Plan B (tools + recall): `docs/superpowers/plans/2026-04-18-memory-v4-b-tools-and-recall.md`
- Plan C (context injection): `docs/superpowers/plans/2026-04-18-memory-v4-c-context-injection.md`
- Plan D (life engine + state sync): `docs/superpowers/plans/2026-04-18-memory-v4-d-life-engine-and-state-sync.md`
- Plan E (reviewer + cutover): `docs/superpowers/plans/2026-04-18-memory-v4-e-reviewer-and-cutover.md`
