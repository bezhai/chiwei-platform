# Dataflow Phase 6 — 终结清扫（capability gap surface）

**状态**: Draft v4 (2026-05-07，重定 scope 到原 dataflow design line 543 "终结清扫")
**前置**: PR #209 (Phase 5b) shipped to prod 1.0.0.328；本期分支 `refactor/flow-parse-6` 已落 v3 4 commits（PR #210）作为 v4 baseline
**后续**: v5/v6 继续闭合 capability gap，Gap 1-12 的全 surface 见 §1

## 0. v4 vs v3 的关系

v3（2 刀：proactive emit ChatTrigger + queries.py 拆 9 domain）已落 PR #210，4 commits：
- `d312a48` glimpse 升级监控（保留 catch + `[chat_submit_failed]` 标记）
- `aece91d` chat placement 硬验收
- `53961db` proactive emit ChatTrigger
- `b3920c3` queries.py 拆 9 domain

v4 **重定 Phase 6 的 scope** 到原 dataflow design `2026-04-21-agent-dataflow-abstraction-design.md` line 543：

> 7. **Phase 6 — 清扫**：删除旧代码、旧 worker 入口、旧 orm crud god object、bridge 残留。

v3 把 Phase 6 砍成 2 刀（"emit 跨进程不存在"+"workers 没死代码"两个事实约束让原 4 刀降级），剩下扔给虚构的"Phase 7+"。但**原 dataflow design 没规划 Phase 7+**，扔出去的事变成无主之地、长期债务。

v4 的工作：
1. **把 Phase 6 拉回原设计 scope**（业务代码绕开 framework 的痕迹一次清完）
2. **从 framework capability gap 视角组织 spec**（不是从代码改动量视角），后续 v5/v6/... 沿 capability 维度迭代
3. **不留隐患**：v4 实施的 gap 必须做透（无 deprecated wrapper / shim / fallback / TODO）；v4 不实施的 gap **业务代码不允许 workaround**（只能用 framework 当前提供的 primitive 或 fail-fast）。

v3 已实施部分（proactive emit / queries 拆）属于 v4 衍生清扫的子集，**保留 PR #210 那 4 commit 不动**，v4 在它们之上继续。

## 1. Framework Capability Gap Surface（共 12 个）

每条格式：**framework 现状 / 业务绕痕 / 缺什么 / 目标版本 / 不留隐患约束**。

### v4 范围（6 个 gap，本期必须闭合）

#### Gap 1 — HTTP Source 不完整

**Framework 现状**: `runtime/http_source.py` 36 行；只支持 `Source.http(path)` POST + JSON body + 202 fire-and-forget。

**业务绕痕**: `api/routes.py` 292 行 / 13 个手写 `@router.get/post/delete`，覆盖 health / 4 admin trigger / 4 GET 查询 / 1 POST 写返回 / 1 DELETE / 1 root 死代码 / 1 search / 1 debug。

**缺什么**:
- HTTP method（GET / DELETE / PUT，不只是 POST）
- path 参数（`/api/schedule/{id}` 这类）
- query 参数（`?persona_id=xxx` 这类）
- sync response body（业务想拿写后返回值 / 查询结果，不只是 fire-and-forget 202）
- 健康检查 builtin 入口（不需要业务声明 wire）

**v4 目标**: `runtime/http_source.py` 扩到完整 capability，所有 13 个 endpoint 全走 wiring；`routes.py` 删除（或仅留 < 30 行的 `register_http_sources(app)` 调用，且 main.py 已经 import wiring）。

**不留隐患约束**: v4 完成后，`grep -rn "@router\.\|@app\." apps/agent-service/app/` = 0（除 framework 自身的 `app.post(...)` 注册逻辑）。**业务代码 0 个手写 FastAPI route**。

---

#### Gap 2 — emit 跨进程

