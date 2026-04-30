# Dataflow Phase 4 — Life Engine / Schedule / Glimpse 进 Graph

**状态**: Draft v4 (2026-04-30，吸收 reviewer 第 2 轮 5 条意见)
**前置**: PR #205 (Phase 3 drift/afterthought) shipped to prod 1.0.0.320
**后续**: Phase 5 chat 主 pipeline + Stream[T] runtime

**v4 关键变化（vs v3）**：
- §3.3 GlimpseRequest 去掉 `Meta.transient = True`，新增 `request_id: Annotated[str, Key]` —— durable 边强制要求 Data 类有持久化表做 `insert_idempotent` dedup（runtime graph.py:286 在 compile 期 raise；v3 同时声明 transient 与 .durable() 会被拒绝）。runtime 自动建 `glimpse_requests` 表，副产品是 glimpse 触发的天然审计。其他 Data 仍 transient（修 round-2 P0 #1）
- §3.0 / §3.7 新增 source watchdog：`Runtime.start_source_loops()` 内部启一个 background watcher task，监 `_source_error` / `_stop_event`，任何 source loop fatal error 触发后让进程退出（lifespan 内 raise / `os._exit(1)`），让 PaaS 拉起新 pod —— 否则 main.py lifespan 调 start_source_loops 返回后 cron task 死亡而 web 还健康（修 round-2 P0 #2）
- §3.5 所有 fan-out 节点把 `_list_persona_ids()` + 整段循环放在 try/except 内，DB 异常仅 log 不冒泡到 source loop（修 round-2 P1 #3，v3 写法 db 调用在 try 外会触发 `_record_source_error` 让进程退出）
- §4 删除项加 `arq_settings.py::on_startup` 里的 `asyncio.create_task(cron_generate_voice(None))` 那行 + 顶部对 `cron_*` 的 import；`cron.py` 删除后这些 import 会让 arq-worker 起不来（修 round-2 P1 #4）
- §6 glimpse 验收改成行为驱动（mock activity 单测覆盖语义）+ 线上仅看节奏 / browsing 切换补发，删除"前后 1h 计数 ±5%"（修 round-2 P2 #5；保留 5min 周期 + 即时事件 + 15% 抽样后小窗口计数自然偏差大，频次对齐不可作 SLO）

**v3 关键变化（vs v2）**：
- glimpse 不改纯事件驱动 —— 保留 5min cron 周期触发 + 加 LifeStateChanged "切到 browsing 时" 补一拍即时事件。v2 把 glimpse 改成纯事件驱动是语义错：旧逻辑的本质是"在状态期间反复有概率刷手机"（持续行为），事件驱动只触发切换瞬间一次会丢掉持续期。新方案：5min cron 保持持续刷的人感；切到 browsing 的事件路径仅消除"刚切就要等 5min"的间隙
- §1 业务收益从"glimpse 改事件驱动"改成"切到 browsing 立即响应（消除 5min 调度延迟）"
- §3.2 graph 新增 `GlimpseTick (5min cron) → fan_out_glimpse → GlimpseTickRequest → glimpse_tick_node`（5min 周期路径）+ `LifeStateChanged → glimpse_event_node`（即时路径），两条都汇入 `GlimpseRequest .durable() → run_glimpse_node`
- §3.3 新增 `GlimpseTick` / `GlimpseTickRequest` 两个 Data
- §3.5 `glimpse_node` 拆成两个：`glimpse_tick_node`（5min 周期，内部 `find_latest_life_state` 判 activity；与现状语义一致）+ `glimpse_event_node`（事件路径，仅在切到 browsing 时 emit GlimpseRequest）。"现读 pg" 在 glimpse_tick_node 内部保留 —— 业务上人是否在 sleeping 必须查
- §6 验收：保留 5min 节奏行为对齐 + 加切到 browsing 即时响应一项
- §1 验收点同步修正

**v2 关键变化（vs v1）**：
- §3.0 新增 cron source 宿主设计：main.py lifespan 启 sources-only Runtime（修 round-1 P0 #1，v1 删 ARQ cron 后 cron source 无人启动）
- §2.1 修正 task_executor_job 处理：`cron_jobs = [cron(task_executor_job, minute=None)]` 而非空列表（修 round-1 P0 #2 self-contradiction）
- §3.0 新增 `Source.cron(expr, tz=...)` runtime 小 extension + 全部 cron 表达式声明 `tz="Asia/Shanghai"`（修 round-1 P0 #3，旧设计 cron 表达式按 UTC 跑，daily_plan 偏 8 小时）
- §3.1 Data 类全部改用 `Annotated[str, Key]` + `class Meta: transient = True`（修 round-1 P0 #4，`Key[str]` 不是合法语法 + 缺 transient 会被 migrator 建表）
- §3.2 / §3.3 / §3.5 LifeStateChanged 链路改两段：LifeStateChanged in-process 进 glimpse_node 轻量过滤 + GlimpseRequest **`.durable()`** 走 mq 给 run_glimpse_node（修 round-1 P1 #5，v1 同步链路会让 run_glimpse 的 LLM 调用阻塞 commit_life_state tool）
- §3.3 daily plan 拆成 shared 节点 + fan-out 节点，新增 `SharedDailyContext` Data 在 in-process 传递 wild/anchors/theater（修 round-1 P1 #6，v1 简单拆 per-persona 会让 shared pipeline N 倍开销）
- §6 验收指标改成行为驱动（切到 browsing 立即触发；sleeping 不触发；其他切换 15% 抽样），删除频次对齐（修 round-1 P2 #7）

## 1. 背景

Phase 0+1 落地 runtime 框架 + vectorize；Phase 2 把 safety 收进 graph；Phase 3 落地 `.debounce()` runtime 并把 drift / afterthought 改造成节点。`arq_settings.cron_jobs` 是 dataflow 没接管的最后一片：7 条 cron + `for_each_persona` 轮询 + 一处"现读 pg 判活动状态"的轮询模式（glimpse）。

**业务收益（唯一一条）**: glimpse 切到 browsing 立即响应，消除"刚切到 browsing 还要等下一个 5min 刻度"的最高 5 分钟调度延迟，符合"赤尾是人不是工程系统"。持续期"反复有概率刷手机"的语义保留 5min cron 不变。

