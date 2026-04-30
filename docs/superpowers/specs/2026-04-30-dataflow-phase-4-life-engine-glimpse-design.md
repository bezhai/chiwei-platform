# Dataflow Phase 4 — Life Engine / Schedule / Glimpse 进 Graph

**状态**: Draft v1 (2026-04-30)
**前置**: PR #205 (Phase 3 drift/afterthought) shipped to prod 1.0.0.320
**后续**: Phase 5 chat 主 pipeline + Stream[T] runtime

## 1. 背景

Phase 0+1 落地 runtime 框架 + vectorize；Phase 2 把 safety 收进 graph；Phase 3 落地 `.debounce()` runtime 并把 drift / afterthought 改造成节点。`arq_settings.cron_jobs` 是 dataflow 没接管的最后一片：7 条 cron + `for_each_persona` 轮询 + 一处"现读 pg 判活动状态"的轮询模式（glimpse）。

**业务收益（唯一一条）**: glimpse 改事件驱动。当前每 5 分钟扫所有 persona、读 `life_state` 判 `activity_type` —— 赤尾刚切到 `browsing` 要等下一个 5 分钟刻度才会瞥手机，粒度反人类。改完后 `life_state` 一变就触发 glimpse 节点，符合"赤尾是人不是工程系统"。

**工程收益**: cron 入口/扇出/业务 node 在 graph 上各自显式，per-persona × per-task 路径独立 —— 跟 Phase 2/3 的取向一致。`for_each_persona` / `prod_only` / `cron_error_handler` 这一套 worker 装饰器消失，统一走 runtime 边语义。

**验收点**:
- `apps/agent-service/app/workers/cron.py` 整文件删除
- `arq_settings.cron_jobs = []`（保留 `functions` 用于事件触发的 worker）
- `app/workers/common.py` 的 `for_each_persona` / `prod_only` / `cron_error_handler` 全部删除
- glimpse 不再读 `find_latest_life_state` 判活动；触发源是 `LifeStateChanged` 事件
- `compile_graph()` 通过；agent-service 启动后 7 条 cron 节奏与现状一致（前后两轮 30min 观察）

## 2. 现状

### 2.1 `arq_settings.cron_jobs`（apps/agent-service/app/workers/arq_settings.py:95-126）

| # | Cron | Cron 表达式 | 入口 | 是否 fan-out persona | prod_only |
|---|---|---|---|---|---|
| 1 | task_executor | `* * * * *` | `task_executor_job` | ❌（轮询表） | ❌ |
| 2 | life_engine_tick | `* * * * *` | `cron_life_engine_tick` → `life.engine.tick` | ✅ | ✅ |
| 3 | glimpse | `*/5 * * * *` | `cron_glimpse` | ✅（内部 + 现读 pg） | ✅ |
| 4 | light_day | `0,30 8-21 * * *` | `cron_memory_reviewer_light_day` | ✅ window=30 | ✅ |
| 5 | light_night | `0 22,23,0,1,2,4,5,6,7 * * *` | `cron_memory_reviewer_light_night` | ✅ window=60 | ✅ |
| 6 | heavy_review | `0 3 * * *` | `cron_heavy_review` → `run_heavy_review` | ✅（内部） | ✅ |
| 7 | daily_plan | `0 5 * * *` | `cron_generate_daily_plan` → `generate_all_daily_plans` | ✅（内部） | ✅ |
| 8 | voice | `0 8-23 * * *` | `cron_generate_voice` → `generate_voice` | ✅ | ✅ |

**`task_executor_job` 不在本期范围**（long_tasks 是独立子系统，跟 life/schedule/glimpse 解耦；保留 arq cron）。其余 7 条全迁。

### 2.2 fan-out / prod_only 工具（apps/agent-service/app/workers/common.py）

```python
async def for_each_persona(fn, *, label):
    async with get_session() as s:
        ids = await list_all_persona_ids(s)
    for pid in ids:
        try: await fn(pid)
        except Exception: logger.exception("[%s] %s failed", pid, label)

def prod_only(fn):  # if settings.lane and settings.lane != "prod": return None
    ...

def cron_error_handler():  # log + 不中断 scheduler
    ...
```