**Framework 现状**: `runtime/emit.py:54` `emit()` 只在本进程 dispatch；不会反查 wire 的 `Source.mq(...)` 自动 publish 到 mq queue。同进程发同进程消费 in-process 直接调 consumer；publisher 在 A 进程 / consumer 在 B 进程时 `if c not in own_nodes: continue` 静默跳过 → 消息丢失。

**业务绕痕**:
- `memory/vectorize_memory.py:112,123` `enqueue_fragment_vectorize` / `enqueue_abstract_vectorize` 仍 `mq.publish` 直发（v3 §0 第二刀推迟的根因）
- `domain/safety.py:62` 注释提"原 mq.publish RECALL"

**缺什么**: emit Data 时，runtime 应当：
1. 收集所有匹配 wire（已实现）
2. 对每个 wire：若 consumer in-process（`c in own_nodes`）→ 直接调（已实现）；**若不在本进程 + wire source 是 mq → 自动 publish 到该 mq queue**（缺）
3. 同一 Data 既有 in-process consumer 也有跨进程 consumer 时 → 两条路径都要走

**v4 目标**: emit 透明支持跨进程；`memory/vectorize_memory.py` 的 `enqueue_*` helpers 删除，调用方改 `await emit(MemoryFragmentRequest(...))` / `await emit(MemoryAbstractRequest(...))`。

**不留隐患约束**: v4 完成后，`grep -rn "mq.publish" apps/agent-service/app/` 仅在 `runtime/` + `infra/rabbitmq.py` 命中。**业务代码 0 个直接 mq.publish**。

---

#### Gap 3 — agent tool 副作用进 wire

**Framework 现状**: agent tool 调用产生的 DB 副作用（写 abstract / 写 schedule_revision / 写 note / 写 fragment 等）没有 framework 约束去 emit Data；下游消费者只能"再去 query DB"或"通过别的旁路通信"。

**业务绕痕**:
- `agent/tools/commit_abstract.py:48-62` 写 `abstract_memory` + `memory_edge` 后，**单独调 `enqueue_abstract_vectorize`**（即 mq.publish）通知 vectorize-worker
- `agent/tools/update_schedule.py:38` 写 `schedule_revision` 后 **arq enqueue `sync_life_state_after_schedule`**（绕 graph）
- `agent/tools/notes.py:30` 写 `note` 后**没有 emit**，下游 reviewer 只能 query DB

**缺什么**: tool 写 DB → emit Data 是一种"事件溯源"模式：
- `commit_abstract` → emit `AbstractMemoryCommitted` → wire 到 vectorize / dirty-cache invalidation / reviewer notification
- `update_schedule` → emit `ScheduleRevisionCreated` → wire 到 sync_life_state node（替代 arq）
- `commit_note` → emit `NoteCreated` → wire 到 reviewer

**v4 目标**: 每个 mutation tool 写 DB 后 emit Data；旁路通信（mq.publish + arq enqueue）全部转换为 wire（同进程 in-process 或跨进程 durable）。

**不留隐患约束**: v4 完成后，`grep -rn "mq.publish\|enqueue_job\|create_pool" apps/agent-service/app/agent/` = 0。**tool 不允许任何旁路通信**。

---

#### Gap 4 — 事件驱动 worker 没进 graph

**Framework 现状**: dataflow 有 `Source.mq` / `Source.cron` / `Source.http` / 默认 in-process / `.durable()` 等 worker primitive；但事件驱动 worker（业务事件 `Y` 触发 → 跑 worker function `f`）的标准模式是：业务 emit Y → wire(Y).to(f) → in-process 或 durable，**不是再开一套 arq 入口**。

**业务绕痕**:
- `workers/arq_settings.py` 93 行 + `workers/state_sync_worker.py` 44 行 是 arq runtime
- `update_schedule.py` 通过 arq pool `enqueue_job(sync_life_state_after_schedule)` 触发
- 双 worker 入口（`workers/runtime_entry.py` dataflow + `workers/arq_settings.py` arq）共存