**工程收益**: cron 入口 / 扇出 / 业务 node 在 graph 上各自显式，per-persona × per-task 路径独立 —— 跟 Phase 2/3 的取向一致。`for_each_persona` / `prod_only` / `cron_error_handler` 这一套 worker 装饰器消失，统一走 runtime 边语义。

**验收点**:
- `apps/agent-service/app/workers/cron.py` 整文件删除
- `arq_settings.cron_jobs` 仅保留 task_executor_job 一条
- `app/workers/common.py` 的 `for_each_persona` / `prod_only` / `cron_error_handler` 全部删除
- glimpse 5min 周期触发节奏不变；切到 browsing 多一条 LifeStateChanged 即时路径
- `compile_graph()` 通过；agent-service 启动后 cron 节奏与现状一致
- runtime cron source loop 跑在 agent-service 主进程 lifespan（不新增 deployment）

## 2. 现状

### 2.1 `arq_settings.cron_jobs`（apps/agent-service/app/workers/arq_settings.py:95-126）

| # | Cron | Cron 表达式 (CST) | 入口 | 是否 fan-out persona | prod_only | 本期 |
|---|---|---|---|---|---|---|
| 1 | task_executor | `* * * * *` | `task_executor_job` | ❌（轮询表） | ❌ | **不迁** |
| 2 | life_engine_tick | `* * * * *` | `cron_life_engine_tick` → `life.engine.tick` | ✅ | ✅ | 迁 |
| 3 | glimpse | `*/5 * * * *` | `cron_glimpse`（含现读 pg 判 activity） | ✅（内部） | ✅ | 迁（保留 5min 周期 + 加 LifeStateChanged 即时路径） |
| 4 | light_day | `0,30 8-21 * * *` | `cron_memory_reviewer_light_day` | ✅ window=30 | ✅ | 迁 |
| 5 | light_night | `0 22,23,0,1,2,4,5,6,7 * * *` | `cron_memory_reviewer_light_night` | ✅ window=60 | ✅ | 迁 |
| 6 | heavy_review | `0 3 * * *` | `cron_heavy_review` → `run_heavy_review` | ✅（内部） | ✅ | 迁 |
| 7 | daily_plan | `0 5 * * *` | `cron_generate_daily_plan` → `generate_all_daily_plans` | ✅（含 shared pipeline + per-persona） | ✅ | 迁（拆 shared / per-persona） |
| 8 | voice | `0 8-23 * * *` | `cron_generate_voice` → `generate_voice` | ✅ | ✅ | 迁 |

**`task_executor_job` 不在本期范围**（long_tasks 是独立子系统，跟 life/schedule/glimpse 解耦；它仍走 ARQ cron）。其余 7 条全迁。

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

迁移后这三个 helper 消失，由 fan-out node 内嵌 + node 内 try/except 承担。

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

每 5 分钟全量扫所有 persona + 读 `life_state` 判活动。本期保留 5min 周期触发的业务语义（"持续期反复有概率刷"是 glimpse 的本质行为模型，事件驱动无法替代），但把入口从 ARQ cron + `for_each_persona` 内嵌改成 dataflow `Source.cron` + graph fan-out node；`find_latest_life_state` 仍由 glimpse_tick_node 内部调用。

**额外的事件路径**（v3 新增）：当 `commit_life_state_impl` 写入 `life_state` 切到 `browsing` 时，emit 一条 `LifeStateChanged` 事件，glimpse_event_node 即时 emit `GlimpseRequest` —— 这条仅消除"刚切到 browsing 但还要等下个 5min 刻度"的最高 5 分钟延迟。其他状态切换不在事件路径触发，由下个 5min 周期处理。

### 2.4 life_state 写入点（apps/agent-service/app/life/tool.py:84-94）

```python
async def commit_life_state_impl(...) -> CommitResult:
    # validations §9.5
    async with get_session() as s:
        life_state_id = await insert_life_state(s, persona_id=..., activity_type=..., ...)
    return CommitResult(ok=True, is_refresh=is_refresh, life_state_id=...)
```

唯一调用 `insert_life_state` 的入口 —— `commit_life_state` tool（life engine LLM 调用）+ `state_only_refresh`（schedule 更新引发的 refresh）都收敛到这里。`LifeStateChanged` 在这里 emit 是最干净的拦截点。

注意 `is_refresh=True`（段内 refresh，§9.5 Validation 4）时 `activity_type` 与 prev 相同，业务上人没切活动，只是 LLM 重新校准 reasoning —— glimpse 不应该响应。

### 2.5 daily plan 现状（apps/agent-service/app/life/schedule.py:331-353）

```python
async def generate_all_daily_plans(target_date=None):
    wild, anchors, theater = await _run_shared_pipeline(target_date)  # 一次
    persona_ids = await list_all_persona_ids(...)
    for persona_id in persona_ids:
        try:
            await _run_persona_pipeline(persona_id, target_date, wild, anchors, theater)
        except Exception: logger.exception(...)
```

**`_run_shared_pipeline()` 必须每天只跑一次**（wild agents + 真实搜索 + 戏剧化加工的成本和风格一致性都要求 shared）。Phase 4 拆链路时必须保留这个语义。

### 2.6 cron source 当前的运行宿主

`Runtime.run()`（engine.py:132）启动 cron / interval / mq source loop，是 vectorize-worker 等 deployment 的入口（`workers/runtime_entry.py`）。**agent-service 主进程的 FastAPI lifespan（main.py）目前只调 `load_dataflow_graph()` + `declare_durable_topology()` + `start_consumers/start_debounce_consumers/start_chat_consumer` + `register_http_sources`，没有启动任何 cron source loop。**

如果直接删 ARQ cron 而不接管 cron source 启动，整批 wire 静默失效（cron source loop 不存在，下游 fan-out / business node 永不触发）。

## 3. 目标架构

### 3.0 cron source 宿主：main.py lifespan 启 sources-only Runtime

`Runtime.run()` 现行职责包括 (a) migrate schema、(b) start durable consumers、(c) start source loops、(d) block until cancelled。本期 **agent-service 主进程已在 lifespan 里手动做了 a + b + d 等价工作**（migrate 由 PaaS / runtime_entry 在 worker pod 触发；durable consumer 在 main.py:66 启动；FastAPI 自身阻塞驻留）—— 缺的只是 (c) cron source loop。