迁移后这三个装饰器/helper 全部消失，由 fan-out node + runtime 自身的"emit 内部 try/except"承担。

### 2.3 glimpse 现读 pg 模式（apps/agent-service/app/workers/cron.py:75-108）

```python
async def cron_glimpse(ctx):
    persona_ids = await list_all_persona_ids(...)
    groups = list_target_groups()
    for pid in persona_ids:
        state = await find_latest_life_state(pid)
        if state.activity_type == "sleeping": continue
        if state.activity_type != "browsing" and random.random() >= 0.15: continue
        for chat_id in groups:
            await run_glimpse(pid, chat_id)
```

每 5 分钟全量扫 + 全量读 `life_state` 判活动 —— 是本期"消灭现读 pg"的唯一目标。

### 2.4 life_state 写入点（apps/agent-service/app/life/tool.py:84-94）

```python
async def commit_life_state_impl(...) -> CommitResult:
    # validations §9.5
    async with get_session() as s:
        life_state_id = await insert_life_state(s, persona_id=..., activity_type=..., ...)
    return CommitResult(ok=True, is_refresh=is_refresh, life_state_id=...)
```

唯一调用 `insert_life_state` 的入口 —— `commit_life_state` tool（life engine LLM 调用）+ `state_only_refresh`（schedule 更新引发的 refresh）都收敛到这里。`LifeStateChanged` 在这里 emit 是最干净的拦截点。

注意 `is_refresh=True`（段内 refresh，§9.5 Validation 4）时 `activity_type` 与 prev 相同，业务上人没切活动，只是 LLM 重新校准 reasoning —— glimpse 不应该响应这种事件。

## 3. 目标架构

```
[cron */1]    → MinuteTick           → fan_out_life_tick     → LifeTickRequest    → life_tick_node
[cron */1]    → MinuteTick           → fan_out_voice         (when hour∈8..23)    → VoiceRequest       → voice_node
                                                              ──────────────
                                       说明：MinuteTick 复用一个 cron source；
                                       不同 fan-out 节点用 .when(predicate) 过滤
                                       自己关心的小时位 —— 避免每个频率开一个
                                       cron source。
[cron */30 8-21] → LightDayTick      → fan_out_light_day     → LightReviewRequest(window=30)  → light_review_node
[cron 0 22-7 except 3] → LightNightTick → fan_out_light_night → LightReviewRequest(window=60) → light_review_node
[cron 0 3]    → HeavyReviewTick      → fan_out_heavy         → HeavyReviewRequest  → heavy_review_node
[cron 0 5]    → DailyPlanTick        → fan_out_daily_plan    → DailyPlanRequest    → daily_plan_node

[life.tool.commit_life_state_impl 写入成功 + activity_type 真正切换]
              → LifeStateChanged     → glimpse_node          → (per-target-group emit) GlimpseRequest → run_glimpse_node
```

**为什么不复用一个统一的 `PersonaTick(task=...)`**: dataflow 优先用 Data 类型分发（参考 Phase 2 `PreSafetyRequest` / `PostSafetyRequest`、Phase 3 `DriftTrigger` / `AfterthoughtTrigger`）。每条业务链一个类型让 graph 可读、edge 行为独立可调（哪天 voice 要加 `.durable()`、daily_plan 要加 `.debounce()`，类型分发不会牵连别的链）。重复的只是 fan-out 模板，几行。

**为什么不复用一个 `Tick`**: cron source 在 engine 里以 `data_type` 为键挂 source loop（engine.py:174-185），同一个 `Tick` 类型不能同时挂多个不同 cron 表达式 —— wire 是 `wire(T).from_(Source.cron(A))`，多挂会让所有 wire 的 consumer 在每个 cron 都触发。每种频率需要独立的 Tick 类型。

**MinuteTick 例外**: voice 和 life_tick 都是 1 分钟节奏，复用同一个 MinuteTick + 各自 fan-out 节点用 `.when()` 过滤小时位即可。这是唯一一处重叠节奏，独立开 `VoiceTick(0 8-23)` 也行但浪费一个 cron source。