**缺什么**:
- 事件驱动 node：直接用 `wire(ScheduleRevisionCreated).to(sync_life_state_node).durable()`，跑在 dataflow runtime 内（agent-service 主进程或 vectorize-worker，按 placement 决定）
- arq 整套删除

**v4 目标**:
- `sync_life_state_after_schedule` 改 dataflow node（接收 `ScheduleRevisionCreated`）
- `update_schedule.py` 删 arq 调用，改 emit `ScheduleRevisionCreated`
- `workers/arq_settings.py` + `workers/state_sync_worker.py` 删除
- arq 依赖从 `pyproject.toml` 移除（如果只是 worker 用）

**不留隐患约束**: v4 完成后，`grep -rn "arq\|enqueue_job\|create_pool" apps/agent-service/app/` 在业务代码 0 命中（仅 README 说明可保留）；`workers/` 仅剩 `runtime_entry.py` + `common.py` + `__init__.py`。

---

#### Gap 5 — 散落 fire-and-forget background task

**Framework 现状**: 没有"后台触发流水"的统一入口；业务用 `asyncio.create_task(coro)` 直接 spawn。

**业务绕痕**:
- `chat/context.py:119` `asyncio.create_task(_persist_tos_files(...))`：写 ConversationMessage.content TOS 文件后台 sync
- `chat/post_actions.py:89,94,99` 三处 `asyncio.create_task(_emit_memory_trigger(...))`：drift / afterthought 触发
- `api/routes.py:139` `asyncio.create_task(generate_daily_plan(...))`：admin trigger schedule（属 Gap 1）
- `chat/pre_safety_gate.py:63` + `nodes/chat_node.py:132` `pre_task = asyncio.create_task(...)`：pre-safety 边界 await pattern（与 Phase 5a 设计一致，**这两处合理保留**）

**缺什么**: fire-and-forget 后台任务应当 emit Data → wire 到对应 node（in-process 或 durable）：
- `_persist_tos_files` → emit `ConversationMessageContentSynced`（durable wire）
- `_emit_memory_trigger`（drift / afterthought）→ 已经有 `MemoryDriftTrigger` / `MemoryAfterthoughtTrigger` Data，post_actions.py 该直接 emit，不需要 wrapper
- `generate_daily_plan` → emit `DailyPlanRequest`（已 cron 触发，admin trigger 共享同 Data）

**v4 目标**: 业务代码 `asyncio.create_task` 仅保留 chat_node pre-safety 边界 await 这种**单次 task 句柄**用法；任何"触发后台任务跑 X 函数"必须 emit Data + wire。

**不留隐患约束**: v4 完成后，`grep -rn "asyncio.create_task\|asyncio.ensure_future" apps/agent-service/app/` 在 `chat/post_actions.py` / `chat/context.py` / `api/routes.py` 0 命中。仅允许 `chat_node` / `pre_safety_gate` / `runtime/` 内部出现（且每处必须有 docstring 说明为何不能用 emit）。

---

#### Gap 6 — 双 worker 入口（Gap 4 副产物）

**Framework 现状**: `workers/runtime_entry.py`（dataflow）+ `workers/arq_settings.py`（arq）并存。同一 ImageRepo `agent-service` 跑出 3 个 K8s Deployment：
- `agent-service`（HTTP server，runtime_entry.py 启动 + uvicorn）
- `arq-worker`（arq runtime 入口）
- `vectorize-worker`（runtime_entry.py 启动 + APP_NAME 过滤）

**业务绕痕**: arq-worker 是单独的 worker entry，跟 vectorize-worker / agent-service 用的 dataflow runtime 是双轨。

**缺什么**: Gap 4 关闭后，arq-worker 这个 Deployment 失去存在理由（worker functions 全是 dataflow node + bind 到 specific app）。