**方案**: 抽 `Runtime.start_source_loops(app_name)` 出来作为独立 API（不含 migrate / durable consumer / blocking），让 main.py lifespan 调一次。

```python
# main.py lifespan 内（在 register_http_sources 之后）
from app.runtime.engine import Runtime
runtime_for_sources = Runtime(app_name="agent-service", migrate_schema_on_run=False)
await runtime_for_sources.start_source_loops()
# 把 stop hook 挂在 lifespan teardown
```

**为什么不新建 deployment**:
- 一镜像多服务的复杂性（新建 ImageRepo 映射 + chart values + 部署铁律 §4 同步 release）不为本期带来收益
- cron source 本身极轻量（每分钟一次 emit），主进程承担无负担
- fan-out + business node 都在 agent-service 进程，全 in-process emit，不需要任何 mq queue

**改 Runtime API**: 把 `Runtime.run()` 拆成 `start_source_loops()` + `_block()` 两段；`run()` 仍是顶层 entrypoint，给 runtime_entry.py 用。main.py 只调 `start_source_loops`，由 FastAPI 自己驻留。停止逻辑由 lifespan teardown 调 `await runtime.stop_source_loops()`（cancel 所有 source task）。

具体 API：

```python
class Runtime:
    async def start_source_loops(self) -> None:
        """Start cron / interval / mq source loops for nodes bound to this app.

        Also starts a watchdog task that monitors `_source_error`. If any
        source loop hits a fatal error, watchdog re-raises in its own task
        context; main.py lifespan picks this up via task done callback +
        triggers process exit (uvicorn shutdown). Idempotent? No — second
        call without stop_source_loops raises. Migrate / durable consumer
        / block 不在本方法范围。
        """
        # 已有 run() 的 §163-194 段抽出来；末尾追加：
        # self._watchdog_task = asyncio.create_task(self._watch_source_error())
        ...

    async def _watch_source_error(self) -> None:
        """Wait for `_stop_event`; on fire, escalate to process exit.

        Concrete escalation: log fatal error + call `os._exit(1)`.
        sys.exit(1) is not enough because lifespan + uvicorn 不一定能在
        FastAPI worker 内合作干净退出（worker 持有的连接 / 任务）；
        os._exit(1) 立即终止进程，PaaS healthcheck 检测后重启 pod —— 跟
        Runtime.run() 走完 finally + raise 的语义一致，差别只是
        unwind path（lifespan 内不能依赖 Runtime.run() 的 finally）。
        """
        await self._stop_event.wait()
        if self._source_error is not None:
            logger.fatal(
                "runtime: source loop fatal error %r, exiting",
                self._source_error,
            )
            os._exit(1)

    async def stop_source_loops(self) -> None:
        """Cancel + await every source task + watchdog."""
        ...
```

**为什么用 os._exit 而不是 sys.exit / raise**: lifespan 已经 yield 给 FastAPI 主循环，watchdog task 抛异常不会传到 lifespan 调用栈；让 watchdog 主动终止进程是最确定的故障传导路径。代价：跳过 Python 层 cleanup —— 但 source loop fatal 已是异常状态，PaaS 重启 pod 比尝试干净 unwind 更重要。

**测试**:
- 已有 `tests/runtime/test_engine.py` 覆盖 `Runtime.run()`；本期补 `test_start_source_loops_starts_only_sources` + `test_watchdog_exits_on_source_error` 两个单测（后者把 `os._exit` monkeypatch 成可观察的 marker）
- main.py lifespan 单测：mock `Runtime`，断言 `start_source_loops()` 被调用

### 3.1 Source.cron 加 timezone 参数（runtime 小 extension）

现状（runtime/source.py:31）：

```python
@staticmethod
def cron(expr: str) -> SourceSpec:
    return SourceSpec("cron", {"expr": expr})
```

改成：

```python
@staticmethod
def cron(expr: str, *, tz: str = "UTC") -> SourceSpec:
    """tz: IANA zone name (e.g. 'Asia/Shanghai'). Cron expression is
    interpreted in this tz; loop fires at the right wall-clock time.
    """
    return SourceSpec("cron", {"expr": expr, "tz": tz})
```

`engine.py:_source_loop_cron` 改两行：

```python
from zoneinfo import ZoneInfo
tz_name = src.params.get("tz", "UTC")
zone = ZoneInfo(tz_name) if tz_name != "UTC" else UTC
base = datetime.now(tz=zone)
itr = croniter(expr, base)  # croniter 按 base 的 tz 解释 expr
```

**测试**: `test_engine.py` 加 `test_cron_source_respects_tz` —— 用 `tz="America/New_York"` + 一个区分 UTC 和 NY 的小时，断言 emit 时刻命中 NY wall clock。

### 3.2 graph 全图

```
[cron */1 CST]    → MinuteTick           → fan_out_life_tick     → LifeTickRequest    → life_tick_node
[cron */1 CST]    → MinuteTick           → fan_out_voice         (when hour∈8..23, minute=0) → VoiceRequest → voice_node
                                                                  ──────────────
                                          说明：MinuteTick 复用一个 cron source；
                                          fan_out_voice 内部再判时段，避免开两个
                                          cron source 跑相同节奏。
[cron 0,30 8-21 CST]    → LightDayTick    → fan_out_light_day    → LightReviewRequest(window=30)  → light_review_node
[cron 0 22,23,0,1,2,4,5,6,7 CST] → LightNightTick → fan_out_light_night → LightReviewRequest(window=60) → light_review_node
[cron 0 3 CST]    → HeavyReviewTick      → fan_out_heavy         → HeavyReviewRequest  → heavy_review_node
[cron 0 5 CST]    → DailyPlanTick        → run_shared_daily_pipeline_node → SharedDailyContext
                                                                              → fan_out_daily_plan → DailyPlanRequest → daily_plan_node

# Glimpse 双路径：5min 周期 (主) + 切 browsing 即时事件 (补一拍)
[cron */5 CST]    → GlimpseTick          → fan_out_glimpse → GlimpseTickRequest → glimpse_tick_node ──┐
                                                                       (内部读 life_state 判 activity) │
[life.tool.commit_life_state_impl 写入成功]                                                            │
                  → LifeStateChanged (in-process) → glimpse_event_node ─────────────────────────────┤
                              (仅切到 browsing 时触发)                                                  │
                                                                                                       ▼
                                                                           GlimpseRequest .durable() → run_glimpse_node
```