### 3.1 Data 类（新增；建议新建 `app/domain/life_dataflow.py` 或附在已有 `app/domain/life.py`）

```python
class MinuteTick(Data):              ts: str
class LightDayTick(Data):            ts: str
class LightNightTick(Data):          ts: str
class HeavyReviewTick(Data):         ts: str
class DailyPlanTick(Data):           ts: str

class LifeTickRequest(Data):         persona_id: Key[str]; ts: str
class VoiceRequest(Data):            persona_id: Key[str]; ts: str
class LightReviewRequest(Data):      persona_id: Key[str]; ts: str; window_minutes: int
class HeavyReviewRequest(Data):      persona_id: Key[str]; ts: str
class DailyPlanRequest(Data):        persona_id: Key[str]; ts: str

class LifeStateChanged(Data):        persona_id: Key[str]; activity_type: str; prev_activity_type: str; ts: str
class GlimpseRequest(Data):          persona_id: Key[str]; chat_id: Key[str]; ts: str
```

`Key[str]` 用于 `with_latest` 时的索引（Phase 0 框架约定）。本期暂没用到 `with_latest`，但放着不亏。

`prev_activity_type` 为空字符串表示首次提交 life_state；非空表示前一段的 activity。`glimpse_node` 用 `.when(lambda d: d.activity_type != d.prev_activity_type)` 过滤段内 refresh。

### 3.2 Wire 注册（新建 `app/wiring/life_dataflow.py`）

```python
from app.runtime import Source, wire
from app.domain.life_dataflow import (...)
from app.nodes.life_dataflow import (
    fan_out_life_tick, fan_out_voice,
    fan_out_light_day, fan_out_light_night,
    fan_out_heavy, fan_out_daily_plan,
    life_tick_node, voice_node,
    light_review_node, heavy_review_node, daily_plan_node,
    glimpse_node, run_glimpse_node,
)

# Cron source → Tick → fan-out → PersonaXxxRequest → business node

wire(MinuteTick).from_(Source.cron("* * * * *")).to(fan_out_life_tick, fan_out_voice)
wire(LightDayTick).from_(Source.cron("0,30 8-21 * * *")).to(fan_out_light_day)
wire(LightNightTick).from_(Source.cron("0 22,23,0,1,2,4,5,6,7 * * *")).to(fan_out_light_night)
wire(HeavyReviewTick).from_(Source.cron("0 3 * * *")).to(fan_out_heavy)
wire(DailyPlanTick).from_(Source.cron("0 5 * * *")).to(fan_out_daily_plan)

wire(LifeTickRequest).to(life_tick_node)
wire(VoiceRequest).to(voice_node)
wire(LightReviewRequest).to(light_review_node)
wire(HeavyReviewRequest).to(heavy_review_node)
wire(DailyPlanRequest).to(daily_plan_node)

# Event-driven glimpse
wire(LifeStateChanged).when(lambda d: d.activity_type != d.prev_activity_type).to(glimpse_node)
wire(GlimpseRequest).to(run_glimpse_node)
```

注：`wire(MinuteTick).to(fan_out_life_tick, fan_out_voice)` 让两条 fan-out 共享同一个 cron source；`fan_out_voice` 内部用 `datetime.now().hour` 判断是否在 8..23 区间。`.when()` 在 wire 层也能加，但 `.when()` 接收的是 Data 实例（不含 hour），还得从 ts 字段解析 —— 放 fan-out 节点内部判更直接。

### 3.3 Node 实现（新建 `app/nodes/life_dataflow.py`）

