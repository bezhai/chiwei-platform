# Dataflow Phase 4 — Life Engine / Schedule / Glimpse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `arq_settings.cron_jobs` 里的 7 条 cron（life_engine_tick / glimpse / light_day / light_night / heavy_review / daily_plan / voice）迁到 dataflow `Source.cron` + graph fan-out node；glimpse 在保留 5min 周期触发的同时新增 LifeStateChanged 即时事件路径（切到 browsing 立即响应）。

**Architecture:** 5 cron sources → 6 fan-out node → per-persona request → 5 business node + 1 daily-plan shared pipeline；glimpse 双路径汇入 `GlimpseRequest .durable() → run_glimpse_node`。cron source loop 跑在 agent-service 主进程 lifespan（不新增 deployment），watchdog task 监 source error 触发 `os._exit(1)`。

**Tech Stack:** Python (uv), pytest, FastAPI lifespan, croniter + ZoneInfo, RabbitMQ (durable consumers), PostgreSQL (runtime migrator), pydantic Data + dataclass.

**Spec:** `docs/superpowers/specs/2026-04-30-dataflow-phase-4-life-engine-glimpse-design.md` (v5)

**File map:**

- Modify `apps/agent-service/app/runtime/source.py` — Source.cron 加 `tz` 参数
- Modify `apps/agent-service/app/runtime/engine.py` — cron loop 用 ZoneInfo + 拆出 `start_source_loops` / `stop_source_loops` + watchdog
- Create `apps/agent-service/app/domain/life_dataflow.py` — 14 个 Data 类
- Modify `apps/agent-service/app/life/schedule.py` — `_run_persona_pipeline` / `_run_shared_pipeline` 提公开（去下划线 / 不下划线但 wiring 直接 import）；删 `generate_all_daily_plans`
- Modify `apps/agent-service/app/memory/reviewer/heavy.py` — `run_heavy_review_for_persona` 提公开；删 `run_heavy_review`
- Create `apps/agent-service/app/nodes/life_dataflow.py` — 16 个 @node + helpers
- Create `apps/agent-service/app/wiring/life_dataflow.py` — 15 wire 注册
- Modify `apps/agent-service/app/wiring/__init__.py` — import 新 wiring 模块
- Modify `apps/agent-service/app/life/tool.py` — `commit_life_state_impl` 末尾 emit `LifeStateChanged`
- Modify `apps/agent-service/app/main.py` — lifespan 调 `migrate_schema` + `start_source_loops` + teardown
- Modify `apps/agent-service/app/workers/arq_settings.py` — `cron_jobs` 缩到 task_executor + 删 cron_* import + 删 startup seed voice
- Delete `apps/agent-service/app/workers/cron.py`
- Modify `apps/agent-service/app/workers/common.py` — 删 `for_each_persona` / `prod_only` / `cron_error_handler`
- Create `apps/agent-service/tests/runtime/test_engine_phase4.py` — Source.cron tz + start_source_loops + watchdog
- Create `apps/agent-service/tests/nodes/test_life_dataflow.py` — fan-out / business / glimpse node 单测
- Create `apps/agent-service/tests/wiring/test_life_dataflow_wiring.py` — compile_graph 通过 + wire 数量
- Create `apps/agent-service/tests/life/test_tool_emit.py` — commit_life_state_impl emit LifeStateChanged

---

## Task 1: Source.cron 加 timezone 参数

**Files:**
- Modify: `apps/agent-service/app/runtime/source.py`
- Modify: `apps/agent-service/app/runtime/engine.py`
- Test: `apps/agent-service/tests/runtime/test_engine_phase4.py`

- [ ] **Step 1: 写失败测试 — Source.cron tz 参数被尊重**

Create `apps/agent-service/tests/runtime/test_engine_phase4.py`:

```python
"""Phase 4 runtime extensions: Source.cron tz + start_source_loops + watchdog."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.runtime.data import Data, Key
from app.runtime.emit import reset_emit_runtime
from app.runtime.engine import Runtime
from app.runtime.node import node
from app.runtime.placement import clear_bindings
from app.runtime.source import Source, SourceSpec
from app.runtime.wire import clear_wiring, wire


def test_source_cron_default_tz_is_utc():
    spec = Source.cron("* * * * *")
    assert isinstance(spec, SourceSpec)
    assert spec.kind == "cron"
    assert spec.params["expr"] == "* * * * *"
    assert spec.params["tz"] == "UTC"


def test_source_cron_accepts_tz():
    spec = Source.cron("0 5 * * *", tz="Asia/Shanghai")
    assert spec.params["tz"] == "Asia/Shanghai"
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_engine_phase4.py -v`
Expected: FAIL（current Source.cron 不接受 tz kwarg）

- [ ] **Step 3: 改 source.py 加 tz 参数**

Edit `apps/agent-service/app/runtime/source.py`, replace the `cron` staticmethod:

```python
    @staticmethod
    def cron(expr: str, *, tz: str = "UTC") -> SourceSpec:
        """5-field cron expression. ``tz``: IANA zone name
        (e.g. 'Asia/Shanghai'); the loop fires at the right wall-clock
        time in that zone. ``croniter.get_next`` is absolute-time based.
        """
        return SourceSpec("cron", {"expr": expr, "tz": tz})
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_engine_phase4.py::test_source_cron_default_tz_is_utc tests/runtime/test_engine_phase4.py::test_source_cron_accepts_tz -v`
Expected: 2 passed

- [ ] **Step 5: 写测试 — engine cron loop 用 tz**

Append to `tests/runtime/test_engine_phase4.py`:

```python
class _TzTick(Data):
    ts: Annotated[str, Key]


_tz_emitted: list[_TzTick] = []


@node
async def _record_tz_tick(t: _TzTick) -> None:
    _tz_emitted.append(t)


@pytest.mark.asyncio
async def test_cron_source_uses_declared_tz(monkeypatch):
    """croniter base must be in the declared tz so cron expressions are
    interpreted at the right wall clock."""
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _tz_emitted.clear()

    captured: dict = {}

    def fake_croniter(expr, base):
        captured["base"] = base
        captured["expr"] = expr
        # Stub croniter that returns a single far-future tick so the loop
        # awaits and the test cancels it.
        class _Iter:
            def get_next(self, _t):
                return base.replace(year=base.year + 1)
        return _Iter()

    monkeypatch.setattr("croniter.croniter", fake_croniter)

    wire(_TzTick).from_(Source.cron("0 5 * * *", tz="Asia/Shanghai")).to(_record_tz_tick)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    # Use start_source_loops once Task 2 lands; for now invoke private
    # helper to validate tz wiring. Rewrite this test in Task 2.
    # NOTE: this assertion requires _source_loop_cron to honor tz.
    task = asyncio.create_task(rt._source_loop_cron(
        next(w for w in __import__("app.runtime.graph", fromlist=["compile_graph"]).compile_graph().wires if w.data_type is _TzTick),
        Source.cron("0 5 * * *", tz="Asia/Shanghai"),
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

    assert captured["base"].tzinfo == ZoneInfo("Asia/Shanghai")
```