**为什么不复用一个统一的 `PersonaTick(task=...)`**: dataflow 优先用 Data 类型分发（参考 Phase 2 `PreSafetyRequest` / `PostSafetyRequest`、Phase 3 `DriftTrigger` / `AfterthoughtTrigger`）。每条业务链一个类型让 graph 可读、edge 行为独立可调（哪天 voice 要加 `.durable()`、daily_plan 要加 `.debounce()`，类型分发不会牵连别的链）。重复的只是 fan-out 模板，几行。

**为什么不复用一个 `Tick`**: cron source 在 engine 里以 `data_type` 为键挂 source loop（engine.py:174-185），同一个 `Tick` 类型不能同时挂多个不同 cron 表达式。每种频率独立 Tick 类型。

**MinuteTick 例外**: voice 和 life_tick 都是 1 分钟节奏，复用同一个 MinuteTick + 各自 fan-out 节点用 if/return 过滤即可。

**GlimpseRequest 必须 `.durable()`**: 两条上游路径（5min cron 周期 / LifeStateChanged 即时事件）汇入同一条 GlimpseRequest 边。run_glimpse_node 内部走 LLM 调用，必须在 mq durable consumer 异步跑：
- 5min cron 路径：fan_out_glimpse 是 graph 上独立路径，本身可 in-process emit GlimpseTickRequest 给 glimpse_tick_node；glimpse_tick_node 判完 activity 后 emit GlimpseRequest 走 mq
- LifeStateChanged 路径：在 commit_life_state_impl 调用栈里，重活同步跑会阻塞 langchain tool；GlimpseRequest 走 mq 解耦

跟 Phase 2 PostSafetyRequest 范式一致。

### 3.3 Data 类（新建 `app/domain/life_dataflow.py`）

```python
from typing import Annotated
from app.runtime.data import Data, Key

# Cron tick 入口（5 种频率独立类型）

class MinuteTick(Data):
    ts: Annotated[str, Key]
    class Meta: transient = True

class LightDayTick(Data):
    ts: Annotated[str, Key]
    class Meta: transient = True

class LightNightTick(Data):
    ts: Annotated[str, Key]
    class Meta: transient = True

class HeavyReviewTick(Data):
    ts: Annotated[str, Key]
    class Meta: transient = True

class DailyPlanTick(Data):
    ts: Annotated[str, Key]
    class Meta: transient = True

class GlimpseTick(Data):
    ts: Annotated[str, Key]
    class Meta: transient = True

# Per-persona business request

class LifeTickRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str
    class Meta: transient = True

class VoiceRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str
    class Meta: transient = True

class LightReviewRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str
    window_minutes: int
    class Meta: transient = True

class HeavyReviewRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str
    class Meta: transient = True

class GlimpseTickRequest(Data):
    """5min 周期 fan-out 出的 per-persona 触发；下游 glimpse_tick_node 内部判 activity。"""
    persona_id: Annotated[str, Key]
    ts: str
    class Meta: transient = True

# Shared daily-plan context (in-process only, transient)

class SharedDailyContext(Data):
    target_date: Annotated[str, Key]   # YYYY-MM-DD
    wild_materials: str
    search_anchors: str
    theater: str
    class Meta: transient = True

class DailyPlanRequest(Data):
    persona_id: Annotated[str, Key]
    target_date: str
    wild_materials: str       # in-process emit, payload 直接带
    search_anchors: str
    theater: str
    class Meta: transient = True

# Event-driven glimpse 链路

class LifeStateChanged(Data):
    persona_id: Annotated[str, Key]
    activity_type: str
    prev_activity_type: str    # "" 表示首次
    ts: str
    class Meta: transient = True

class GlimpseRequest(Data):
    """走 .durable() 跨进程，runtime 自动建 glimpse_requests 表做 dedup。

    request_id 是 emit 端生成的 uuid —— 同一条 mq message 在 redelivery
    时复用 emit 端 payload（含 request_id），insert_idempotent 拒绝
    第二次插入 → run_glimpse 不会被同一 request 跑两次。同时这张表
    顺便给 glimpse 触发提供历史审计。
    """
    request_id: Annotated[str, Key]   # uuid4
    persona_id: str
    chat_id: str
    ts: str
    trigger_kind: str                  # "tick" | "event"，便于审计区分两路触发
    # 没有 Meta.transient —— 这是 durable 边的硬约束（runtime graph.py:286）
```

`prev_activity_type` 为空字符串表示首次提交 life_state；非空表示前一段的 activity。`glimpse_node` 用 `c.activity_type != c.prev_activity_type` 内部判断（也可放 wire 层 `.when()`，但 node 内部更显式）。

**Meta.transient = True 全部加上**：所有这些 Data 都是调度信号 / 请求载荷，不持久化。否则 runtime migrator 会按 DATA_REGISTRY 给它们建 pg 表。

### 3.4 Wire 注册（新建 `app/wiring/life_dataflow.py`）