**v4 目标**:
- 删 `workers/arq_settings.py` + `workers/state_sync_worker.py`
- arq-worker Deployment 下线（PaaS API 删 app，或保留作 dataflow 通用 worker entry）
- 重新命名 / 评估：原 arq-worker Deployment 是不是该改用 runtime_entry + APP_NAME=`event-worker`（专跑事件驱动 durable wire 的 dataflow worker pod）

**不留隐患约束**: v4 完成后，`workers/` 目录仅 `runtime_entry.py` + `common.py` + `__init__.py`。K8s Deployment list 不再有 `arq-worker` 名字（或改名为 `event-worker` 跑 dataflow runtime）。

---

### v5 候选（基础能力补强，下版本闭合）

#### Gap 7 — durable wire retry 不可配置

**Framework 现状**: `runtime/durable.py:99` 固定 `requeue=False` fail-to-DLQ；无 retry 机制。

**v4 内业务约束**: 业务**不允许自己实现 retry 循环**（不许 try/except + sleep 重试）。transient error 只能 fail-to-DLQ，运维重放（DLQ replay 受 Gap 12 影响默认 no-op，知道）。

**v5 目标**: `wire(...).retry(n=3, backoff=exponential)` 配置。

#### Gap 8 — emit 跨事务边界（outbox）

**Framework 现状**: 业务靠注释"emit AFTER commit"自觉（`life/proactive.py:141`）。DB 回滚 / emit 已发出会引入数据脏化。

**v4 内业务约束**: 业务必须在 `get_session()` 上下文 **之外** emit（即写 DB commit 后再 emit）；这条约束写进 `runtime/emit.py` docstring 强调，不再依赖业务自觉散落注释。

**v5 目标**: outbox 模式（事务内 insert outbox 表 + commit + 后台 publisher 跑 emit）。

#### Gap 9 — delayed / scheduled emit

**Framework 现状**: 无 `emit_delayed(data, delay=10s)` / `emit_at(data, ts)` API。debounce wire 走 redis SETNX 自实现的延迟（`runtime/debounce.py`），不是 framework primitive。

**v4 内业务约束**: 业务代码**不允许 `await asyncio.sleep(N) + emit`** 这种自实现延迟。需要延迟触发只能用现有 debounce wire 或 cron。

**v5 目标**: x-delayed-message exchange 之类的延迟原语。

#### Gap 10 — streaming response 没原生抽象

**Framework 现状**: v3 spec line 132 注明 `Stream[T]` 已被 Phase 1-4 实践证伪、删除；现状是 fan-out emit 多段 `ChatResponseSegment` + `part_index` / `is_last` 自定义协议。

**v4 内业务约束**: chat 段输出维持 Phase 5a 的 ChatResponseSegment 模式不变；任何**新业务**需要 streaming 输出 → 复用 ChatResponseSegment 协议（part_index / is_last），不允许自定义新协议。

**v5/v6 目标**: 评估是否需要重启 Stream 抽象，或彻底标准化"段输出"模式（共享 part_index 字段约定）。

#### Gap 11 — trace / lane context propagation 散落

**Framework 现状**: contextvars 在 in-process emit 自动传；跨进程 mq publish/consume `runtime/debounce.py:205,256,302` + `runtime/durable.py` 各自手动塞 header / restore。每加一种 Source 类型都要重写一遍。

**v4 内业务约束**: 不新增 Source 类型；现有 Source.mq / Source.http / Source.cron 维持既有 propagation 实现。

**v5 目标**: mq publish/consume 层统一 trace context propagation hook，新 Source 类型自动继承。

#### Gap 12 — DLQ replay 语义不闭合

**Framework 现状**: DLQ 重放被 consumer-side `insert_idempotent` dedup 跳过 → replay 默认 no-op（v3 §2.3 + Phase 5 spec acknowledged）。

**v4 内业务约束**: 业务接受"DLQ 重放 default no-op"现状；运维需要重放时手工清对应 idempotent 行 + 重投 DLQ 消息。这条约束写进 `runtime/durable.py` docstring。