- [ ] **Step 6: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_engine_phase4.py::test_cron_source_uses_declared_tz -v`
Expected: FAIL（current `_source_loop_cron` 用 `datetime.now(tz=UTC)`）

- [ ] **Step 7: 改 engine.py 用 ZoneInfo**

Edit `apps/agent-service/app/runtime/engine.py`, replace lines around 263-292 (`_source_loop_cron`):

```python
    async def _source_loop_cron(self, w: WireSpec, src: SourceSpec) -> None:
        """Fire ``emit()`` for ``w`` each time the cron expression ticks.

        Uses ``croniter`` (5-field standard cron, 1-minute minimum) with
        the declared timezone (``src.params["tz"]``) so cron expressions
        are interpreted at the right wall clock.
        """
        from zoneinfo import ZoneInfo

        from croniter import croniter

        from app.runtime.emit import emit

        expr = src.params["expr"]
        tz_name = src.params.get("tz", "UTC")
        zone = ZoneInfo(tz_name) if tz_name != "UTC" else UTC
        name = f"cron[{w.data_type.__name__}]"
        base = datetime.now(tz=zone)
        itr = croniter(expr, base)
        try:
            while True:
                next_ts = itr.get_next(datetime)
                delay = (next_ts - datetime.now(tz=zone)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                payload = self._build_payload(w, next_ts)
                await emit(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._record_source_error(name, e)
            return
```

- [ ] **Step 8: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_engine_phase4.py -v`
Expected: 3 passed

- [ ] **Step 9: 跑全量 runtime tests 确认无回归**

Run: `cd apps/agent-service && uv run pytest tests/runtime/ -v`
Expected: 全部 pass

- [ ] **Step 10: Commit**

```bash
git add apps/agent-service/app/runtime/source.py apps/agent-service/app/runtime/engine.py apps/agent-service/tests/runtime/test_engine_phase4.py
git commit -m "feat(runtime): Source.cron(tz=...) + engine cron loop honors tz"
```

---

## Task 2: Runtime.start_source_loops / stop_source_loops + watchdog

**Files:**
- Modify: `apps/agent-service/app/runtime/engine.py`
- Test: `apps/agent-service/tests/runtime/test_engine_phase4.py`

- [ ] **Step 1: 写失败测试 — start_source_loops 启动 cron task**

Append to `tests/runtime/test_engine_phase4.py`:

```python
class _StartTick(Data):
    ts: Annotated[str, Key]


_start_seen: list[_StartTick] = []


@node
async def _record_start(t: _StartTick) -> None:
    _start_seen.append(t)


@pytest.mark.asyncio
async def test_start_source_loops_starts_only_sources():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _start_seen.clear()

    wire(_StartTick).from_(Source.interval(seconds=0.05)).to(_record_start)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    try:
        await asyncio.sleep(0.2)
        assert len(_start_seen) >= 2
    finally:
        await rt.stop_source_loops()


@pytest.mark.asyncio
async def test_normal_stop_does_not_exit(monkeypatch):
    """stop_source_loops on the happy path must not call os._exit."""
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    _start_seen.clear()

    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))

    wire(_StartTick).from_(Source.interval(seconds=0.05)).to(_record_start)
    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    await asyncio.sleep(0.1)
    await rt.stop_source_loops()

    assert exits == []


@pytest.mark.asyncio
async def test_watchdog_exits_on_source_error(monkeypatch):
    """A fatal source loop error triggers os._exit(1)."""
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()

    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda code: exits.append(code))

    class _BadTick(Data):
        ts: Annotated[str, Key]

    @node
    async def _bad_consumer(t: _BadTick) -> None:
        raise RuntimeError("consumer raises every tick")

    wire(_BadTick).from_(Source.interval(seconds=0.05)).to(_bad_consumer)

    rt = Runtime(app_name="agent-service", migrate_schema_on_run=False)
    await rt.start_source_loops()
    await asyncio.sleep(0.2)  # give watchdog time to react
    await rt.stop_source_loops()

    assert exits == [1]
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_engine_phase4.py::test_start_source_loops_starts_only_sources -v`
Expected: FAIL（`Runtime` 没有 `start_source_loops` 方法）

- [ ] **Step 3: 拆 Runtime.run() 抽出 start_source_loops / stop_source_loops + watchdog**

Edit `apps/agent-service/app/runtime/engine.py`. 在 `__init__` 里加 watchdog field：

```python
    def __init__(
        self,
        app_name: str | None = None,
        *,
        migrate_schema_on_run: bool = True,
    ) -> None:
        self.app_name = app_name or os.getenv("APP_NAME") or DEFAULT_APP
        self._migrate_schema_on_run = migrate_schema_on_run
        self._source_tasks: list[asyncio.Task] = []
        self._watchdog_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._source_error: BaseException | None = None
```

新增方法 `start_source_loops` / `stop_source_loops` / `_watch_source_error`（在 `_record_source_error` 之后）：

```python
    async def start_source_loops(self) -> None:
        """Start cron / interval / mq source loops for nodes bound to this app.

        Also starts a watchdog task that monitors `_stop_event`. If a
        source loop hits a fatal error, watchdog calls ``os._exit(1)``
        so PaaS restarts the pod.

        Migrate / durable consumer / blocking 不在本方法范围 —— 调用方
        （main.py lifespan 或 Runtime.run()）自己负责。
        """
        if self._source_tasks or self._watchdog_task is not None:
            raise RuntimeError(
                "start_source_loops already called; call stop_source_loops() first"
            )

        valid = known_apps()
        if self.app_name not in valid:
            raise RuntimeError(
                f"start_source_loops for app={self.app_name!r}: "
                f"no @node bound there (known: {sorted(valid)})"
            )

        graph = compile_graph()
        allowed_nodes = nodes_for_app(self.app_name)
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        for w in graph.wires:
            if not w.consumers:
                continue
            if not all(c in allowed_nodes for c in w.consumers):
                continue
            for src in w.sources:
                if src.kind == "cron":
                    self._source_tasks.append(
                        loop.create_task(
                            self._source_loop_cron(w, src),
                            name=f"cron[{w.data_type.__name__}]",
                        )
                    )
                elif src.kind == "interval":
                    self._source_tasks.append(
                        loop.create_task(
                            self._source_loop_interval(w, src),
                            name=f"interval[{w.data_type.__name__}]",
                        )
                    )
                elif src.kind == "mq":
                    self._source_tasks.append(
                        loop.create_task(
                            self._source_loop_mq(w, src),
                            name=f"mq[{w.data_type.__name__}]",
                        )
                    )

        self._watchdog_task = loop.create_task(
            self._watch_source_error(),
            name=f"runtime-watchdog[{self.app_name}]",
        )

        logger.info(
            "runtime: app=%s start_source_loops (%d source task(s))",
            self.app_name,
            len(self._source_tasks),
        )

    async def stop_source_loops(self) -> None:
        """Cancel + await every source task + watchdog (explicit cancel)."""
        for t in self._source_tasks:
            t.cancel()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
        for t in [*self._source_tasks, self._watchdog_task]:
            if t is None:
                continue
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("runtime: task %s exited with %r", t.get_name(), e)
        self._source_tasks.clear()
        self._watchdog_task = None
        self._stop_event = None

    async def _watch_source_error(self) -> None:
        """Wait for `_stop_event`; on fire (with `_source_error` set),
        log fatal + ``os._exit(1)``.

        Normal shutdown 不走这条 —— stop_source_loops cancels this task
        directly so it never reads `_source_error`.
        """
        assert self._stop_event is not None
        await self._stop_event.wait()
        if self._source_error is not None:
            logger.fatal(
                "runtime: source loop fatal error %r, exiting process",
                self._source_error,
            )
            os._exit(1)
```

把 `Runtime.run()` 改写成调用新 API（保持原对外行为：migrate + durable consumers + start sources + block + stop + raise）：

```python
    async def run(self) -> None:
        """Boot the runtime and block until cancelled."""
        if self._migrate_schema_on_run:
            await self.migrate_schema()
        await start_consumers(app_name=self.app_name)
        await self.start_source_loops()
        try:
            assert self._stop_event is not None
            await self._stop_event.wait()
        finally:
            await self.stop_source_loops()
            await stop_consumers()

        if self._source_error is not None:
            raise self._source_error
```

(注：原 `run()` 把 source loop start 内联在 try 块里；本次抽出成 `start_source_loops`。`_record_source_error` 不变，仍 set `_stop_event` —— `run()` 路径靠 `_stop_event.wait()` 唤醒；lifespan 路径靠 watchdog `os._exit`。两路独立不冲突。)

- [ ] **Step 4: 跑新增测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_engine_phase4.py -v`
Expected: 6 passed (3 from Task 1 + 3 new)

- [ ] **Step 5: 跑现有 Runtime.run 测试确认无回归**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_engine.py -v`
Expected: 全部 pass（包括原有 `test_runtime_starts_source_loops_only_for_local_consumers` 等）

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/runtime/engine.py apps/agent-service/tests/runtime/test_engine_phase4.py
git commit -m "feat(runtime): start/stop_source_loops + watchdog for lifespan use"
```

---

## Task 3: 创建 app/domain/life_dataflow.py — 14 个 Data 类

**Files:**
- Create: `apps/agent-service/app/domain/life_dataflow.py`
- Test: `apps/agent-service/tests/domain/test_life_dataflow.py`

- [ ] **Step 1: 写失败测试 — Data 类可 import + 有 Key + 满足 transient 约束**

Create `apps/agent-service/tests/domain/test_life_dataflow.py`:

```python
"""Phase 4 life_dataflow Data classes — surface check."""
from __future__ import annotations

import pytest

from app.runtime.data import key_fields


def test_all_classes_importable():
    from app.domain import life_dataflow as ld

    for name in [
        "MinuteTick", "LightDayTick", "LightNightTick", "HeavyReviewTick",
        "DailyPlanTick", "GlimpseTick",
        "LifeTickRequest", "VoiceRequest", "LightReviewRequest",
        "HeavyReviewRequest", "GlimpseTickRequest",
        "SharedDailyContext", "DailyPlanRequest",
        "LifeStateChanged", "GlimpseRequest",
    ]:
        assert hasattr(ld, name), f"{name} missing"


def test_tick_classes_are_transient():
    from app.domain.life_dataflow import (
        MinuteTick, LightDayTick, LightNightTick, HeavyReviewTick,
        DailyPlanTick, GlimpseTick,
    )
    for cls in [MinuteTick, LightDayTick, LightNightTick, HeavyReviewTick,
                DailyPlanTick, GlimpseTick]:
        meta = getattr(cls, "Meta", None)
        assert meta is not None and getattr(meta, "transient", False), (
            f"{cls.__name__} should declare Meta.transient = True"
        )


def test_glimpse_request_is_persisted():
    """GlimpseRequest is durable -> must NOT be transient."""
    from app.domain.life_dataflow import GlimpseRequest
    meta = getattr(GlimpseRequest, "Meta", None)
    if meta is not None:
        assert not getattr(meta, "transient", False), (
            "GlimpseRequest goes through .durable() — must not be transient"
        )


def test_glimpse_request_key_is_request_id():
    from app.domain.life_dataflow import GlimpseRequest
    assert key_fields(GlimpseRequest) == ("request_id",)


def test_other_business_requests_keyed_by_persona():
    from app.domain.life_dataflow import (
        LifeTickRequest, VoiceRequest, LightReviewRequest, HeavyReviewRequest,
        GlimpseTickRequest, DailyPlanRequest,
    )
    for cls in [LifeTickRequest, VoiceRequest, LightReviewRequest,
                HeavyReviewRequest, GlimpseTickRequest, DailyPlanRequest]:
        assert key_fields(cls) == ("persona_id",), f"{cls.__name__} key wrong"


def test_shared_daily_context_keyed_by_date():
    from app.domain.life_dataflow import SharedDailyContext
    assert key_fields(SharedDailyContext) == ("target_date",)


def test_life_state_changed_keyed_by_persona():
    from app.domain.life_dataflow import LifeStateChanged
    assert key_fields(LifeStateChanged) == ("persona_id",)
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/domain/test_life_dataflow.py -v`
Expected: FAIL（module 不存在）

- [ ] **Step 3: 创建 life_dataflow.py**

Create `apps/agent-service/app/domain/life_dataflow.py`:

```python
"""Phase 4 dataflow — life engine / schedule / glimpse 调度信号 + 请求载荷.

cron tick 入口（5 种频率 + glimpse 5min）→ fan-out @node →
per-persona request → business @node。glimpse 还有一条 LifeStateChanged
即时事件路径，与 5min 周期路径汇入同一条 GlimpseRequest .durable() 边。

GlimpseRequest 是本期唯一持久化 Data —— durable 边要求消费端
``insert_idempotent`` dedup，必须有 pg 表。其他 Tick / Request 都是
进程内调度信号，``Meta.transient = True``。
"""
from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


# ---------------------------------------------------------------------------
# Cron tick 入口
# ---------------------------------------------------------------------------


class MinuteTick(Data):
    """Per-minute cron source. Shared by life_tick + voice fan-out."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class LightDayTick(Data):
    """Light reviewer 白天节奏（每 30min, CST 8-21）."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class LightNightTick(Data):
    """Light reviewer 夜间节奏（整点，CST 22-7 except 03）."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class HeavyReviewTick(Data):
    """Heavy reviewer 每日节奏（CST 03:00）."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class DailyPlanTick(Data):
    """Daily plan 每日节奏（CST 05:00）."""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


class GlimpseTick(Data):
    """Glimpse 5min 周期节奏。"""
    ts: Annotated[str, Key]

    class Meta:
        transient = True


# ---------------------------------------------------------------------------
# Per-persona business request
# ---------------------------------------------------------------------------


class LifeTickRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str

    class Meta:
        transient = True


class VoiceRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str

    class Meta:
        transient = True


class LightReviewRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str
    window_minutes: int

    class Meta:
        transient = True


class HeavyReviewRequest(Data):
    persona_id: Annotated[str, Key]
    ts: str

    class Meta:
        transient = True


class GlimpseTickRequest(Data):
    """5min 周期 fan-out 出的 per-persona 触发；下游 glimpse_tick_node
    内部判 activity 决定是否 emit GlimpseRequest。"""
    persona_id: Annotated[str, Key]
    ts: str

    class Meta:
        transient = True


# ---------------------------------------------------------------------------
# Daily plan：shared pipeline 输出 + per-persona request（in-process 内存传递）
# ---------------------------------------------------------------------------


class SharedDailyContext(Data):
    """Daily plan shared pipeline 输出（wild agents + search + theater）。
    target_date 作 Key 让 graph 上是 per-day singleton。in-process only。"""
    target_date: Annotated[str, Key]   # YYYY-MM-DD
    wild_materials: str
    search_anchors: str
    theater: str

    class Meta:
        transient = True


class DailyPlanRequest(Data):
    persona_id: Annotated[str, Key]
    target_date: str
    wild_materials: str
    search_anchors: str
    theater: str

    class Meta:
        transient = True


# ---------------------------------------------------------------------------
# Glimpse 事件路径
# ---------------------------------------------------------------------------


class LifeStateChanged(Data):
    """commit_life_state_impl 写入成功后 emit。"""
    persona_id: Annotated[str, Key]
    activity_type: str
    prev_activity_type: str   # "" 表示首次提交
    ts: str

    class Meta:
        transient = True


class GlimpseRequest(Data):
    """走 .durable() 跨进程 → run_glimpse_node。

    request_id 是 emit 端生成的 uuid4 —— mq redelivery 时复用同一
    request_id 让 ``insert_idempotent`` 拒绝第二次插入，run_glimpse 不会
    被同一 request 跑两次。runtime 自动建 ``data_glimpse_request`` 表
    （migrator.py:76 命名规则 ``data_{to_snake(ClassName)}``）；副产品
    是 glimpse 触发的天然审计。

    没有 Meta.transient —— durable 边的硬约束（runtime graph.py:286 拒绝
    ``transient + .durable()`` 组合）。
    """
    request_id: Annotated[str, Key]   # uuid4
    persona_id: str
    chat_id: str
    ts: str
    trigger_kind: str   # "tick" | "event"，便于审计区分两路触发
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/domain/test_life_dataflow.py -v`
Expected: 全部 pass

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/domain/life_dataflow.py apps/agent-service/tests/domain/test_life_dataflow.py
git commit -m "feat(domain): Phase 4 life_dataflow Data classes"
```

---

## Task 4: 公开 per-persona 入口（schedule + heavy reviewer）

**Files:**
- Modify: `apps/agent-service/app/memory/reviewer/heavy.py`
- Test: `apps/agent-service/tests/memory/test_reviewer_heavy_per_persona.py`

注意：`life/schedule.py` 的 `_run_persona_pipeline` / `_run_shared_pipeline` 暂保留下划线，wiring 直接 from import；`heavy.py::run_heavy_review_for_persona` 已经存在（看 `app/workers/cron.py` 那段引用），本任务只校验它对外可用 + 后续删 `run_heavy_review`。

- [ ] **Step 1: 验证 `_run_persona_pipeline` / `_run_shared_pipeline` 已可被外部 import**

Run: `cd apps/agent-service && uv run python -c "from app.life.schedule import _run_persona_pipeline, _run_shared_pipeline; print('ok')"`
Expected: prints `ok`

如果 print ok 则跳到 Step 2；若 ImportError 则需要去掉下划线（重命名为 `run_persona_pipeline` + `run_shared_pipeline`，并搜全仓 grep 改调用方）。

- [ ] **Step 2: 写 heavy reviewer 测试 — `run_heavy_review_for_persona` 是 public 入口**

Create `apps/agent-service/tests/memory/test_reviewer_heavy_per_persona.py`:

```python
"""run_heavy_review_for_persona 是 Phase 4 graph fan-out 的入口."""
from __future__ import annotations


def test_run_heavy_review_for_persona_is_importable():
    from app.memory.reviewer.heavy import run_heavy_review_for_persona
    import inspect
    assert inspect.iscoroutinefunction(run_heavy_review_for_persona)
    sig = inspect.signature(run_heavy_review_for_persona)
    assert "persona_id" in sig.parameters
```

- [ ] **Step 3: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/memory/test_reviewer_heavy_per_persona.py -v`
Expected: pass（函数本来就存在，只是这是 contract test 锁住后续删 `run_heavy_review` 时不能误删它）

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/tests/memory/test_reviewer_heavy_per_persona.py
git commit -m "test(memory): lock run_heavy_review_for_persona contract for Phase 4 graph fan-out"
```

---

## Task 5: 创建 nodes/life_dataflow.py — fan-out + business node

**Files:**
- Create: `apps/agent-service/app/nodes/life_dataflow.py`
- Test: `apps/agent-service/tests/nodes/test_life_dataflow.py`

- [ ] **Step 1: 写失败测试 — fan_out_life_tick 调用每个 persona 的 emit**

Create `apps/agent-service/tests/nodes/test_life_dataflow.py`:

```python
"""Phase 4 life_dataflow @node tests."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.runtime.emit import reset_emit_runtime
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring


CST = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def reset_runtime():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


@pytest.fixture
def mock_prod():
    with patch("app.nodes.life_dataflow._is_prod", return_value=True):
        yield


@pytest.fixture
def mock_personas(monkeypatch):
    async def _fake_list():
        return ["p1", "p2"]
    monkeypatch.setattr("app.nodes.life_dataflow._list_persona_ids", _fake_list)


@pytest.mark.asyncio
async def test_fan_out_life_tick_emits_per_persona(reset_runtime, mock_prod, mock_personas):
    from app.domain.life_dataflow import LifeTickRequest, MinuteTick
    from app.nodes.life_dataflow import fan_out_life_tick
    from app.runtime import wire

    seen: list[LifeTickRequest] = []

    async def _capture(r: LifeTickRequest) -> None:
        seen.append(r)
    # bind a probe consumer to the in-process LifeTickRequest wire
    from app.runtime.node import node
    probe = node(_capture)
    wire(LifeTickRequest).to(probe)

    await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert {r.persona_id for r in seen} == {"p1", "p2"}


@pytest.mark.asyncio
async def test_fan_out_life_tick_skips_non_prod(reset_runtime, mock_personas, monkeypatch):
    monkeypatch.setattr("app.nodes.life_dataflow._is_prod", lambda: False)
    from app.domain.life_dataflow import LifeTickRequest, MinuteTick
    from app.nodes.life_dataflow import fan_out_life_tick
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []

    async def _capture(r: LifeTickRequest) -> None:
        seen.append(r)
    wire(LifeTickRequest).to(node(_capture))

    await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_fan_out_life_tick_swallows_db_error(reset_runtime, mock_prod, monkeypatch, caplog):
    """DB 抖动不冒泡到 source loop。"""
    async def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("app.nodes.life_dataflow._list_persona_ids", _boom)

    from app.domain.life_dataflow import MinuteTick
    from app.nodes.life_dataflow import fan_out_life_tick

    # No exception raised — only logged
    await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert "list_persona_ids failed" in caplog.text


@pytest.mark.asyncio
async def test_fan_out_voice_only_at_top_of_hour(reset_runtime, mock_prod, mock_personas):
    from app.domain.life_dataflow import VoiceRequest, MinuteTick
    from app.nodes.life_dataflow import fan_out_voice
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: VoiceRequest) -> None: seen.append(r)
    wire(VoiceRequest).to(node(_capture))

    # 8:30 — wrong minute, no emit
    await fan_out_voice(MinuteTick(ts="2026-04-30T08:30:00+08:00"))
    assert seen == []

    # 8:00 — top of hour in 8..23, emits per persona
    await fan_out_voice(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert {r.persona_id for r in seen} == {"p1", "p2"}

    # 03:00 — top of hour but out of 8..23
    seen.clear()
    await fan_out_voice(MinuteTick(ts="2026-04-30T03:00:00+08:00"))
    assert seen == []
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/nodes/test_life_dataflow.py -v`
Expected: FAIL（`app.nodes.life_dataflow` 不存在）

- [ ] **Step 3: 创建 nodes/life_dataflow.py — fan-out + business node 部分（先不含 glimpse）**

Create `apps/agent-service/app/nodes/life_dataflow.py`:

```python
"""Phase 4 dataflow nodes: fan-out + business per-persona node + glimpse 双路径.

业务逻辑零搬迁 —— 每个 business node 都套薄壳调原函数（life.engine.tick /
memory.voice.generate_voice / reviewer.run_*_for_persona / schedule.
_run_persona_pipeline / glimpse.run_glimpse）。本期是调度层迁移；后续
phase 在重写 chat 时再回头看薄壳要不要去掉。
"""
from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from app.data.queries import list_all_persona_ids
from app.data.session import get_session
from app.domain.life_dataflow import (
    DailyPlanRequest,
    DailyPlanTick,
    HeavyReviewRequest,
    HeavyReviewTick,
    LifeTickRequest,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
    MinuteTick,
    SharedDailyContext,
    VoiceRequest,
)
from app.infra.config import settings
from app.runtime import emit, node

logger = logging.getLogger(__name__)
CST = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_prod() -> bool:
    """Lane gate — fan-out 在非 prod 直接 return，不 emit per-persona request."""
    return not (settings.lane and settings.lane != "prod")


async def _list_persona_ids() -> list[str]:
    async with get_session() as s:
        return await list_all_persona_ids(s)


async def _fan_out_per_persona(label: str, build_request) -> None:
    """通用 fan-out：包住 list_persona_ids + emit 循环，所有异常 log 不冒泡.

    DB 抖动 / emit 失败一律不扔回 source loop —— 否则 ``_record_source_error``
    会让进程退出。fan-out 失败的代价是这一拍丢，下一拍自然恢复。
    """
    try:
        pids = await _list_persona_ids()
    except Exception:
        logger.exception("%s: list_persona_ids failed", label)
        return
    for pid in pids:
        try:
            await emit(build_request(pid))
        except Exception:
            logger.exception("[%s] %s fan-out failed", pid, label)


# ---------------------------------------------------------------------------
# Cron fan-out @node
# ---------------------------------------------------------------------------


@node
async def fan_out_life_tick(t: MinuteTick) -> None:
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "life_tick", lambda pid: LifeTickRequest(persona_id=pid, ts=t.ts)
    )


@node
async def fan_out_voice(t: MinuteTick) -> None:
    if not _is_prod():
        return
    cst_ts = datetime.fromisoformat(t.ts).astimezone(CST)
    if cst_ts.hour not in range(8, 24):
        return
    if cst_ts.minute != 0:
        return  # voice 整点触发
    await _fan_out_per_persona(
        "voice", lambda pid: VoiceRequest(persona_id=pid, ts=t.ts)
    )


@node
async def fan_out_light_day(t: LightDayTick) -> None:
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "light_day",
        lambda pid: LightReviewRequest(persona_id=pid, ts=t.ts, window_minutes=30),
    )


@node
async def fan_out_light_night(t: LightNightTick) -> None:
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "light_night",
        lambda pid: LightReviewRequest(persona_id=pid, ts=t.ts, window_minutes=60),
    )


@node
async def fan_out_heavy(t: HeavyReviewTick) -> None:
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "heavy", lambda pid: HeavyReviewRequest(persona_id=pid, ts=t.ts)
    )


# ---------------------------------------------------------------------------
# Daily plan：shared 节点先跑一次 → SharedDailyContext → fan-out per-persona
# ---------------------------------------------------------------------------


@node
async def run_shared_daily_pipeline_node(t: DailyPlanTick) -> SharedDailyContext | None:
    if not _is_prod():
        return None
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


# ---------------------------------------------------------------------------
# Per-persona business @node — 薄壳调原函数；本期不动业务实现
# ---------------------------------------------------------------------------


@node
async def life_tick_node(r: LifeTickRequest) -> None:
    from app.life.engine import tick
    try:
        await tick(r.persona_id)
    except Exception:
        logger.exception("[%s] life_tick failed", r.persona_id)


@node
async def voice_node(r: VoiceRequest) -> None:
    from app.memory.voice import generate_voice
    try:
        await generate_voice(r.persona_id)
    except Exception:
        logger.exception("[%s] voice failed", r.persona_id)


@node
async def light_review_node(r: LightReviewRequest) -> None:
    from app.memory.reviewer.light import run_light_review
    try:
        await run_light_review(persona_id=r.persona_id, window_minutes=r.window_minutes)
    except Exception:
        logger.exception("[%s] light_review failed", r.persona_id)


@node
async def heavy_review_node(r: HeavyReviewRequest) -> None:
    from app.memory.reviewer.heavy import run_heavy_review_for_persona
    try:
        await run_heavy_review_for_persona(r.persona_id)
    except Exception:
        logger.exception("[%s] heavy_review failed", r.persona_id)


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
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/nodes/test_life_dataflow.py -v`
Expected: 全部 4 个 fan-out 测试 pass

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/nodes/life_dataflow.py apps/agent-service/tests/nodes/test_life_dataflow.py
git commit -m "feat(nodes): Phase 4 fan-out + business per-persona @node"
```

---

## Task 6: nodes/life_dataflow.py — glimpse 三节点（tick + event + run）

**Files:**
- Modify: `apps/agent-service/app/nodes/life_dataflow.py`
- Test: `apps/agent-service/tests/nodes/test_life_dataflow.py`

- [ ] **Step 1: 写失败测试 — glimpse_tick_node 业务行为**

Append to `tests/nodes/test_life_dataflow.py`:

```python
@pytest.fixture
def mock_target_groups(monkeypatch):
    monkeypatch.setattr("app.life.glimpse.list_target_groups", lambda: ["chatA", "chatB"])


@pytest.fixture
def mock_random_below_threshold(monkeypatch):
    """random.random() == 0.0 < 0.15 → 命中 15% 抽样."""
    monkeypatch.setattr("app.nodes.life_dataflow.random.random", lambda: 0.0)


@pytest.fixture
def mock_random_above_threshold(monkeypatch):
    monkeypatch.setattr("app.nodes.life_dataflow.random.random", lambda: 0.99)


class _FakeState:
    def __init__(self, activity: str):
        self.activity_type = activity


@pytest.fixture
def mock_life_state(monkeypatch):
    """回拨函数允许测试逐次设置 activity."""
    state_box: dict = {"activity": ""}

    async def _fake_find(_session, _persona_id):
        a = state_box["activity"]
        return _FakeState(a) if a else None
    monkeypatch.setattr("app.data.queries.find_latest_life_state", _fake_find)
    return state_box


@pytest.mark.asyncio
async def test_glimpse_tick_skips_sleeping(reset_runtime, mock_target_groups, mock_life_state):
    from app.domain.life_dataflow import GlimpseRequest, GlimpseTickRequest
    from app.nodes.life_dataflow import glimpse_tick_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    mock_life_state["activity"] = "sleeping"
    await glimpse_tick_node(GlimpseTickRequest(persona_id="p1", ts="2026-04-30T10:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_glimpse_tick_browsing_emits_for_each_target(reset_runtime, mock_target_groups, mock_life_state):
    from app.domain.life_dataflow import GlimpseRequest, GlimpseTickRequest
    from app.nodes.life_dataflow import glimpse_tick_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list[GlimpseRequest] = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    mock_life_state["activity"] = "browsing"
    await glimpse_tick_node(GlimpseTickRequest(persona_id="p1", ts="2026-04-30T10:00:00+08:00"))
    assert {r.chat_id for r in seen} == {"chatA", "chatB"}
    assert all(r.persona_id == "p1" for r in seen)
    assert all(r.trigger_kind == "tick" for r in seen)


@pytest.mark.asyncio
async def test_glimpse_tick_other_activity_15pct_hit(reset_runtime, mock_target_groups, mock_life_state, mock_random_below_threshold):
    from app.domain.life_dataflow import GlimpseRequest, GlimpseTickRequest
    from app.nodes.life_dataflow import glimpse_tick_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    mock_life_state["activity"] = "working"
    await glimpse_tick_node(GlimpseTickRequest(persona_id="p1", ts="2026-04-30T10:00:00+08:00"))
    assert len(seen) == 2  # 两个 chat


@pytest.mark.asyncio
async def test_glimpse_tick_other_activity_15pct_miss(reset_runtime, mock_target_groups, mock_life_state, mock_random_above_threshold):
    from app.domain.life_dataflow import GlimpseRequest, GlimpseTickRequest
    from app.nodes.life_dataflow import glimpse_tick_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    mock_life_state["activity"] = "working"
    await glimpse_tick_node(GlimpseTickRequest(persona_id="p1", ts="2026-04-30T10:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_glimpse_event_only_for_browsing(reset_runtime, mock_prod, mock_target_groups):
    from app.domain.life_dataflow import GlimpseRequest, LifeStateChanged
    from app.nodes.life_dataflow import glimpse_event_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    # 切到 browsing → 触发
    await glimpse_event_node(LifeStateChanged(
        persona_id="p1", activity_type="browsing",
        prev_activity_type="working", ts="2026-04-30T10:00:00+08:00",
    ))
    assert {r.chat_id for r in seen} == {"chatA", "chatB"}
    assert all(r.trigger_kind == "event" for r in seen)

    seen.clear()
    # 切到 working → 不触发
    await glimpse_event_node(LifeStateChanged(
        persona_id="p1", activity_type="working",
        prev_activity_type="browsing", ts="...",
    ))
    assert seen == []

    # 段内 refresh（同 activity）→ 不触发
    await glimpse_event_node(LifeStateChanged(
        persona_id="p1", activity_type="browsing",
        prev_activity_type="browsing", ts="...",
    ))
    assert seen == []


@pytest.mark.asyncio
async def test_run_glimpse_node_does_not_swallow_exception(monkeypatch):
    """durable 节点必须把异常抛出去，让 mq handler nack→DLQ。"""
    from app.domain.life_dataflow import GlimpseRequest
    from app.nodes.life_dataflow import run_glimpse_node

    async def _boom(_pid, _chat):
        raise RuntimeError("LLM down")
    monkeypatch.setattr("app.life.glimpse.run_glimpse", _boom)

    with pytest.raises(RuntimeError, match="LLM down"):
        await run_glimpse_node(GlimpseRequest(
            request_id="r1", persona_id="p1", chat_id="c1",
            ts="...", trigger_kind="tick",
        ))
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/nodes/test_life_dataflow.py::test_glimpse_tick_skips_sleeping -v`
Expected: FAIL（glimpse_tick_node 不存在）

- [ ] **Step 3a: 扩展 nodes/life_dataflow.py 顶部 import 块**

Edit `apps/agent-service/app/nodes/life_dataflow.py` 顶部 import 段，把新增 Data 类合并进现有 import 块：

```python
from app.domain.life_dataflow import (
    DailyPlanRequest,
    DailyPlanTick,
    GlimpseRequest,
    GlimpseTick,
    GlimpseTickRequest,
    HeavyReviewRequest,
    HeavyReviewTick,
    LifeStateChanged,
    LifeTickRequest,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
    MinuteTick,
    SharedDailyContext,
    VoiceRequest,
)
```

- [ ] **Step 3b: 在 nodes/life_dataflow.py 末尾追加 glimpse 三节点**

Append to `apps/agent-service/app/nodes/life_dataflow.py`:

```python
# ---------------------------------------------------------------------------
# Glimpse 双路径：5min 周期 + 切到 browsing 即时事件，汇入 GlimpseRequest
# ---------------------------------------------------------------------------


def _new_glimpse_request(persona_id: str, chat_id: str, ts: str, kind: str) -> GlimpseRequest:
    """统一构造 GlimpseRequest —— request_id 是 emit 端生成的 uuid4，
    durable consumer 在 redelivery 时复用同一 id 让 ``insert_idempotent`` 拒重."""
    return GlimpseRequest(
        request_id=str(uuid.uuid4()),
        persona_id=persona_id,
        chat_id=chat_id,
        ts=ts,
        trigger_kind=kind,
    )


@node
async def fan_out_glimpse(t: GlimpseTick) -> None:
    """5min cron → 对每个 persona emit GlimpseTickRequest."""
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "glimpse_tick", lambda pid: GlimpseTickRequest(persona_id=pid, ts=t.ts)
    )


@node
async def glimpse_tick_node(r: GlimpseTickRequest) -> None:
    """5min 周期路径：读 life_state 判 activity，决定要不要 emit GlimpseRequest.

    业务语义跟现状 cron_glimpse 完全一致：sleeping 跳过；browsing 必发；
    其他活动 15% 概率发。读 pg 失败按"这拍跳过"处理，下一拍恢复。
    """
    from app.data.queries import find_latest_life_state
    from app.life.glimpse import list_target_groups
    try:
        async with get_session() as s:
            state = await find_latest_life_state(s, r.persona_id)
    except Exception:
        logger.exception("[%s] glimpse_tick read life_state failed", r.persona_id)
        return
    activity = state.activity_type if state else ""
    if activity == "sleeping":
        return
    if activity != "browsing" and random.random() >= 0.15:
        return
    for chat_id in list_target_groups():
        try:
            await emit(_new_glimpse_request(r.persona_id, chat_id, r.ts, "tick"))
        except Exception:
            logger.exception("[%s][%s] glimpse_tick emit failed", r.persona_id, chat_id)


@node
async def glimpse_event_node(c: LifeStateChanged) -> None:
    """即时路径：仅在切到 browsing 瞬间补一拍 GlimpseRequest.

    其他状态切换（如切到 working / sleeping）不在事件路径触发 ——
    "持续期反复刷"由 5min cron 路径承担。
    """
    if not _is_prod():
        return
    if c.activity_type != "browsing":
        return
    if c.activity_type == c.prev_activity_type:
        return  # 段内 refresh 不响应
    from app.life.glimpse import list_target_groups
    for chat_id in list_target_groups():
        try:
            await emit(_new_glimpse_request(c.persona_id, chat_id, c.ts, "event"))
        except Exception:
            logger.exception("[%s][%s] glimpse_event emit failed", c.persona_id, chat_id)


@node
async def run_glimpse_node(r: GlimpseRequest) -> None:
    """LLM 重活，走 .durable() consumer。两条上游路径汇入这里.

    **不 try/except**: durable handler 在 ``message.process(requeue=False)``
    上下文里依靠 consumer 抛异常来 nack → DLX → DLQ。捕获异常会让
    handler 看到正常返回 → ack → message 永远不会进 DLQ，PR #202 的
    DLQ 监控失效。参照 ``run_post_safety`` 范式（row 不存在直接 raise）。
    """
    from app.life.glimpse import run_glimpse
    await run_glimpse(r.persona_id, r.chat_id)
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/nodes/test_life_dataflow.py -v`
Expected: 全部 pass

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/nodes/life_dataflow.py apps/agent-service/tests/nodes/test_life_dataflow.py
git commit -m "feat(nodes): Phase 4 glimpse double-path (tick + event) + run_glimpse_node"
```

---

## Task 7: 创建 wiring/life_dataflow.py + 注册到 wiring/__init__.py

**Files:**
- Create: `apps/agent-service/app/wiring/life_dataflow.py`
- Modify: `apps/agent-service/app/wiring/__init__.py`
- Test: `apps/agent-service/tests/wiring/test_life_dataflow_wiring.py`

- [ ] **Step 1: 写失败测试 — wiring 加载后 compile_graph 通过 + wire 数 15**

Create `apps/agent-service/tests/wiring/test_life_dataflow_wiring.py`:

```python
"""Phase 4 life_dataflow wiring smoke test."""
from __future__ import annotations

import pytest

from app.runtime.emit import reset_emit_runtime
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring


@pytest.fixture(autouse=True)
def _reset():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


def test_life_dataflow_wiring_compiles():
    """Loading the wiring module must produce a graph that compiles."""
    from app.runtime.graph import compile_graph
    from app.wiring import life_dataflow  # noqa: F401  side-effect import

    graph = compile_graph()  # raises GraphError on misconfig
    assert graph is not None


def test_life_dataflow_wire_count_is_15():
    from app.runtime.wire import WIRING_REGISTRY
    from app.wiring import life_dataflow  # noqa: F401

    # 5 cron Tick + GlimpseTick + SharedDailyContext + DailyPlanRequest +
    # 4 PersonaXxxRequest + GlimpseTickRequest + LifeStateChanged + GlimpseRequest
    # = 6 + 1 + 1 + 4 + 1 + 1 + 1 = 15
    types = {w.data_type.__name__ for w in WIRING_REGISTRY}
    expected = {
        "MinuteTick", "LightDayTick", "LightNightTick", "HeavyReviewTick",
        "DailyPlanTick", "GlimpseTick",
        "SharedDailyContext", "DailyPlanRequest",
        "LifeTickRequest", "VoiceRequest", "LightReviewRequest",
        "HeavyReviewRequest", "GlimpseTickRequest",
        "LifeStateChanged", "GlimpseRequest",
    }
    assert types == expected
    assert len(WIRING_REGISTRY) == 15


def test_glimpse_request_wire_is_durable():
    from app.runtime.wire import WIRING_REGISTRY
    from app.wiring import life_dataflow  # noqa: F401

    glimpse_req_wires = [w for w in WIRING_REGISTRY if w.data_type.__name__ == "GlimpseRequest"]
    assert len(glimpse_req_wires) == 1
    assert glimpse_req_wires[0].durable is True
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/wiring/test_life_dataflow_wiring.py -v`
Expected: FAIL（`app.wiring.life_dataflow` 不存在）

- [ ] **Step 3: 创建 wiring/life_dataflow.py**

Create `apps/agent-service/app/wiring/life_dataflow.py`:

```python
"""Phase 4 wiring：cron / 事件 source 接 fan-out / 业务 node.

Graph 拓扑（详见 docs/superpowers/specs/2026-04-30-dataflow-phase-4-...）:

  cron */1   → MinuteTick → fan_out_life_tick + fan_out_voice
  cron 0,30 8-21 → LightDayTick → fan_out_light_day
  cron 0 22-7 except 3 → LightNightTick → fan_out_light_night
  cron 0 3 → HeavyReviewTick → fan_out_heavy
  cron 0 5 → DailyPlanTick → run_shared_daily_pipeline_node → SharedDailyContext
                                                            → fan_out_daily_plan
  cron */5 → GlimpseTick → fan_out_glimpse → GlimpseTickRequest → glimpse_tick_node
  LifeStateChanged → glimpse_event_node
  GlimpseRequest .durable() → run_glimpse_node
"""
from app.domain.life_dataflow import (
    DailyPlanRequest,
    DailyPlanTick,
    GlimpseRequest,
    GlimpseTick,
    GlimpseTickRequest,
    HeavyReviewRequest,
    HeavyReviewTick,
    LifeStateChanged,
    LifeTickRequest,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
    MinuteTick,
    SharedDailyContext,
    VoiceRequest,
)
from app.nodes.life_dataflow import (
    daily_plan_node,
    fan_out_daily_plan,
    fan_out_glimpse,
    fan_out_heavy,
    fan_out_life_tick,
    fan_out_light_day,
    fan_out_light_night,
    fan_out_voice,
    glimpse_event_node,
    glimpse_tick_node,
    heavy_review_node,
    life_tick_node,
    light_review_node,
    run_glimpse_node,
    run_shared_daily_pipeline_node,
    voice_node,
)
from app.runtime import Source, wire

TZ = "Asia/Shanghai"

# Cron tick 入口
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
wire(GlimpseTickRequest).to(glimpse_tick_node)         # 5min 周期路径
wire(LifeStateChanged).to(glimpse_event_node)          # 即时路径
wire(GlimpseRequest).to(run_glimpse_node).durable()    # 重活走 mq
```

- [ ] **Step 4: 加入 wiring/__init__.py**

Edit `apps/agent-service/app/wiring/__init__.py`:

```python
"""Import all wiring submodules so their ``wire(...)`` calls run on package import."""
from app.wiring import (  # noqa: F401
    life_dataflow,
    memory,
    memory_triggers,
    memory_vectorize,
    safety,
)
```

- [ ] **Step 5: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/wiring/test_life_dataflow_wiring.py -v`
Expected: 3 passed

- [ ] **Step 6: 跑全量 wiring tests 确认无回归**

Run: `cd apps/agent-service && uv run pytest tests/wiring/ -v`
Expected: 全部 pass

- [ ] **Step 7: Commit**

```bash
git add apps/agent-service/app/wiring/life_dataflow.py apps/agent-service/app/wiring/__init__.py apps/agent-service/tests/wiring/test_life_dataflow_wiring.py
git commit -m "feat(wiring): Phase 4 life_dataflow wire registration"
```

---

## Task 8: life/tool.py emit LifeStateChanged

**Files:**
- Modify: `apps/agent-service/app/life/tool.py`
- Test: `apps/agent-service/tests/life/test_tool_emit.py`

- [ ] **Step 1: 写失败测试 — commit_life_state_impl 成功后 emit LifeStateChanged**

Create `apps/agent-service/tests/life/test_tool_emit.py`:

```python
"""commit_life_state_impl emits LifeStateChanged after a successful insert."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.runtime.emit import reset_emit_runtime
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring

CST = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _reset():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


@pytest.mark.asyncio
async def test_commit_life_state_emits_event(monkeypatch):
    from app.domain.life_dataflow import LifeStateChanged
    from app.life.tool import commit_life_state_impl
    from app.runtime import wire
    from app.runtime.node import node

    seen: list[LifeStateChanged] = []

    async def _capture(c: LifeStateChanged) -> None:
        seen.append(c)
    wire(LifeStateChanged).to(node(_capture))

    # mock insert_life_state to skip db
    async def _fake_insert(*_args, **_kwargs):
        return 12345
    monkeypatch.setattr("app.life.tool.insert_life_state", _fake_insert)

    # mock get_session to a context manager that yields None
    class _NullSession:
        async def __aenter__(self): return None
        async def __aexit__(self, *_): return False
    monkeypatch.setattr("app.life.tool.get_session", lambda: _NullSession())

    now = datetime.now(CST)
    end = now + timedelta(hours=1)

    result = await commit_life_state_impl(
        persona_id="p1",
        activity_type="browsing",
        current_state="刷手机",
        response_mood="放松",
        state_end_at=end,
        skip_until=None,
        reasoning=None,
        now=now,
        prev_state=None,
    )
    assert result.ok is True
    assert len(seen) == 1
    assert seen[0].persona_id == "p1"
    assert seen[0].activity_type == "browsing"
    assert seen[0].prev_activity_type == ""


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_commit(monkeypatch, caplog):
    from app.domain.life_dataflow import LifeStateChanged
    from app.life.tool import commit_life_state_impl
    from app.runtime import wire
    from app.runtime.node import node

    async def _boom(c: LifeStateChanged) -> None:
        raise RuntimeError("downstream broken")
    wire(LifeStateChanged).to(node(_boom))

    async def _fake_insert(*_a, **_kw): return 1
    monkeypatch.setattr("app.life.tool.insert_life_state", _fake_insert)

    class _NullSession:
        async def __aenter__(self): return None
        async def __aexit__(self, *_): return False
    monkeypatch.setattr("app.life.tool.get_session", lambda: _NullSession())

    now = datetime.now(CST)
    result = await commit_life_state_impl(
        persona_id="p1",
        activity_type="browsing",
        current_state="x",
        response_mood="x",
        state_end_at=now + timedelta(hours=1),
        skip_until=None,
        reasoning=None,
        now=now,
        prev_state=None,
    )
    assert result.ok is True   # commit success despite emit failure
    assert "LifeStateChanged emit failed" in caplog.text
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/life/test_tool_emit.py -v`
Expected: FAIL（commit_life_state_impl 还没 emit）

- [ ] **Step 3: 改 life/tool.py 末尾追加 emit**

Edit `apps/agent-service/app/life/tool.py`. 找到 `commit_life_state_impl` 的 return 之前（在 `async with get_session() as s: life_state_id = await insert_life_state(...)` 之后），加：

```python
    async with get_session() as s:
        life_state_id = await insert_life_state(
            s,
            persona_id=persona_id,
            current_state=current_state,
            activity_type=activity_type,
            response_mood=response_mood,
            reasoning=reasoning,
            skip_until=skip_until,
            state_end_at=state_end_at,
        )

    # Emit event for event-driven downstream (glimpse / etc).
    # try/except wraps emit so a downstream wire failure doesn't fail the
    # langchain tool — life_state already persisted; we don't want
    # life-engine retry causing a duplicate insert.
    try:
        from app.domain.life_dataflow import LifeStateChanged
        from app.runtime import emit
        prev_activity = (prev_state.activity_type if prev_state else "") or ""
        await emit(LifeStateChanged(
            persona_id=persona_id,
            activity_type=activity_type,
            prev_activity_type=prev_activity,
            ts=now.isoformat(),
        ))
    except Exception:
        logger.exception(
            "[%s] LifeStateChanged emit failed; commit succeeded", persona_id,
        )

    return CommitResult(ok=True, is_refresh=is_refresh, life_state_id=life_state_id)
```

- [ ] **Step 4: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/life/test_tool_emit.py -v`
Expected: 2 passed

- [ ] **Step 5: 跑现有 life tool 测试无回归**

Run: `cd apps/agent-service && uv run pytest tests/life/ -v 2>/dev/null || echo "no existing life tests"`
Expected: pass 或 "no existing life tests"

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/life/tool.py apps/agent-service/tests/life/test_tool_emit.py
git commit -m "feat(life): emit LifeStateChanged after commit_life_state_impl insert"
```

---

## Task 9: main.py lifespan — migrate_schema + start_source_loops + teardown

**Files:**
- Modify: `apps/agent-service/app/main.py`
- Test: `apps/agent-service/tests/runtime/test_main_lifespan.py`

- [ ] **Step 1: 写失败测试 — lifespan 调 migrate_schema 在 start_consumers 之前 + start_source_loops 在最后**

Create `apps/agent-service/tests/runtime/test_main_lifespan.py`:

```python
"""main.py lifespan invokes migrate + start_source_loops in the right order."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_migrates_then_starts_sources():
    """migrate_schema must run BEFORE start_consumers (durable consumer
    needs the table to exist) and start_source_loops must run AFTER
    register_http_sources."""
    call_order: list[str] = []

    async def _migrate(self):
        call_order.append("migrate_schema")

    async def _start_consumers(*_a, **_kw):
        call_order.append("start_consumers")

    async def _start_source_loops(self):
        call_order.append("start_source_loops")

    async def _stop_source_loops(self):
        call_order.append("stop_source_loops")

    with patch("app.runtime.engine.Runtime.migrate_schema", _migrate), \
         patch("app.runtime.durable.start_consumers", AsyncMock(side_effect=_start_consumers)), \
         patch("app.runtime.engine.Runtime.start_source_loops", _start_source_loops), \
         patch("app.runtime.engine.Runtime.stop_source_loops", _stop_source_loops), \
         patch("app.infra.qdrant.init_collections", AsyncMock()), \
         patch("app.runtime.bootstrap.declare_durable_topology", AsyncMock()), \
         patch("app.runtime.debounce.start_debounce_consumers", AsyncMock()), \
         patch("app.runtime.debounce.stop_debounce_consumers", AsyncMock()), \
         patch("app.runtime.durable.stop_consumers", AsyncMock()), \
         patch("app.workers.chat_consumer.start_chat_consumer", AsyncMock()), \
         patch("app.skills.registry.SkillRegistry.load_all"), \
         patch("app.skills.registry.skill_reload_loop", AsyncMock()), \
         patch("app.runtime.http_source.register_http_sources"):
        from app.main import lifespan
        from fastapi import FastAPI

        app = FastAPI()
        async with lifespan(app):
            pass

    assert call_order.index("migrate_schema") < call_order.index("start_consumers")
    assert call_order.index("start_source_loops") > call_order.index("start_consumers")
    assert "stop_source_loops" in call_order  # teardown ran
```

- [ ] **Step 2: 跑测试确认 fail**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_main_lifespan.py -v`
Expected: FAIL（main.py 还没调这些方法）

- [ ] **Step 3: 改 main.py lifespan**

Edit `apps/agent-service/app/main.py`. 在 `lifespan` 函数内的 `load_dataflow_graph()` 之后、`if settings.rabbitmq_url: await declare_durable_topology()` 之后，添加 migrate + 创建 runtime 对象；在 `register_http_sources(app)` 之后启 `start_source_loops`；teardown 调 `stop_source_loops`：

具体改动（在 lifespan 里）：

```python
    from app.runtime.bootstrap import declare_durable_topology, load_dataflow_graph
    from app.runtime.engine import Runtime

    load_dataflow_graph()

    runtime_for_sources = Runtime(
        app_name="agent-service",
        migrate_schema_on_run=False,  # we drive migrate explicitly below
    )

    # Migrate BEFORE starting durable consumers — Phase 4 GlimpseRequest is
    # persisted; consumer-side insert_idempotent needs the table to exist.
    await runtime_for_sources.migrate_schema()

    if settings.rabbitmq_url:
        await declare_durable_topology()

    # ... existing skill load + reload loop unchanged ...

    consumer_tasks: list[asyncio.Task] = []
    if settings.rabbitmq_url:
        from app.runtime.debounce import start_debounce_consumers
        from app.runtime.durable import start_consumers
        from app.workers.chat_consumer import start_chat_consumer
        await start_consumers(app_name="agent-service")
        logger.info("Runtime durable consumers started for agent-service")
        await start_debounce_consumers(app_name="agent-service")
        logger.info("Runtime debounce consumers started for agent-service")

        consumer_tasks.append(asyncio.create_task(start_chat_consumer()))
        logger.info("Chat request consumer started")

    from app.runtime.http_source import register_http_sources
    register_http_sources(app)
    logger.info("dataflow http sources registered")

    # Start cron / interval / mq source loops + watchdog (Phase 4)
    await runtime_for_sources.start_source_loops()
    logger.info("dataflow source loops started")

    yield

    # Teardown
    logger.info("dataflow source loops stopping")
    await runtime_for_sources.stop_source_loops()
    # ... existing consumer / debounce stop unchanged ...
```

(完整 lifespan 上下文较长，按现有结构在对应位置插入即可。)

- [ ] **Step 4: 跑测试确认 pass**

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_main_lifespan.py -v`
Expected: pass

- [ ] **Step 5: 跑全量 test 确认无回归**

Run: `cd apps/agent-service && uv run pytest -x --ignore=tests/integration -v 2>&1 | tail -30`
Expected: 全部 pass

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/main.py apps/agent-service/tests/runtime/test_main_lifespan.py
git commit -m "feat(main): lifespan migrates schema + starts source loops with watchdog"
```

---

## Task 10: arq_settings cutover — cron_jobs 缩 + 删 cron_* import + 删 startup seed

**Files:**
- Modify: `apps/agent-service/app/workers/arq_settings.py`

注：本 task 在 Task 11（删 cron.py）之前执行 —— 先把 arq_settings 的引用清掉，再删被引用的模块。

- [ ] **Step 1: 改 arq_settings.py — 删 cron_* import + 缩 cron_jobs + 删 startup seed**

Edit `apps/agent-service/app/workers/arq_settings.py`. 替换为：

```python
"""ARQ Worker configuration — long_task executor cron + event-driven workers.

Phase 4 cutover: life-engine / glimpse / voice / review / daily-plan cron
迁到 dataflow Source.cron + graph fan-out node（在 agent-service 主进程
lifespan 里跑）。arq-worker 现在只剩：
  - task_executor cron（每分钟轮询 long_tasks 表 —— long_tasks 子系统独立）
  - sync_life_state_after_schedule（事件触发 worker function）

Start command:
    arq app.workers.arq_settings.WorkerSettings
"""

from __future__ import annotations

import logging

from arq import cron
from arq.connections import RedisSettings
from inner_shared.logger import setup_logging

from app.infra.config import settings
from app.workers.state_sync_worker import sync_life_state_after_schedule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Long-task executor (every-minute poll)
# ---------------------------------------------------------------------------


async def task_executor_job(ctx) -> None:
    """arq cron: poll and execute long-running tasks."""
    from app.infra.config import settings as _s
    from app.long_tasks.executor import poll_and_execute_tasks

    await poll_and_execute_tasks(
        batch_size=_s.long_task_batch_size,
        lock_timeout_seconds=_s.long_task_lock_timeout,
    )


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------


async def on_startup(ctx) -> None:
    """Worker startup: configure logging, connect MQ.

    Phase 4: removed seed voice (cron_generate_voice). voice 由 dataflow
    主进程 graph cron 接管；arq-worker 不再承担 voice / life-engine /
    glimpse / review / daily-plan 调度。
    """
    setup_logging(log_dir="/logs/agent-service", log_file="arq-worker.log")
    logger.info("arq-worker started, file logging enabled")

    # MQ connect — sync_life_state_after_schedule 可能 emit 触发下游
    from app.infra.rabbitmq import mq

    await mq.connect()
    await mq.declare_topology()


# ---------------------------------------------------------------------------
# Worker settings
# ---------------------------------------------------------------------------


class WorkerSettings:
    """Unified ARQ Worker configuration.

    Start command:
        arq app.workers.arq_settings.WorkerSettings
    """

    on_startup = on_startup

    queue_name = f"arq:queue:{settings.lane}" if settings.lane else "arq:queue"

    redis_settings = RedisSettings(
        host=settings.redis_host or "localhost",
        port=6379,
        password=settings.redis_password,
        database=0,
    )

    functions: list = [sync_life_state_after_schedule]

    cron_jobs = [
        # task_executor: long_tasks 子系统独立保留，不在 Phase 4 范围
        cron(task_executor_job, minute=None),
    ]
```

- [ ] **Step 2: 验证 arq_settings 仍可 import（cron.py 还在，但已不被引用）**

Run: `cd apps/agent-service && uv run python -c "from app.workers.arq_settings import WorkerSettings; print(WorkerSettings.cron_jobs)"`
Expected: 输出包含一个 cron job（task_executor_job）

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/workers/arq_settings.py
git commit -m "chore(arq): drop cron_jobs except task_executor; remove cron_* imports + seed voice"
```

---

## Task 11: 删旧代码 — cron.py + common.py helpers + run_heavy_review + generate_all_daily_plans

**Files:**
- Delete: `apps/agent-service/app/workers/cron.py`
- Modify: `apps/agent-service/app/workers/common.py`
- Modify: `apps/agent-service/app/memory/reviewer/heavy.py`
- Modify: `apps/agent-service/app/life/schedule.py`

- [ ] **Step 1: grep 验证 cron.py 已无外部引用**

Run: `grep -rn "from app.workers.cron import\|app.workers.cron\." apps/agent-service/app apps/agent-service/tests 2>/dev/null`
Expected: 0 行（如果有，回到 Task 10 处理遗漏）

- [ ] **Step 2: 删 cron.py**

Run: `rm apps/agent-service/app/workers/cron.py`

- [ ] **Step 3: grep 验证 for_each_persona / prod_only / cron_error_handler 已无引用**

Run: `grep -rn "for_each_persona\|prod_only\|cron_error_handler" apps/agent-service/app apps/agent-service/tests 2>/dev/null | grep -v workers/common.py`
Expected: 0 行

- [ ] **Step 4: 删 common.py 三个 helper**

Edit `apps/agent-service/app/workers/common.py`. 保留 `mq_error_handler`，删除 `for_each_persona` / `prod_only` / `cron_error_handler` + 它们专用的 imports。最终内容：

```python
"""Shared worker utilities — MQ error handler.

Phase 4 cutover removed for_each_persona / prod_only / cron_error_handler
—— 调度迁到 dataflow graph fan-out + node 自身负责 lane gate 和 error
handling。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def mq_error_handler() -> Callable:
    """MQ consumer error handling: log + nack (no requeue).

    If the handler already acked/nacked via ``message.process()``,
    the second nack is safely ignored.
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(message, *args: P.args, **kwargs: P.kwargs) -> T | None:
            try:
                return await func(message, *args, **kwargs)  # type: ignore[misc]
            except Exception:
                logger.exception("MQ handler %s failed", func.__name__)
                if hasattr(message, "nack"):
                    try:
                        await message.nack(requeue=False)
                    except Exception:
                        pass  # already processed
                return None

        return wrapper  # type: ignore[return-value]

    return decorator
```

- [ ] **Step 5: 删 reviewer/heavy.py::run_heavy_review**

Edit `apps/agent-service/app/memory/reviewer/heavy.py`. 找到 `async def run_heavy_review() -> None:` 函数（含 `for_each_persona(run_heavy_review_for_persona, ...)`）整段删除。**保留 `run_heavy_review_for_persona`**（graph fan-out 入口）。

- [ ] **Step 6: 删 schedule.py::generate_all_daily_plans**

Edit `apps/agent-service/app/life/schedule.py`. 找到 `async def generate_all_daily_plans(...)` 整段删除。**保留 `_run_shared_pipeline` / `_run_persona_pipeline` / `generate_daily_plan`（admin trigger）**。

- [ ] **Step 7: 验证 import + compile_graph + 全测试通过**

```bash
cd apps/agent-service
uv run python -c "from app.main import lifespan; print('main.py imports ok')"
uv run python -c "from app.runtime.graph import compile_graph; from app.wiring import life_dataflow; compile_graph(); print('graph compiles')"
uv run pytest -x --ignore=tests/integration 2>&1 | tail -10
```

Expected: 三条都 ok / pass

- [ ] **Step 8: grep 最终扫尾验证**

Run:
```bash
grep -rn "cron_generate_voice\|cron_glimpse\|cron_heavy_review\|cron_life_engine_tick\|cron_memory_reviewer\|cron_generate_daily_plan" apps/agent-service 2>/dev/null
grep -rn "generate_all_daily_plans\|^async def run_heavy_review\b" apps/agent-service/app 2>/dev/null
grep -rn "for_each_persona\|@prod_only\|@cron_error_handler" apps/agent-service/app 2>/dev/null
```

Expected: 全部 0 行（除了 spec/plan 文档自身）

- [ ] **Step 9: Commit**

```bash
git add -u
git rm apps/agent-service/app/workers/cron.py
git commit -m "chore: delete cron.py + workers/common helpers + run_heavy_review + generate_all_daily_plans

Phase 4 cleanup. Graph fan-out 接管 cron 调度后所有这些都死代码。
保留：mq_error_handler / run_heavy_review_for_persona / _run_shared_pipeline /
_run_persona_pipeline / generate_daily_plan."
```

---

## Task 12: 泳道部署验证

**Files:** （只跑命令，不改代码）

- [ ] **Step 1: 推到远端**

```bash
git push -u origin refactor/flow-parse-4
```

- [ ] **Step 2: 部署 agent-service 到独立泳道**

```bash
make deploy APP=agent-service LANE=feat-flow-parse-4 GIT_REF=refactor/flow-parse-4
```

注：一镜像多服务，agent-service 镜像同时产出 arq-worker / vectorize-worker。Makefile 的 `deploy APP=agent-service` 应自动同步。如果没有，按 CLAUDE.md §4.4 部署铁律手动 release：
```bash
make release APP=arq-worker LANE=feat-flow-parse-4 VERSION=<version-from-deploy>
make release APP=vectorize-worker LANE=feat-flow-parse-4 VERSION=<version-from-deploy>
```

- [ ] **Step 3: 观察 agent-service 启动日志，确认 5 个 cron source + 1 个 watchdog 启动**

```bash
make logs APP=agent-service LANE=feat-flow-parse-4 KEYWORD=start_source_loops SINCE=5m
make logs APP=agent-service LANE=feat-flow-parse-4 KEYWORD=cron\\[ SINCE=5m
```

Expected: 看到 `app=agent-service start_source_loops (6 source task(s))` + 6 个 `cron[*Tick]` 名字

- [ ] **Step 4: 观察 5min 内 cron tick 节奏**

```bash
make logs APP=agent-service LANE=feat-flow-parse-4 KEYWORD=fan_out_life_tick SINCE=10m
make logs APP=agent-service LANE=feat-flow-parse-4 KEYWORD=glimpse_tick_node SINCE=10m
```

Expected:
- fan_out_life_tick：每分钟一次（dev 泳道直接 return，但 fan-out 节点入口本身仍触发；可能不输出 INFO log。改 grep INFO 级别 prod-only skip 或换看 source loop level log）

注：本期 fan-out 节点 `_is_prod()` 返回 False 时直接 return，不输出 log。dev 泳道验证依赖于：
- (a) cron source 已启动（通过 startup log 确认 ✓ Step 3）
- (b) `Runtime.start_source_loops` 没退出，watchdog 没触发 `os._exit(1)`（pod Running ≠ 0 重启次数）
- (c) emit 全链路在单测 + integration 测试已覆盖

prod 业务行为先不在本步验证。

- [ ] **Step 5: 部署到 prod（按 ship 流程）**

按用户 review 通过后，走 `/ship` skill；ship 之前在本 worktree 内确认：
- [ ] 所有改动 commit
- [ ] 全测试 pass
- [ ] PR 描述列改动 + 风险

ship 后立即看 prod 1 小时：
- `make logs APP=agent-service KEYWORD=cron\\[ SINCE=15m` → 6 个 cron source 启动
- `/ops-db @chiwei 'select count(*) from life_states where created_at > now() - interval ''30 min'''` → life_state 写入节奏与历史一致
- `/ops-db @chiwei 'select count(*), trigger_kind from data_glimpse_request where created_at > now() - interval ''30 min'' group by trigger_kind'` → 5min × N persona × M chat 量级 + 偶发 event kind
- `/ops-db @chiwei 'select count(*) from agent_responses where created_at > now() - interval ''1 hour'''` → voice 整点节奏

- [ ] **Step 6: 不在 plan 范围内**

cleanup 部署 / pr / merge 都按用户 ship 流程；本 plan 终止于代码 + 泳道验证。

---

## 自检清单

实施完毕后核对：

- [ ] `apps/agent-service/app/workers/cron.py` 不存在
- [ ] `app/workers/common.py` 没有 `for_each_persona` / `prod_only` / `cron_error_handler`
- [ ] `arq_settings.WorkerSettings.cron_jobs` 仅含 `task_executor_job`
- [ ] `app/wiring/life_dataflow.py` 存在；wire 数为 15
- [ ] `app/nodes/life_dataflow.py` 存在；@node 数为 16
- [ ] `app/life/tool.py::commit_life_state_impl` 末尾 emit `LifeStateChanged`，try/except 包
- [ ] `app/life/schedule.py::generate_all_daily_plans` 不存在；`_run_shared_pipeline` / `_run_persona_pipeline` / `generate_daily_plan` 保留
- [ ] `app/memory/reviewer/heavy.py::run_heavy_review` 不存在；`run_heavy_review_for_persona` 保留
- [ ] `app/main.py` lifespan 调 `migrate_schema()` + `start_source_loops()`，teardown 调 `stop_source_loops()`
- [ ] `app/runtime/source.py::Source.cron(expr, *, tz="UTC")` 签名落地
- [ ] `app/runtime/engine.py` cron loop 用 ZoneInfo
- [ ] `Runtime.start_source_loops()` / `stop_source_loops()` / `_watch_source_error()` 实现
- [ ] `data_glimpse_request` 表能由 main.py lifespan migrate 出来
- [ ] 全测试通过