```python
from app.runtime import Source, wire
from app.domain.life_dataflow import (
    MinuteTick, LightDayTick, LightNightTick, HeavyReviewTick, DailyPlanTick, GlimpseTick,
    LifeTickRequest, VoiceRequest, LightReviewRequest, HeavyReviewRequest, GlimpseTickRequest,
    SharedDailyContext, DailyPlanRequest,
    LifeStateChanged, GlimpseRequest,
)
from app.nodes.life_dataflow import (
    fan_out_life_tick, fan_out_voice,
    fan_out_light_day, fan_out_light_night,
    fan_out_heavy,
    run_shared_daily_pipeline_node, fan_out_daily_plan,
    fan_out_glimpse,
    life_tick_node, voice_node,
    light_review_node, heavy_review_node, daily_plan_node,
    glimpse_tick_node, glimpse_event_node, run_glimpse_node,
)

# Cron tick 入口
TZ = "Asia/Shanghai"
wire(MinuteTick).from_(Source.cron("* * * * *", tz=TZ)).to(fan_out_life_tick, fan_out_voice)
wire(LightDayTick).from_(Source.cron("0,30 8-21 * * *", tz=TZ)).to(fan_out_light_day)
wire(LightNightTick).from_(Source.cron("0 22,23,0,1,2,4,5,6,7 * * *", tz=TZ)).to(fan_out_light_night)
wire(HeavyReviewTick).from_(Source.cron("0 3 * * *", tz=TZ)).to(fan_out_heavy)
wire(DailyPlanTick).from_(Source.cron("0 5 * * *", tz=TZ)).to(run_shared_daily_pipeline_node)
wire(GlimpseTick).from_(Source.cron("*/5 * * * *", tz=TZ)).to(fan_out_glimpse)

# Daily plan 内部链
wire(SharedDailyContext).to(fan_out_daily_plan)
wire(DailyPlanRequest).to(daily_plan_node)

# Per-persona business
wire(LifeTickRequest).to(life_tick_node)
wire(VoiceRequest).to(voice_node)
wire(LightReviewRequest).to(light_review_node)
wire(HeavyReviewRequest).to(heavy_review_node)

# Glimpse 双路径汇入 GlimpseRequest
wire(GlimpseTickRequest).to(glimpse_tick_node)         # 5min 周期路径，内部判 activity
wire(LifeStateChanged).to(glimpse_event_node)          # 即时路径，仅切到 browsing 触发
wire(GlimpseRequest).to(run_glimpse_node).durable()    # 重活走 mq
```

**注**: `wire(MinuteTick).to(fan_out_life_tick, fan_out_voice)` 让两条 fan-out 共享同一个 cron source（同 WireSpec 多 consumer，fan-out 节点内部用 if/return 过滤自己关心的时段）。

### 3.5 Node 实现（新建 `app/nodes/life_dataflow.py`）

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from app.runtime import node, emit
from app.infra.config import settings
from app.data.queries import list_all_persona_ids
from app.data.session import get_session

CST = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)

def _is_prod() -> bool:
    return not (settings.lane and settings.lane != "prod")

async def _list_persona_ids() -> list[str]:
    async with get_session() as s:
        return await list_all_persona_ids(s)


async def _fan_out_per_persona(label: str, build_request) -> None:
    """通用 fan-out：包住 list_persona_ids + emit 循环，所有异常 log 不冒泡。

    DB 抖动 / emit 失败一律不扔回 source loop —— 否则 `_record_source_error`
    会让进程退出。fan-out 失败的代价是这一拍丢，下一拍自然恢复。
    """
    try:
        pids = await _list_persona_ids()
    except Exception:
        logger.exception("%s: list_persona_ids failed", label)
        return
    for pid in pids:
        try: await emit(build_request(pid))
        except Exception: logger.exception("[%s] %s fan-out failed", pid, label)

@node
async def fan_out_life_tick(t: MinuteTick) -> None:
    if not _is_prod(): return
    await _fan_out_per_persona(
        "life_tick", lambda pid: LifeTickRequest(persona_id=pid, ts=t.ts)
    )

@node
async def fan_out_voice(t: MinuteTick) -> None:
    if not _is_prod(): return
    cst_ts = datetime.fromisoformat(t.ts).astimezone(CST)
    if cst_ts.hour not in range(8, 24): return
    if cst_ts.minute != 0: return  # voice 整点触发
    await _fan_out_per_persona(
        "voice", lambda pid: VoiceRequest(persona_id=pid, ts=t.ts)
    )

@node
async def fan_out_light_day(t: LightDayTick) -> None:
    if not _is_prod(): return
    await _fan_out_per_persona(
        "light_day",
        lambda pid: LightReviewRequest(persona_id=pid, ts=t.ts, window_minutes=30),
    )

@node
async def fan_out_light_night(t: LightNightTick) -> None:
    if not _is_prod(): return
    await _fan_out_per_persona(
        "light_night",
        lambda pid: LightReviewRequest(persona_id=pid, ts=t.ts, window_minutes=60),
    )

@node
async def fan_out_heavy(t: HeavyReviewTick) -> None:
    if not _is_prod(): return
    await _fan_out_per_persona(
        "heavy", lambda pid: HeavyReviewRequest(persona_id=pid, ts=t.ts)
    )

# Daily plan: shared 节点先跑一次 → SharedDailyContext → fan-out per-persona

@node
async def run_shared_daily_pipeline_node(t: DailyPlanTick) -> SharedDailyContext | None:
    if not _is_prod(): return None
    from app.life.schedule import _run_shared_pipeline
    target_date = datetime.now(CST).date()
    wild, anchors, theater = await _run_shared_pipeline(target_date)
    return SharedDailyContext(
        target_date=target_date.isoformat(),
        wild_materials=wild,
        search_anchors=anchors or "",
        theater=theater,
    )

@node
async def fan_out_daily_plan(c: SharedDailyContext) -> None:
    await _fan_out_per_persona(
        "daily_plan",
        lambda pid: DailyPlanRequest(
            persona_id=pid,
            target_date=c.target_date,
            wild_materials=c.wild_materials,
            search_anchors=c.search_anchors,
            theater=c.theater,
        ),
    )


# Per-persona business node — 都是薄壳调原函数

@node
async def life_tick_node(r: LifeTickRequest) -> None:
    from app.life.engine import tick
    try: await tick(r.persona_id)
    except Exception: logger.exception("[%s] life_tick failed", r.persona_id)

@node
async def voice_node(r: VoiceRequest) -> None:
    from app.memory.voice import generate_voice
    try: await generate_voice(r.persona_id)
    except Exception: logger.exception("[%s] voice failed", r.persona_id)

@node
async def light_review_node(r: LightReviewRequest) -> None:
    from app.memory.reviewer.light import run_light_review
    try: await run_light_review(persona_id=r.persona_id, window_minutes=r.window_minutes)
    except Exception: logger.exception("[%s] light_review failed", r.persona_id)

@node
async def heavy_review_node(r: HeavyReviewRequest) -> None:
    from app.memory.reviewer.heavy import run_heavy_review_for_persona
    try: await run_heavy_review_for_persona(r.persona_id)
    except Exception: logger.exception("[%s] heavy_review failed", r.persona_id)