**v5/v6 目标**: DLQ replay 模式（CLI 工具：可选清 idempotent + 重跑）。

## 2. v4 衍生业务清扫（Gap 1-6 关闭后自然要做）

按 capability gap 关闭顺序，业务代码同步收敛：

### 2.1 Gap 1 关闭 → routes.py 收敛

`api/routes.py` 292 行 → < 30 行（仅 `register_http_sources(app)` 调用 + 必要 import）。

需要为每个 endpoint 找/建对应 Data + wire：
- `GET /` 死代码 → 删（用户没人调）
- `GET /health` → builtin Source.http_health（runtime 自带，不需要业务声明）
- `POST /admin/trigger-life-engine-tick` → emit `LifeEngineTickRequest`（或复用 `MinuteTick`，看语义）
- `POST /admin/trigger-glimpse` → emit `GlimpseRequest`（已 wire）
- `POST /admin/debug-glimpse` → 复杂，含读 DB 逻辑；改 GET + Source.http GET，节点 query 后返回 dict
- `POST /admin/trigger-voice` → emit `VoiceRequest`（cron 已触发，复用同 Data）
- `POST /admin/trigger-schedule` → emit `DailyPlanRequest`（cron 已触发，复用）
- `POST /admin/search` → emit `AdminSearchRequest` 节点返回 results（RPC 模式）
- `GET /api/schedule` → Source.http GET + 节点返回 list
- `GET /api/schedule/current` → 同上
- `GET /api/schedule/daily/{target_date}` → Source.http GET + path_params
- `POST /api/schedule` → Source.http POST + 节点写 DB + 返回 saved（emit `ScheduleCreated`）
- `DELETE /api/schedule/{id}` → Source.http DELETE + path_params

### 2.2 Gap 2 关闭 → vectorize_memory.py + 调用方收敛

`memory/vectorize_memory.py`：
- 删 `enqueue_fragment_vectorize` / `enqueue_abstract_vectorize`（共 ~20 行）
- 调用方（`agent/tools/commit_abstract.py` / `life/glimpse.py` / `nodes/memory_pipelines.py`）改 `await emit(MemoryFragmentRequest(fragment_id=fid))` / `await emit(MemoryAbstractRequest(abstract_id=aid))`

### 2.3 Gap 3 关闭 → agent tools 全部 emit

每个 mutation tool 写 DB 后 emit：
- `commit_abstract.py` → emit `AbstractMemoryCommitted`（含 abstract_id / persona_id / chat_id），wire 到 vectorize node + reviewer 通知 node（如有）
- `commit_life_state.py`（如存在）→ emit `LifeStateCommitted`
- `update_schedule.py` → emit `ScheduleRevisionCreated`（替代 arq enqueue）
- `notes.py` → emit `NoteCreated`
- 所有 emit 必须在 `get_session()` commit 之后

### 2.4 Gap 4 关闭 → workers/ 收敛 + arq 退场

- `workers/state_sync_worker.py` → 改 dataflow node `sync_life_state_node`，签名 `(ScheduleRevisionCreated) -> None`
- `workers/arq_settings.py` 删除
- `workers/runtime_entry.py` 维持，可能改名为 `worker_entry.py` 简化
- `workers/common.py` 评估是否还有用，无用则删
- `pyproject.toml` 删 arq 依赖（如仅 worker 用）
- K8s Deployment `arq-worker` 退场（或重命名 `event-worker`）

### 2.5 Gap 5 关闭 → chat/ 后台触发 emit

- `chat/post_actions.py` 三处 `asyncio.create_task(_emit_memory_trigger(...))` → 直接 emit Data（drift / afterthought 各自 Data 已存在）；`_emit_memory_trigger` wrapper 删除
- `chat/context.py:119` `_persist_tos_files` → 改 emit `ConversationMessageContentSynced`（新增 Data） + wire 到 durable node
- `chat_node.py:132` + `pre_safety_gate.py:63` 保留（合法 single-task 句柄用法）