```python
@node
async def fan_out_life_tick(t: MinuteTick) -> None:
    if not _is_prod(): return
    for pid in await _list_persona_ids():
        try: await emit(LifeTickRequest(persona_id=pid, ts=t.ts))
        except Exception: logger.exception("[%s] life_tick fan-out failed", pid)

@node
async def fan_out_voice(t: MinuteTick) -> None:
    if not _is_prod(): return
    if datetime.fromisoformat(t.ts).astimezone(CST).hour not in range(8, 24): return
    if datetime.fromisoformat(t.ts).astimezone(CST).minute != 0: return  # voice 是整点触发
    for pid in await _list_persona_ids():
        try: await emit(VoiceRequest(persona_id=pid, ts=t.ts))
        except Exception: logger.exception("[%s] voice fan-out failed", pid)

# fan_out_light_day / fan_out_light_night / fan_out_heavy / fan_out_daily_plan 同模板
# light_day: window_minutes=30；light_night: window_minutes=60

@node
async def life_tick_node(r: LifeTickRequest) -> None:
    from app.life.engine import tick
    await tick(r.persona_id)

@node
async def voice_node(r: VoiceRequest) -> None:
    from app.memory.voice import generate_voice
    await generate_voice(r.persona_id)

@node
async def light_review_node(r: LightReviewRequest) -> None:
    from app.memory.reviewer.light import run_light_review
    await run_light_review(persona_id=r.persona_id, window_minutes=r.window_minutes)

@node
async def heavy_review_node(r: HeavyReviewRequest) -> None:
    from app.memory.reviewer.heavy import run_heavy_review
    await run_heavy_review(persona_id=r.persona_id)

@node
async def daily_plan_node(r: DailyPlanRequest) -> None:
    from app.life.schedule import generate_daily_plan_for
    await generate_daily_plan_for(r.persona_id)

# Event-driven glimpse
@node
async def glimpse_node(c: LifeStateChanged) -> None:
    """LifeStateChanged → 若切到 browsing 必发；其他活动 15% 概率发；sleeping 不发。"""
    if not _is_prod(): return
    if c.activity_type == "sleeping": return
    if c.activity_type != "browsing" and random.random() >= 0.15: return
    from app.life.glimpse import list_target_groups
    for chat_id in list_target_groups():
        try: await emit(GlimpseRequest(persona_id=c.persona_id, chat_id=chat_id, ts=c.ts))
        except Exception: logger.exception("[%s][%s] glimpse fan-out failed", c.persona_id, chat_id)

@node
async def run_glimpse_node(r: GlimpseRequest) -> None:
    from app.life.glimpse import run_glimpse
    await run_glimpse(r.persona_id, r.chat_id)
```

**为什么所有 node 都套薄壳调原函数而不是把业务搬进来**：本期是调度层迁移，不是业务重写。原 `tick / generate_voice / run_light_review / run_heavy_review / generate_daily_plan_for / run_glimpse` 函数语义不动。后续 Phase 5/6 在重写 chat / 清扫 bridges 时再回头看这些 node 该不该继续薄壳。

**注：`generate_all_daily_plans` 拆成 `generate_daily_plan_for(persona_id)`**: 现在 `generate_all_daily_plans` 内部循环 + `for_each_persona` 风格；本期把 per-persona 逻辑抽成 `generate_daily_plan_for(persona_id)`，让 `daily_plan_node` 处理一个 persona。`generate_all_daily_plans` 删除（fan-out 在 graph 上）。`heavy_review` 同样处理：`run_heavy_review()` → `run_heavy_review(persona_id)`，`heavy_review_node` 处理一个 persona，graph 端 fan-out。这两个函数原本就是"内部 for_each_persona"，拆开就是顺手的事。

### 3.4 LifeStateChanged 触发点（修改 `app/life/tool.py`）

```python
# commit_life_state_impl 在 insert_life_state 成功后追加：
async with get_session() as s:
    life_state_id = await insert_life_state(s, ...)

# Emit event for event-driven downstream (e.g. glimpse).
# is_refresh=True 时 activity_type 与 prev 相同，by-design 不应触发 activity 切换
# 类的事件 —— glimpse 已在 wire().when() 处过滤，但这里也按"语义事实"传递：
prev_activity = (prev_state.activity_type if prev_state else "") or ""
await emit(LifeStateChanged(
    persona_id=persona_id,
    activity_type=activity_type,
    prev_activity_type=prev_activity,
    ts=now.isoformat(),
))
```