@node
async def daily_plan_node(r: DailyPlanRequest) -> None:
    from datetime import date as _date
    from app.life.schedule import _run_persona_pipeline
    try:
        await _run_persona_pipeline(
            r.persona_id,
            _date.fromisoformat(r.target_date),
            r.wild_materials,
            r.search_anchors,
            r.theater,
        )
    except Exception:
        logger.exception("[%s] daily_plan failed", r.persona_id)


# Glimpse 双路径：5min 周期 fan-out + 即时事件，都汇入 GlimpseRequest

import uuid

def _new_glimpse_request(persona_id: str, chat_id: str, ts: str, kind: str) -> GlimpseRequest:
    """统一构造 GlimpseRequest —— request_id 是 emit 端生成的 uuid4，
    durable consumer redelivery 时复用同一 id 让 insert_idempotent 拒重。"""
    return GlimpseRequest(
        request_id=str(uuid.uuid4()),
        persona_id=persona_id,
        chat_id=chat_id,
        ts=ts,
        trigger_kind=kind,
    )

@node
async def fan_out_glimpse(t: GlimpseTick) -> None:
    """5min cron → 对每个 persona emit GlimpseTickRequest。"""
    if not _is_prod(): return
    await _fan_out_per_persona(
        "glimpse_tick", lambda pid: GlimpseTickRequest(persona_id=pid, ts=t.ts)
    )

@node
async def glimpse_tick_node(r: GlimpseTickRequest) -> None:
    """5min 周期路径：读 life_state 判 activity，决定要不要 emit GlimpseRequest。

    业务语义跟现状 cron_glimpse 完全一致：sleeping 跳过；browsing 必发；
    其他活动 15% 概率发。读 pg 失败按"这拍跳过"处理，下一拍恢复。"""
    from app.data.queries import find_latest_life_state
    from app.life.glimpse import list_target_groups
    try:
        async with get_session() as s:
            state = await find_latest_life_state(s, r.persona_id)
    except Exception:
        logger.exception("[%s] glimpse_tick read life_state failed", r.persona_id)
        return
    activity = state.activity_type if state else ""
    if activity == "sleeping": return
    if activity != "browsing" and random.random() >= 0.15: return
    for chat_id in list_target_groups():
        try:
            await emit(_new_glimpse_request(r.persona_id, chat_id, r.ts, "tick"))
        except Exception:
            logger.exception("[%s][%s] glimpse_tick emit failed", r.persona_id, chat_id)

@node
async def glimpse_event_node(c: LifeStateChanged) -> None:
    """即时路径：仅在切到 browsing 瞬间补一拍 GlimpseRequest。

    其他状态切换（如切到 working / sleeping）不在事件路径触发 ——
    "持续期反复刷"由 5min cron 路径承担。"""
    if not _is_prod(): return
    if c.activity_type != "browsing": return
    if c.activity_type == c.prev_activity_type: return  # 段内 refresh 不响应
    from app.life.glimpse import list_target_groups
    for chat_id in list_target_groups():
        try:
            await emit(_new_glimpse_request(c.persona_id, chat_id, c.ts, "event"))
        except Exception:
            logger.exception("[%s][%s] glimpse_event emit failed", c.persona_id, chat_id)

@node
async def run_glimpse_node(r: GlimpseRequest) -> None:
    """LLM 重活，走 .durable() consumer。两条上游路径汇入这里。"""
    from app.life.glimpse import run_glimpse
    try: await run_glimpse(r.persona_id, r.chat_id)
    except Exception: logger.exception("[%s][%s] run_glimpse failed", r.persona_id, r.chat_id)
```

**为什么所有 node 都套薄壳调原函数而不是把业务搬进来**：本期是调度层迁移，不是业务重写。`tick / generate_voice / run_light_review / run_heavy_review_for_persona / _run_persona_pipeline / run_glimpse` 函数语义不动。后续 Phase 5/6 在重写 chat / 清扫 bridges 时再回头看这些 node 该不该继续薄壳。

**heavy_review 入口改名**：`run_heavy_review()` 内部本就是 `for_each_persona(run_heavy_review_for_persona, ...)`（`memory/reviewer/heavy.py:105`）。本期把 `run_heavy_review_for_persona` 提为公开入口，graph fan-out 直接调它；`run_heavy_review()` 删除（被 fan-out 替代）。

**daily_plan_node 复用 `_run_persona_pipeline`**：现有函数已经是 per-persona 接口。本期把它从 module-private 改 module-public（去掉前缀下划线，或者保留下划线但 wiring 内部 import —— 倾向后者，避免改 import 范围）。`generate_all_daily_plans` 删除；`generate_daily_plan(persona_id, target_date)` 这个公开 admin trigger 保留（admin API / CLI 用，仍走 `_run_shared_pipeline + _run_persona_pipeline` 串行）。

### 3.6 LifeStateChanged 触发点（修改 `app/life/tool.py`）

```python
# commit_life_state_impl 在 insert_life_state 成功后追加：
async with get_session() as s:
    life_state_id = await insert_life_state(s, ...)

# Emit event for event-driven downstream (e.g. glimpse).
prev_activity = (prev_state.activity_type if prev_state else "") or ""
try:
    from app.runtime import emit
    from app.domain.life_dataflow import LifeStateChanged
    await emit(LifeStateChanged(
        persona_id=persona_id,
        activity_type=activity_type,
        prev_activity_type=prev_activity,
        ts=now.isoformat(),
    ))
except Exception:
    logger.exception("[%s] LifeStateChanged emit failed; commit succeeded", persona_id)