### 2.6 Gap 6 关闭 → workers entry 单一化

Gap 4 副产物，无独立改动。

## 3. 业务实现层冗余（独立维度，跟 framework gap 解耦）

这些不是 capability gap 引起的，是业务模块过去 6 个 phase 没顺手清的冗余。**v4 范围内一并做**：

### 3.1 chat/

- `chat/router.py` (58 行) — audit 跟 `nodes/chat_node.route_chat_node` 是否职责重叠：route_chat_node 是 fan-out persona，router.py 是消息路由决定，可能合并到 route_chat_node 或独立保留（grep 证据后决定）
- `chat/agent_stream.py` (244) + `chat/stream.py` (81) — 两个 stream 模块；评估合并
- `chat/context.py` (445 行) — 单文件超 300，违反 CLAUDE.md；按职责拆分（context / l1_results / persist 等）
- `chat/quick_search.py` (186) — context.py 调它；audit dataflow 改造后链路是否还活、能否简化

### 3.2 life/

- `life/sister_theater.py` (39) + `life/wild_agents.py` (60) — 都被 `life/schedule.py` import；audit 是不是合并到 schedule.py 或保留独立模块（39 行单文件可疑）
- `life/state_sync.py` (118) — Gap 4 关闭后该模块被 `nodes/sync_life_state_node` 替代，业务实现层可能也要随之收敛 / 删除
- `life/engine.py` (237) vs `life/state_sync.py` — full eval 和 lite refresh 的关系是不是该统一抽象

### 3.3 memory/

- `memory/cross_chat.py` (218) + `memory/context.py` (152) — 都是 context builder，audit 合并可能性
- `memory/vectorize_memory.py` (123) — Gap 2 关闭后 enqueue helpers 删，剩 vectorize / vectorize_abstract 实现；评估是不是该并到 `nodes/memory_vectorize.py`

### 3.4 agent/tools/

- 每个 tool 加 emit Data（Gap 3）后，evaluate 是否还有重复的 query helper 可以走 `app.data.queries`

## 4. v4 验收（衡量"终结清扫"是否真到位）

每条都是硬验收，必须 0 命中 / 全过：

### Framework gap 关闭

- `grep -rn "@router\.\|@app\." apps/agent-service/app/` = 0（除 framework 自身 register 逻辑）
- `grep -rn "mq.publish" apps/agent-service/app/` 仅 `runtime/` + `infra/rabbitmq.py` 命中
- `grep -rn "enqueue_job\|create_pool\|from arq" apps/agent-service/app/` 业务代码 0 命中
- `grep -rn "asyncio.create_task" apps/agent-service/app/{chat,life,memory,api}/` 0 命中（仅 `chat_node.py` + `pre_safety_gate.py` 内部允许，且每处带 docstring）
- `find apps/agent-service/app/workers/` 仅 `runtime_entry.py` + `common.py`（如有用）+ `__init__.py`

### 业务代码体量

- `wc -l apps/agent-service/app/api/routes.py` < 50 行
- `wc -l apps/agent-service/app/memory/vectorize_memory.py` < 100 行（删了 2 个 enqueue helpers）
- `wc -l apps/agent-service/app/agent/tools/update_schedule.py` < 60 行（删 arq enqueue 块）
- `wc -l apps/agent-service/app/chat/context.py` < 300 行（拆分）
- 其它业务实现层冗余文件按 §3 评估结论（合并 / 删除 / 保留）

### 行为不变量

- 飞书 dev bot 群聊 + p2p 正常对话
- glimpse 触发 proactive 主动消息
- update_schedule tool 调用 → state sync 节点跑（Gap 4 替代 arq）
- vectorize-worker / event-worker（arq-worker 改名）正常消费
- HTTP admin 接口（GET / POST / DELETE）功能等价
- 全量 pytest green
- ruff queries 包 + 新增 framework 改动 0 新增错

### 业务代码 v4 内不允许的 workaround