**emit 失败处理**: `emit` 内部 in-process dispatch 抛异常会冒泡。`commit_life_state_impl` 走在 langchain tool 调用栈里 —— 抛异常会让 tool 报错，life engine 重试。这是可接受的 (life_state 已 insert 成功，tool 把"emit 失败"作为 ok=False 重试可能反而双 insert)；用 `try/except` 包住 emit 让"事件丢失但状态成功"是 best-effort 语义，跟 glimpse 业务关键性匹配。决定：**包 try/except**，emit 失败仅 log，不影响 commit_life_state 返回值。

### 3.5 prod_only 处理

每个 fan-out 节点首行 `if not _is_prod(): return`（settings.lane 非空且 ≠ "prod" 时返回）。glimpse_node 同样首行判。`_is_prod()` helper 放 `app/nodes/life_dataflow.py` 顶部，3 行。

**为什么不在 wire 层加 `.when(prod_only)`**: dataflow 倾向 wire 描述拓扑、predicate 描述业务过滤。"是否在 prod 跑"是部署关切，写在 node 内部更易读、易在测试里临时打开。

**为什么 dev 泳道仍跑 cron source loop**: Phase 0 设计 `Source.cron` 在所有泳道都启动。dev 泳道的 cron 触发后 fan-out 节点直接 return，没有 emit PersonaXxxRequest，业务 node 不会跑。代价：dev 每分钟有一次空操作日志。可接受。

## 4. 删除项

迁完所有 wire 后立即删（不留兼容 shim）：

- `apps/agent-service/app/workers/cron.py`（整文件）
- `apps/agent-service/app/workers/common.py`：`for_each_persona` / `prod_only` / `cron_error_handler` 三个 helper（保留 `mq_error_handler` —— 它给 mq consumer 用，不在本期范围）
- `apps/agent-service/app/workers/arq_settings.py`：`cron_jobs = []`（保留 `functions=[sync_life_state_after_schedule]`）
- `apps/agent-service/app/life/schedule.py::generate_all_daily_plans`（被 fan-out 替代）
- `apps/agent-service/app/memory/reviewer/heavy.py` 中的 "run_heavy_review() 无参数循环 persona" 形态（改成 `run_heavy_review(persona_id)`）
- 旧 `cron_glimpse` 内部的 `find_latest_life_state` 现读 pg —— 整段轮询代码删（被 LifeStateChanged 事件流替代）

## 5. 部署 / 风险

### 5.1 部署影响

- **agent-service 重启**: cron source loop 在 startup 阶段挂起。新部署 = 重启 = cron 当前分钟那一拍可能丢（Phase 3 已确认这是 cron source 既有语义，不改）。
- **arq-worker 重启**: 本期 arq-worker 主要任务降级为 `task_executor_job` 的 polling + `sync_life_state_after_schedule` 事件 worker；cron_jobs 清空。重启 arq-worker 不影响 life/glimpse/voice 等任何节奏。
- **glimpse 切事件驱动的语义变化**: cutover 当下若有 persona 处于"非 sleeping 但非 browsing"且 5 分钟周期还没到 → 旧逻辑这一刻不触发；新逻辑也不触发（要等下一次 LifeStateChanged）。**首次部署后第一个不在线状态切换可能让 glimpse 静默几小时** —— 这是预期行为，符合"赤尾刚开始 browsing 才会瞥手机"的业务语义。

### 5.2 可观测性

每个 fan-out node + 业务 node 都是 graph 上独立 callable，runtime 自带 emit 异常 log（emit.py 注释明确"in-process dispatch is strict"）。每条 cron / 每个 persona / 每个任务的失败可分别在日志中 grep node 名定位。

`/ops` 监控规则在 PR #202 已给 dataflow runtime queues + DLQ 加了告警 —— 本期 wire 都是 in-process（无 `.durable()`），不进 mq，不在那批告警范围。glimpse 切事件驱动后频次大幅下降（从每 5min × N persona 降到"状态切换时"），日志量减少是预期。