```

**emit 失败处理**: `emit` 的 in-process 段（glimpse_event_node）抛异常会冒泡。`commit_life_state_impl` 在 langchain tool 调用栈里 —— 抛异常会让 tool 报错触发 life engine 重试可能双 insert。`try/except` 包住 emit 让"事件丢失但状态成功"是 best-effort 语义；即使事件路径丢一拍，5min cron 路径也会很快补上一次（最多延迟 5min），跟 glimpse 业务关键性匹配。glimpse_event_node 仅做轻量过滤 + per-chat emit，不会抛重异常；run_glimpse 的 LLM 重活在 durable consumer 里跑，根本不在 emit 这层调用栈。

### 3.7 prod_only 处理

每个 fan-out 节点首行 `if not _is_prod(): return`（settings.lane 非空且 ≠ "prod" 时返回）。glimpse_node / run_shared_daily_pipeline_node 同样首行判。`_is_prod()` helper 放 `app/nodes/life_dataflow.py` 顶部。

**为什么不在 wire 层加 `.when(prod_only)`**: dataflow 倾向 wire 描述拓扑、predicate 描述业务过滤。"是否在 prod 跑"是部署关切，写在 node 内部更易读、易在测试里临时打开。

**为什么 dev 泳道仍跑 cron source loop**: cron source 在所有泳道启动。dev 泳道触发后 fan-out 节点直接 return，没有 emit PersonaXxxRequest，业务 node 不会跑。代价：dev 每分钟一次 fan-out 进入即返回。可接受。

## 4. 删除项

迁完所有 wire 后立即删（不留兼容 shim）：

- `apps/agent-service/app/workers/cron.py`（整文件）
- `apps/agent-service/app/workers/common.py`：`for_each_persona` / `prod_only` / `cron_error_handler` 三个 helper（保留 `mq_error_handler`）
- `apps/agent-service/app/workers/arq_settings.py`：
  - `cron_jobs = [cron(task_executor_job, minute=None)]`（保留 task_executor + functions=[sync_life_state_after_schedule]）
  - **删除顶部对 `cron_*` 的 import**（`cron_generate_voice` / `cron_glimpse` / `cron_heavy_review` / `cron_life_engine_tick` / `cron_memory_reviewer_light_*` / `cron_generate_daily_plan`）—— `cron.py` 删除后这些 import 让 arq-worker 起不来
  - **删除 `on_startup` 里 `asyncio.create_task(cron_generate_voice(None))`**：seed voice 由 graph 接管。若需要保留"启动时 seed 一次 voice"行为，由 main.py lifespan 在 `start_source_loops()` 之后追加一次性触发（对所有 persona emit `VoiceRequest`）；本期默认不 seed，等下个整点
- `apps/agent-service/app/life/schedule.py::generate_all_daily_plans`（被 graph fan-out 替代）
- `apps/agent-service/app/memory/reviewer/heavy.py::run_heavy_review`（无参循环 persona 形态被 graph fan-out 替代；`run_heavy_review_for_persona` 保留）

## 5. 部署 / 风险

### 5.1 部署影响

- **agent-service 重启**: cron source loop 在 lifespan startup 阶段挂起。部署 = 杀 Pod = cron 当前分钟那一拍可能丢（同 ARQ cron 现状）。daily_plan / heavy_review 这种小时级以上节奏天然容忍。
- **arq-worker 角色变小**: 本期后 arq-worker 仅承担 task_executor（每分钟轮询 long_tasks 表）+ sync_life_state_after_schedule 事件 worker。重启 arq-worker 不再影响 life/glimpse/voice/review/daily-plan。
- **glimpse 业务语义变化**: 5min 周期触发 + 内部判 activity 的逻辑完全保留（与现状 `cron_glimpse` 等价）；额外加一条 LifeStateChanged 即时路径仅在切到 browsing 时触发一次（消除"刚切到 browsing 就要等下个 5min"的最高 5min 调度延迟）。频次相比现状仅在 browsing 切入瞬间增加一次额外触发，其他时刻不变。

### 5.2 可观测性

每个 fan-out node + 业务 node 都是 graph 上独立 callable，runtime 自带 emit 异常 log（emit.py 注释明确"in-process dispatch is strict"，但本期 fan-out / business node 都自包 try/except，避免单 persona 失败阻塞整批）。

`run_glimpse` 走 `.durable()` mq consumer，PR #202 已给 dataflow runtime queues + DLQ 加了告警 —— GlimpseRequest queue 自动并入这套监控。其他 cron 都是 in-process（无 `.durable()`），不进 mq。

### 5.3 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| `commit_life_state_impl` emit 失败 | LifeStateChanged 丢，glimpse 这一次不响应 | try/except 包；日志报警；下次 state 切换会再有事件 |
| fan-out 节点 `list_all_persona_ids` 撞 db 异常 | 这一拍整批 persona 不触发 | fan-out 节点本身包 try/except；emit 单 persona 失败用内部 try/except 隔离；source loop 不退出 |
| Source.cron tz 逻辑 bug | cron 节奏漂移到错时区 | 加 unit test 覆盖 `tz="America/New_York"` 等非 UTC 情况；泳道部署后用 `make logs APP=agent-service KEYWORD=fan_out` 观察首次触发时刻 |
| main.py lifespan 启 source loop 后 FastAPI 启动失败 | cron 起不来同时 web 服务也起不来 | start_source_loops 内部出错冒泡到 lifespan，FastAPI 不会以"web 健康但 cron 死"的姿态启动 |
| GlimpseRequest queue 拥塞 / DLQ 累积 | 部分 glimpse 不发 | PR #202 监控覆盖；run_glimpse 单条耗时本来就秒级，不易拥塞 |
| daily_plan shared pipeline 失败 | 当天所有 persona 都不出 plan | run_shared_daily_pipeline_node 内部异常会让 emit 不出 SharedDailyContext，per-persona 链整体不触发；与现状 `generate_all_daily_plans` 在 shared 失败时整体失败一致 |
| cutover PR 太大 reviewer 难审 | review 拖延 / 漏看 | 7 条 cron 改一个 PR；按"先建后切再删"提交：(1) Source.cron tz 参数 + start_source_loops API；(2) 加 Data + nodes + wires + LifeStateChanged 触发；(3) `arq_settings.cron_jobs` 缩到 task_executor；(4) 删 cron.py + helpers + run_heavy_review + generate_all_daily_plans。每个 commit 单独可读 |

### 5.4 测试策略

- `tests/runtime/test_engine.py` 加 `test_cron_source_respects_tz` + `test_start_source_loops_starts_only_sources`
- 每个 fan-out / 业务 node 一个 happy + 一个 prod_only 跳过 + 一个 emit 失败/异常路径单测
- compile_graph 测试：load `app/wiring/__init__.py` 后 `compile_graph()` 通过；新增 wire 都在
- 集成测试：mock `Source.cron` 改 `Source.interval(0.1)`，跑 ~1s 验证 fan-out 节点确实触发对应 PersonaXxxRequest
- LifeStateChanged 链路测试：调 `commit_life_state_impl` 用 in-memory db，断言 emit 出 LifeStateChanged + glimpse_node 触发 + 段内 refresh 不触发
- 泳道验证（feat-flow-parse-4）：部署 + `make logs APP=agent-service KEYWORD=fan_out_voice` 观察整点 voice 触发 + `make logs APP=agent-service KEYWORD=fan_out_life_tick` 观察 life tick 每分钟节奏 + 用 dev bot 触发一次 commit_life_state 观察 LifeStateChanged → glimpse → run_glimpse 全链路

## 6. 验收 checklist

- [ ] `app/workers/cron.py` 不存在
- [ ] `app/workers/common.py` 没有 `for_each_persona` / `prod_only` / `cron_error_handler` 三个名字
- [ ] `arq_settings.WorkerSettings.cron_jobs == [cron(task_executor_job, minute=None)]`（grep 验证）
- [ ] `app/wiring/life_dataflow.py` 存在；wire 数量为 15（6 cron tick [Minute/LightDay/LightNight/HeavyReview/DailyPlan/Glimpse] + SharedDailyContext + DailyPlanRequest + 4 PersonaXxxRequest [LifeTick/Voice/LightReview/HeavyReview] + GlimpseTickRequest + LifeStateChanged + GlimpseRequest = 6+1+1+4+1+1+1）
- [ ] `app/nodes/life_dataflow.py` 存在；@node 数量为 16（6 fan-out [life_tick/voice/light_day/light_night/heavy/glimpse] + run_shared_daily_pipeline_node + fan_out_daily_plan + 5 business [life_tick/voice/light_review/heavy_review/daily_plan] + glimpse_tick_node + glimpse_event_node + run_glimpse_node = 6+1+1+5+3）
- [ ] `app/life/tool.py::commit_life_state_impl` 末尾 emit `LifeStateChanged`，try/except 包
- [ ] `app/life/schedule.py::generate_all_daily_plans` 不存在；`_run_shared_pipeline` / `_run_persona_pipeline` 保留（被 graph node 调用）
- [ ] `app/memory/reviewer/heavy.py::run_heavy_review` 不存在；`run_heavy_review_for_persona` 保留
- [ ] `find_latest_life_state` 调用点：现状 `cron_glimpse` 那处删除；新点出现在 `glimpse_tick_node` —— 业务上等价转移，不是真的"消灭"。grep 数量增减 ±1 在预期内
- [ ] `app/main.py` lifespan 调 `Runtime.start_source_loops()`；`Runtime.stop_source_loops()` 在 teardown 调
- [ ] `app/runtime/source.py::Source.cron(expr, *, tz="UTC")` 签名落地；engine.py cron loop 用 ZoneInfo
- [ ] compile_graph 通过；agent-service 启动日志显示 6 个 cron sources（命名 `cron[MinuteTick]` / `cron[LightDayTick]` / `cron[LightNightTick]` / `cron[HeavyReviewTick]` / `cron[DailyPlanTick]` / `cron[GlimpseTick]`）
- [ ] 泳道部署 30 分钟观察：life_state 写入节奏与现状一致；voice 整点触发命中（用 `make logs APP=agent-service KEYWORD=voice_node SINCE=15m`）；light reviewer 节奏命中；glimpse 5min 周期触发命中（`KEYWORD=glimpse_tick_node`）
- [ ] 泳道用 dev bot 触发一次 life_state 切到 browsing → 观察 LifeStateChanged → glimpse_event_node → GlimpseRequest 进 mq → run_glimpse_node 在 durable consumer 跑
- [ ] glimpse 行为验收（语义由单测保证；线上仅看节奏 / 切换）:
  - 单测 mock activity：sleeping 跳过 / browsing 100% emit / 其他活动按 random seed 命中或不命中（注入可控 random）
  - 单测 LifeStateChanged 触发：切到 browsing emit 一次；切到 working / sleeping 不 emit；段内 refresh（activity 与 prev 同）不 emit
  - 线上：5min 周期触发可观测（`make logs APP=agent-service KEYWORD=glimpse_tick_node SINCE=15m` 至少 3 次记录）
  - 线上：dev bot 触发一次切到 browsing 后，5min 内必有一次额外的 `glimpse_event_node` log（不需要等 5min cron 刻度）
- [ ] `glimpse_requests` 表自动建出（runtime migrator 跑过）；durable consumer redelivery 测试：手动 nack + redeliver 同一条 message，确认 run_glimpse_node 仅执行一次（insert_idempotent dedup 命中）
- [ ] watchdog 行为：单测注入 source loop fatal error → 断言 `os._exit(1)` 被调用（monkeypatch）
- [ ] arq-worker 启动正常：去掉 `cron.py` 后 arq-worker pod 不 import fail（`/ops pods APP=arq-worker` 状态 Running）

## 7. 不在本期范围

- `task_executor_job`（long_tasks 子系统）—— 保留 ARQ cron
- 任何 fan-out 节点加 `.durable()` —— 这些任务都"丢一拍可接受"，不值上 mq；只有 GlimpseRequest 因为下游有 LLM 重活才 durable
- `glimpse_requests` 表的 retention/cleanup 策略 —— durable Data 的副产品。目前先让表自然增长（每天约 N persona × ~100 tick × M chat 量级），观察一段时间后再决定 TTL / 老化清理；可由本 Phase 引入的 dataflow cron 机制自身实现（讽刺循环）
- glimpse 改纯事件驱动（消灭 5min 周期）—— v2 草案错误尝试过，丢失"持续期反复刷"语义。本期保留 5min cron + 加事件路径补一拍；后续若把 LifeState 迁到 dataflow Data + 引入"in-state timer"原语，再考虑彻底取消 cron 周期
- `with_latest(LifeState)` 接入 —— 后续若发现 glimpse_tick_node 需要 prev state 的更多字段，再补
- chat 主 pipeline / Stream[T] / bridges 整包删 —— Phase 5 / 6
- 新建 scheduler deployment —— 一镜像多服务暂不引入；cron source 跑在 agent-service 主进程