- 任何 Gap 7-12 的自实现：retry 循环 / outbox 自手写 / `asyncio.sleep + emit` / 自定义 streaming 协议 / 手动塞 trace header / DLQ 自动重放

## 5. PR 策略

**接进 PR #210**（不新开 PR）。原因：
- PR #210 主题就是 "Phase 6 cleanup"
- v3 4 commits + v4 改动一起 ship，单次 dev 泳道验证、单次 prod ship
- PR diff 会更大（预估 +1000 业务代码净减少，但同时 framework 扩 ~300，新加测试 ~500）

commit 策略：v4 改动按 capability gap 切（每 gap 一个 commit 或一组 commits），diff 在 PR 里 review 时按 gap 维度可读：
1. `feat(runtime): http_source 扩展支持 GET/DELETE/method/path_params/RPC` (Gap 1 framework)
2. `refactor(api): routes.py 收敛到 framework wiring` (Gap 1 业务)
3. `feat(runtime): emit 跨进程 dispatch via wire source.mq` (Gap 2 framework)
4. `refactor(memory): vectorize_memory 删 enqueue helpers，调用方改 emit` (Gap 2 业务)
5. `feat(domain): 新增 ScheduleRevisionCreated / AbstractMemoryCommitted / NoteCreated Data` (Gap 3 framework)
6. `refactor(agent): tool 写 DB 后 emit Data` (Gap 3 业务)
7. `feat(nodes): sync_life_state_node 替代 arq state_sync_worker` (Gap 4)
8. `refactor(workers): 删 arq_settings + state_sync_worker，arq-worker 退场` (Gap 4 + 6 业务)
9. `refactor(chat): post_actions / context 删 asyncio.create_task` (Gap 5 业务)
10. `refactor(chat): context.py 拆分（445 → 多文件，每个 < 300）` (§3.1)
11. `refactor(life): sister_theater + wild_agents + state_sync 收敛` (§3.2)
12. `refactor(memory): cross_chat + context 评估` (§3.3)

每 commit 自包含 + 测试 green + ruff 通过。

## 6. Out of Scope（v4 明确不做）

- Gap 7-12 的实施（v5+ 接续）
- 业务功能变化（任何用户感知层面）
- 新业务功能添加
- agent tool 内部业务逻辑变化（仅"加 emit"，不动 tool 语义）
- 数据库 schema 变更（除非新增 Data 表，但每个新 Data 由 runtime migrator 自动建表，不影响 schema migration）

## 7. 风险与回滚

### 风险

- **Gap 4 arq 退场**：`arq-worker` Deployment 下线后，未消费的 arq 队列消息会丢失（arq 用 redis 队列，不像 mq 持久化）。预案：实施前观察 arq 队列空，再下线
- **Gap 3 tool 改 emit**：tool 调用是 LLM 触发的同步操作，加 emit 后下游异步执行；若 emit 抛错传回 tool 会让 LLM 看到 tool error。需要约定 emit 失败 = tool 业务 success（emit 错误 log 不上抛）or 进 outbox 等 v5
- **Gap 1 routes.py 全删**：admin 工具脚本（如 rebuild）依赖现有 endpoint 形态，HTTP 入口 path / 参数 / response 必须严格保留；framework 注册的 endpoint 跟手写等价

### 回滚

- 每 commit 独立可 revert
- `arq-worker` Deployment 下线前先 `make undeploy` 测试 lane 验证；prod 切换分两步：先 release `event-worker` 替代 deployment，再 undeploy `arq-worker`

## 8. v5+ 路线（仅记录，不实施）

| Version | Gap | 主题 |
|---|---|---|
| v5 | 7, 8 | retry 配置 + outbox 事务边界 |
| v6 | 9, 11 | delayed emit + trace propagation 统一 |
| v7 | 10, 12 | streaming 抽象重启评估 + DLQ replay 模式 |

每版独立 spec / plan / PR，不再合并。