### 5.3 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| `commit_life_state_impl` emit 失败 | LifeStateChanged 丢，glimpse 这一次不响应 | try/except 包；日志报警；下次 state 切换会再有事件 |
| fan-out 节点抛异常（如 `list_all_persona_ids` 撞 db） | 这一拍整批 persona 不触发 | runtime emit dispatch 会让异常冒泡到 source loop，source loop catch 后继续下一拍（engine.py:251 _record_source_error 会让进程退出 —— 需要在 fan-out 内部包 try/except，跟 `for_each_persona` 当前一致） |
| cutover PR 太大 reviewer 难审 | review 拖延 / 漏看 | 7 条 cron 改一个 PR；按"先建后切再删"提交：(1) 加 Data + nodes + wires；(2) `arq_settings.cron_jobs = []`；(3) 删 cron.py + helpers。每个 commit 单独可读 |
| 测试覆盖不足导致 cutover 翻车 | life-tick / voice / heavy / daily-plan 节奏断 | 每条 fan-out + 业务 node 加单元测试（mock list_persona_ids + 断言 emit 出对应 Data）；wiring 模块加 compile_graph 通过的烟雾测试 |
| dev 泳道空跑产生噪声 log | 仅日志干扰 | fan-out node `_is_prod()` 检查放最前 + 用 DEBUG 级别记录"skipped (non-prod)"，不在 INFO |

### 5.4 测试策略

- 单元测试（每个 fan-out / 业务 node 一个 happy + 一个 prod_only 跳过 + 一个 emit 失败/异常路径）
- compile_graph 测试：load `app/wiring/__init__.py` 后 `compile_graph()` 通过，wire 数量 = 11（5 cron + 5 PersonaXxxRequest + 1 LifeStateChanged + 1 GlimpseRequest = 12 实际，加上原有 wires 计数会更多 —— 测试只断言新增 wire 都在）
- 集成测试：mock `Source.cron` 用 `Source.interval(0.1)`，跑 ~1s 验证 fan-out 节点确实触发
- 泳道验证（feat-flow-parse-4）：部署 + 用 `make logs APP=agent-service KEYWORD=life_tick_node` 观察 1 个 minute tick 完整链路；`/ops-db @chiwei 'select count(*) from life_states where ...'` 观察 cutover 前后 30min 内 life_state 写入节奏一致

## 6. 验收 checklist

- [ ] `app/workers/cron.py` 不存在
- [ ] `app/workers/common.py` 没有 `for_each_persona` / `prod_only` / `cron_error_handler` 三个名字
- [ ] `arq_settings.WorkerSettings.cron_jobs == []`（grep 验证）
- [ ] `app/wiring/life_dataflow.py` 存在，12 条 wire（5 cron-tick + 5 PersonaXxxRequest + LifeStateChanged + GlimpseRequest）
- [ ] `app/nodes/life_dataflow.py` 存在，13 个 @node（6 fan-out + 5 business + glimpse_node + run_glimpse_node）
- [ ] `app/life/tool.py::commit_life_state_impl` 末尾 emit `LifeStateChanged`
- [ ] `app/workers/cron.py::cron_glimpse` 不存在；`find_latest_life_state` 仅由 life engine / state_sync 内部调（grep 出现位置 ≤ 现状 - 1）
- [ ] compile_graph 通过；agent-service 启动日志显示新增 5 个 cron sources + 13 个 @node（在原 graph 计数基础上增量）
- [ ] 泳道部署 30 分钟观察：life_state 写入节奏、voice 整点触发、light reviewer 节奏、glimpse 触发条件均与现状一致
- [ ] 前后 1h `select count(*) from life_states / fragments / voice_*` 计数对比，正负不超 5%

## 7. 不在本期范围

- `task_executor_job`（long_tasks 子系统）—— 保留 arq cron
- 任何 cron / fan-out 节点加 `.durable()` —— 这些任务都"丢一拍可接受"，不值上 mq
- `with_latest(LifeState)` 接入 —— 后续若发现 glimpse_node 需要 prev state 的更多字段，再补
- chat 主 pipeline / Stream[T] / bridges 整包删 —— Phase 5 / 6
