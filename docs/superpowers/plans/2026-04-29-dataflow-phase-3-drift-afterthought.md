# Dataflow Phase 3 — Drift / Afterthought 进 Graph 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实装 dataflow `.debounce()` runtime；把 drift / afterthought 这俩"in-memory 两阶段 debouncer 管线"改成 graph 节点；进程重启不丢已 publish 的 trigger。

**Architecture:**
- **Runtime `.debounce()`**：mq x-delayed-message + redis SET (latest trigger_id) + INCR (count)；publish-then-drop；handler atomic claim（stale check + clear count = 0）+ conditional DEL（仅删自己 trigger_id）
- **Reschedule 走异常 sentinel**：业务节点锁冲突时 `raise DebounceReschedule(SameTrigger)`，handler catch 后调 module-private `_do_reschedule` 跑 CAS swap latest（trigger_id 由 handler 局部变量提供，不暴露给业务节点）
- **业务侧 single-flight**：`drift_check` / `afterthought_check` 用 redis SETNX + uuid token + Lua compare-and-delete 释放锁
- **Lane fallback off**：debounce route 用 `Route(..., lane_fallback=False)`，跳过 `x-message-ttl=10000` 但保留 `x-dead-letter-exchange=DLX_NAME`（DLQ 仍生效）
- **跨进程**：单镜像 agent-service，三 Deployment 同步发布

**Tech Stack:** Python 3.12 / Pydantic v2 / SQLAlchemy 2 async / aio-pika / redis-py async / pytest-asyncio

**Reference spec:** `docs/superpowers/specs/2026-04-29-dataflow-phase-3-drift-afterthought-design.md` (Draft v8)

---

## File Structure

**新建（apps/agent-service/app/）：**

```
domain/
    memory_triggers.py              # DriftTrigger / AfterthoughtTrigger Data 类（transient）

nodes/
    memory_pipelines.py             # @node drift_check / afterthought_check + 业务 helper：
                                    #   - module-level: _LOCK_RELEASE_LUA
                                    #   - module-level: _AFTERTHOUGHT_CFG / _LOOKBACK_HOURS / _CST
                                    #   - 私有 helper: _run_drift / _recent_timeline /
                                    #     _recent_persona_replies / _generate_fragment / _build_scene
                                    #   - @node: drift_check / afterthought_check

wiring/
    memory.py                       # 2 条 wire 声明（DriftTrigger + AfterthoughtTrigger）

runtime/
    debounce.py                     # .debounce() runtime 全部内容：
                                    #   - 常量: _DEFAULT_TTL_SECONDS = 86400
                                    #   - Lua: _PUBLISH_LUA / _CLAIM_LUA /
                                    #     _CONDITIONAL_DEL_LUA / _RESCHEDULE_CAS_LUA
                                    #   - class DebounceReschedule(Exception)
                                    #   - module-private: _route_for / _do_reschedule
                                    #   - module-public: publish_debounce / start_debounce_consumers /
                                    #     stop_debounce_consumers
                                    #   - module-internal: _build_handler / _consumer_tags
```

**修改（apps/agent-service/app/）：**

```
runtime/
    wire.py                         # WireSpec 加 debounce_key_by 字段；
                                    # WireBuilder.debounce 加 key_by 参数

    graph.py                        # 删 198-209 unimplemented；加 .debounce() 10 项 reject + 1 accept

    emit.py                         # wire dispatch 循环加 if w.debounce is not None 分支

infra/
    rabbitmq.py                     # Route NamedTuple 加 lane_fallback 默认字段；
                                    # _build_queue_args 加 lane_fallback 参数；
                                    # declare_route / _ensure_lane_queue 读 route.lane_fallback

chat/
    post_actions.py                 # _emit_memory_trigger helper（包 emit + try/except）；
                                    # 切换 drift.on_event / afterthought.on_event 调用

main.py                             # lifespan 加 start_debounce_consumers /
                                    # stop_debounce_consumers
```

**删除（apps/agent-service/app/memory/）：**

```
debounce.py                         # DebouncedPipeline 整个废弃
drift.py                            # _Drift / drift / _run_drift / _recent_* 搬到 nodes/memory_pipelines.py
afterthought.py                     # _Afterthought / afterthought / _generate_fragment / 常量搬走
```

**测试（apps/agent-service/tests/）：**

```
unit/runtime/test_debounce.py       # publish_debounce / _build_handler / _do_reschedule /
                                    # DebounceReschedule 异常路径 / consumer 启停
unit/runtime/test_graph_debounce.py # 10 项 reject + 1 accept 校验
unit/runtime/test_wire.py           # 已有；加 .debounce(key_by=) 测试
unit/infra/test_rabbitmq.py         # 已有；加 _build_queue_args lane_fallback 4 case
unit/domain/test_memory_triggers.py # DriftTrigger / AfterthoughtTrigger 序列化 + transient
unit/nodes/test_memory_pipelines.py # drift_check / afterthought_check + helper 搬迁；锁 token / DebounceReschedule
integration/test_phase3_e2e.py      # in-memory mq + redis fake 端到端
```

**运维（不在 PR diff 里，跟踪在 followup）：**

- `RabbitmqConsumerDown` 告警 regex 扩展包含 `debounce_*`（通过 `/ops` 或 K8s alert-rules 同 PR #202 模式）

---

## Task 1: Route NamedTuple 加 lane_fallback 字段 + _build_queue_args 改造

**Files:**
- Modify: `apps/agent-service/app/infra/rabbitmq.py:39-41`（Route 定义）
- Modify: `apps/agent-service/app/infra/rabbitmq.py:108-125`（`_build_queue_args`）
- Test: `apps/agent-service/tests/unit/infra/test_rabbitmq.py`（新增或扩展）

- [ ] **Step 1: 写 _build_queue_args 单测（4 case）**

```python
# apps/agent-service/tests/unit/infra/test_rabbitmq.py 加测试
from app.infra.rabbitmq import (
    Route, _build_queue_args, DLX_NAME, EXCHANGE_NAME, _LANE_FALLBACK_TTL_MS,
    _NON_PROD_EXPIRES_MS,
)


def test_build_queue_args_prod_ignores_lane_fallback():
    # prod queue（lane=None）：永远只有 DLX_NAME，lane_fallback 不影响
    args = _build_queue_args("rk", lane=None, lane_fallback=True)
    assert args == {"x-dead-letter-exchange": DLX_NAME}
    args2 = _build_queue_args("rk", lane=None, lane_fallback=False)
    assert args2 == {"x-dead-letter-exchange": DLX_NAME}


def test_build_queue_args_lane_with_fallback_default():
    # lane queue 默认 lane_fallback=True：含 ttl + DLX 到 EXCHANGE_NAME(prod) + x-expires
    args = _build_queue_args("rk", lane="dev", lane_fallback=True)
    assert args == {
        "x-message-ttl": _LANE_FALLBACK_TTL_MS,
        "x-dead-letter-exchange": EXCHANGE_NAME,
        "x-dead-letter-routing-key": "rk",
        "x-expires": _NON_PROD_EXPIRES_MS,
    }


def test_build_queue_args_lane_fallback_off_keeps_dlx():
    # debounce route lane_fallback=False：不要 ttl + 不 fallback 到 prod，
    # 但 DLX_NAME 必须保留（reviewer round-5 H1：consumer 异常 nack 仍要进 dead_letters）
    args = _build_queue_args("rk", lane="dev", lane_fallback=False)
    assert args == {
        "x-dead-letter-exchange": DLX_NAME,
        "x-expires": _NON_PROD_EXPIRES_MS,
    }
    assert "x-message-ttl" not in args
    assert "x-dead-letter-routing-key" not in args


def test_route_default_lane_fallback_true():
    r = Route("q", "rk")
    assert r.lane_fallback is True


def test_route_explicit_lane_fallback_false():
    r = Route("q", "rk", lane_fallback=False)
    assert r.lane_fallback is False
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/infra/test_rabbitmq.py -v -k "queue_args or lane_fallback"`
Expected: FAIL — `Route` 没有 `lane_fallback` 字段；`_build_queue_args` 没有 `lane_fallback` 参数

- [ ] **Step 3: 修 Route + _build_queue_args**

```python
# apps/agent-service/app/infra/rabbitmq.py:39
class Route(NamedTuple):
    queue: str
    rk: str
    lane_fallback: bool = True   # 新增；默认 True 不破坏现有 Route("queue", "rk") 调用
```

```python
# apps/agent-service/app/infra/rabbitmq.py:108
def _build_queue_args(prod_rk: str, lane: str | None,
                     lane_fallback: bool = True) -> dict[str, Any]:
    """Build queue arguments.

    - prod queues: dead-letter to DLX
    - lane queues with lane_fallback=True: TTL -> main exchange with prod
      routing-key (fallback), plus auto-expire after 24 h idle
    - lane queues with lane_fallback=False: keep DLX (异常 nack 仍要进
      dead_letters), but no ttl-back-to-prod (long-delay messages 留在
      自己 lane 上等到期；reviewer round-1 M5 + round-5 H1)
    """
    extra: dict[str, Any] = {}
    if lane:
        extra["x-expires"] = _NON_PROD_EXPIRES_MS
    if not lane:
        return {"x-dead-letter-exchange": DLX_NAME, **extra}
    if not lane_fallback:
        return {"x-dead-letter-exchange": DLX_NAME, **extra}
    return {
        "x-message-ttl": _LANE_FALLBACK_TTL_MS,
        "x-dead-letter-exchange": EXCHANGE_NAME,
        "x-dead-letter-routing-key": prod_rk,
        **extra,
    }
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/infra/test_rabbitmq.py -v -k "queue_args or lane_fallback"`
Expected: PASS（5 个）

- [ ] **Step 5: 跑现有 rabbitmq 测试，确认没 regress**

Run: `cd apps/agent-service && uv run pytest tests/unit/infra/test_rabbitmq.py -v`
Expected: 全部 PASS（包括原有 case）

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/infra/rabbitmq.py apps/agent-service/tests/unit/infra/test_rabbitmq.py
git commit -m "feat(infra): Route.lane_fallback + _build_queue_args opt-out

debounce 队列需要长延迟（300s）但不能被 lane TTL=10s fallback 截到 prod。
加 Route.lane_fallback=False 让 lane queue 跳过 x-message-ttl + DLX-back-to-prod，
但保留 DLX_NAME 让 consumer 异常 nack 仍进 dead_letters。

Refs: spec §3.4.4 / §4.5"
```

---

## Task 2: declare_route / _ensure_lane_queue 读 route.lane_fallback

**Files:**
- Modify: `apps/agent-service/app/infra/rabbitmq.py:200-217`（`declare_route`）
- Modify: `apps/agent-service/app/infra/rabbitmq.py:219-233`（`_ensure_lane_queue`）
- Test: `apps/agent-service/tests/unit/infra/test_rabbitmq.py`

- [ ] **Step 1: 写测试 — declare_route 用 route.lane_fallback**

```python
# apps/agent-service/tests/unit/infra/test_rabbitmq.py 加
@pytest.mark.asyncio
async def test_declare_route_passes_lane_fallback_through(monkeypatch):
    """declare_route 应该把 route.lane_fallback 透传给 _build_queue_args。"""
    from unittest.mock import AsyncMock, MagicMock
    from app.infra.rabbitmq import _RabbitMQ, Route

    mq = _RabbitMQ()
    mq._channel = MagicMock()
    mq._exchange = MagicMock()
    declared_args = {}

    async def fake_declare_queue(name, durable, arguments):
        declared_args[name] = arguments
        q = MagicMock()
        q.bind = AsyncMock()
        return q

    mq._channel.declare_queue = AsyncMock(side_effect=fake_declare_queue)

    # 模拟 prod lane 用 lane_fallback=False
    monkeypatch.setattr("app.infra.rabbitmq.current_lane", lambda: "dev")
    route = Route("q", "rk", lane_fallback=False)
    await mq.declare_route(route)

    args = declared_args["q_dev"]
    assert "x-message-ttl" not in args
    assert "x-dead-letter-routing-key" not in args
    # DLX 必须保留
    from app.infra.rabbitmq import DLX_NAME
    assert args["x-dead-letter-exchange"] == DLX_NAME
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/infra/test_rabbitmq.py::test_declare_route_passes_lane_fallback_through -v`
Expected: FAIL — `declare_route` 没读 `route.lane_fallback`

- [ ] **Step 3: 修 declare_route + _ensure_lane_queue**

```python
# apps/agent-service/app/infra/rabbitmq.py
async def declare_route(self, route: Route) -> None:
    """Declare a single route's queue + binding on the main exchange.

    Reads ``route.lane_fallback`` (default True for prod compatibility) to
    decide whether the lane queue gets x-message-ttl-back-to-prod fallback.
    debounce routes set ``lane_fallback=False`` so 300s delays don't get
    short-circuited to prod (spec §3.4.4 / reviewer round-5 H1).
    """
    if self._channel is None or self._exchange is None:
        raise RuntimeError("must call connect() + declare_topology() first")
    lane = current_lane()
    q = await self._channel.declare_queue(
        lane_queue(route.queue, lane),
        durable=True,
        arguments=_build_queue_args(route.rk, lane, route.lane_fallback),
    )
    await q.bind(self._exchange, routing_key=_lane_rk(route.rk, lane))


async def _ensure_lane_queue(self, route: Route, lane: str) -> None:
    """Lazily declare a lane queue on first publish (reads route.lane_fallback)."""
    cache_key = f"{route.queue}_{lane}"
    if cache_key in self._declared_lane_queues:
        return
    if self._channel is None:
        raise RuntimeError("must call connect() first")
    q = await self._channel.declare_queue(
        lane_queue(route.queue, lane),
        durable=True,
        arguments=_build_queue_args(route.rk, lane, route.lane_fallback),
    )
    await q.bind(self._exchange, routing_key=_lane_rk(route.rk, lane))
    self._declared_lane_queues.add(cache_key)
    logger.info("Lazy-declared lane queue: %s_%s", route.queue, lane)
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/infra/test_rabbitmq.py::test_declare_route_passes_lane_fallback_through -v`
Expected: PASS

- [ ] **Step 5: 跑全文件测试确认无 regress**

Run: `cd apps/agent-service && uv run pytest tests/unit/infra/test_rabbitmq.py -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/infra/rabbitmq.py apps/agent-service/tests/unit/infra/test_rabbitmq.py
git commit -m "feat(infra): declare_route / _ensure_lane_queue 读 route.lane_fallback

让 debounce route 的 lane_fallback=False 信息从 Route 字段传到 lane queue
declare 路径，consumer 端 declare 跟 producer 端 lazy-declare 行为一致。

Refs: spec §3.4.4"
```

---

## Task 3: WireBuilder.debounce 加 key_by 参数

**Files:**
- Modify: `apps/agent-service/app/runtime/wire.py:28,68-70`
- Test: `apps/agent-service/tests/unit/runtime/test_wire.py`（已有）

- [ ] **Step 1: 写测试**

```python
# apps/agent-service/tests/unit/runtime/test_wire.py 加
from app.runtime.wire import wire, clear_wiring, WIRING_REGISTRY
from app.runtime.data import Data
from typing import Annotated
from app.runtime.data import Key


def test_debounce_stores_key_by():
    clear_wiring()

    class T(Data):
        chat_id: Annotated[str, Key]

        class Meta:
            transient = True

    def consumer(t: T) -> None:
        return None

    wire(T).debounce(
        seconds=60,
        max_buffer=5,
        key_by=lambda e: f"k:{e.chat_id}",
    ).to(consumer)

    spec = WIRING_REGISTRY[-1]
    assert spec.debounce == {"seconds": 60, "max_buffer": 5}
    assert spec.debounce_key_by is not None
    sample = T(chat_id="abc")
    assert spec.debounce_key_by(sample) == "k:abc"
    clear_wiring()


def test_debounce_requires_key_by_keyword():
    clear_wiring()

    class T(Data):
        chat_id: Annotated[str, Key]

        class Meta:
            transient = True

    def consumer(t: T) -> None:
        return None

    # key_by 必填且 keyword-only
    with pytest.raises(TypeError):
        wire(T).debounce(seconds=60, max_buffer=5).to(consumer)  # type: ignore
    clear_wiring()
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_wire.py -v -k debounce`
Expected: FAIL — `debounce_key_by` 字段不存在

- [ ] **Step 3: 修 wire.py**

```python
# apps/agent-service/app/runtime/wire.py:19-29
@dataclass
class WireSpec:
    data_type: type[Data]
    consumers: list[Callable] = field(default_factory=list)
    sinks: list[SinkSpec] = field(default_factory=list)
    sources: list[SourceSpec] = field(default_factory=list)
    durable: bool = False
    as_latest: bool = False
    predicate: Callable | None = None
    debounce: dict | None = None
    debounce_key_by: Callable | None = None  # 新增：debounce wire 的 key 提取函数
    with_latest: tuple[type[Data], ...] = ()
```

```python
# apps/agent-service/app/runtime/wire.py:68-70
def debounce(
    self, *,
    seconds: int,
    max_buffer: int,
    key_by: Callable[[Data], str],
) -> WireBuilder:
    """Declare debounce semantics on this wire.

    ``key_by`` extracts a partition key from each Data instance — debounce
    state (latest trigger_id, count) is per-key. Required (no default) so
    every debounce wire explicitly names its partition.
    """
    self._spec.debounce = {"seconds": seconds, "max_buffer": max_buffer}
    self._spec.debounce_key_by = key_by
    return self
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_wire.py -v -k debounce`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/runtime/wire.py apps/agent-service/tests/unit/runtime/test_wire.py
git commit -m "feat(runtime/wire): WireBuilder.debounce 加 key_by 必填参数

为 .debounce() runtime 准备 DSL 签名；key_by 强制必填，没有默认值，每条
debounce wire 必须显式声明 partition key。

Refs: spec §3.4.1"
```

---

## Task 4: compile_graph 接受 .debounce() + 10 项 reject

**Files:**
- Modify: `apps/agent-service/app/runtime/graph.py:194-209`（删 unimplemented；加 .debounce() 校验段）
- Test: `apps/agent-service/tests/unit/runtime/test_graph_debounce.py`（新增）

- [ ] **Step 1: 写测试 — 11 个 case**

```python
# apps/agent-service/tests/unit/runtime/test_graph_debounce.py
from typing import Annotated
import pytest
from app.runtime.data import Data, Key
from app.runtime.node import node, clear_nodes
from app.runtime.placement import clear_bindings
from app.runtime.sink import Sink
from app.runtime.source import Source
from app.runtime.wire import wire, clear_wiring
from app.runtime.graph import compile_graph, GraphError


class _T(Data):
    chat_id: Annotated[str, Key]

    class Meta:
        transient = True


class _NotTransient(Data):
    chat_id: Annotated[str, Key]
    # 没有 Meta.transient = True


class _T2(Data):
    chat_id: Annotated[str, Key]

    class Meta:
        transient = True


@pytest.fixture(autouse=True)
def _reset():
    clear_wiring()
    clear_nodes()
    clear_bindings()
    yield
    clear_wiring()
    clear_nodes()
    clear_bindings()


def _consumer(_t: _T) -> None: return None


def _consumer2(_t: _T2) -> None: return None


def test_debounce_accepts_minimal_legal_form():
    node(_consumer)
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).to(_consumer)
    g = compile_graph()  # 不抛
    assert _T in g.data_types


def test_debounce_rejects_durable():
    node(_consumer)
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).durable().to(_consumer)
    with pytest.raises(GraphError, match="debounce.*durable"):
        compile_graph()


def test_debounce_rejects_as_latest():
    node(_consumer)
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).as_latest().to(_consumer)
    with pytest.raises(GraphError, match="as_latest"):
        compile_graph()


def test_debounce_rejects_with_latest():
    class _W(Data):
        chat_id: Annotated[str, Key]

        class Meta:
            transient = True

    node(_consumer)
    wire(_W).as_latest()
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).with_latest(_W).to(_consumer)
    with pytest.raises(GraphError, match="with_latest"):
        compile_graph()


def test_debounce_rejects_when_predicate():
    node(_consumer)
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).when(lambda x: True).to(_consumer)
    with pytest.raises(GraphError, match="DebounceReschedule"):
        compile_graph()


def test_debounce_rejects_sink():
    node(_consumer)
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).to(Sink.mq("recall"))
    with pytest.raises(GraphError, match="Sink"):
        compile_graph()


def test_debounce_rejects_source():
    node(_consumer)
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).from_(Source.mq("foo")).to(_consumer)
    with pytest.raises(GraphError, match="Source"):
        compile_graph()


def test_debounce_rejects_fanout():
    def _other(_t: _T) -> None: return None
    node(_consumer)
    node(_other)
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).to(_consumer, _other)
    with pytest.raises(GraphError, match="exactly one"):
        compile_graph()


def test_debounce_rejects_two_wires_same_datatype():
    def _c2(_t: _T) -> None: return None
    node(_consumer)
    node(_c2)
    wire(_T).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).to(_consumer)
    wire(_T).debounce(seconds=120, max_buffer=10, key_by=lambda e: e.chat_id).to(_c2)
    with pytest.raises(GraphError, match="already declared"):
        compile_graph()


def test_debounce_rejects_non_transient_data():
    def _c(_t: _NotTransient) -> None: return None
    node(_c)
    wire(_NotTransient).debounce(seconds=60, max_buffer=5, key_by=lambda e: e.chat_id).to(_c)
    with pytest.raises(GraphError, match="transient"):
        compile_graph()


def test_debounce_rejects_missing_key_by():
    """DSL 层 key_by 必填；测试 graph 层 defensive 校验（构造 spec 时绕过 DSL）。"""
    from app.runtime.wire import WIRING_REGISTRY, WireSpec
    node(_consumer)
    spec = WireSpec(
        data_type=_T,
        consumers=[_consumer],
        debounce={"seconds": 60, "max_buffer": 5},
        debounce_key_by=None,  # defensive 路径
    )
    WIRING_REGISTRY.append(spec)
    with pytest.raises(GraphError, match="key_by"):
        compile_graph()
```

- [ ] **Step 2: 跑测试验证大部分 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_graph_debounce.py -v`
Expected: 全部 FAIL — graph 还在 raise "unimplemented wire features"

- [ ] **Step 3: 修 graph.py — 删 unimplemented + 加 .debounce() 校验**

替换 `apps/agent-service/app/runtime/graph.py:194-209`：

```python
# 5) .debounce() canonical shape:
#   exactly one @node consumer
#   data type Meta.transient = True
#   key_by 必填
#   不跟 .durable() / .as_latest() / .with_latest() / .when() / sinks / sources / fan-out 组合
#   每个 (DataType) 在所有 .debounce() wire 中至多出现一次
#     （否则两条 wire 会共享 redis debounce:latest:{DataType}:{key} 状态污染）
seen_debounce_types: set[type[Data]] = set()
for w in wires:
    if w.debounce is None:
        continue
    if w.debounce_key_by is None:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce(...) requires key_by="
        )
    if w.durable:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce().durable(): "
            f"debounce already implements its own mq transport; "
            f"combining with .durable() is not supported"
        )
    if w.as_latest:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce().as_latest(): "
            f"as_latest persists data via insert_latest, but debounce "
            f"data types must be Meta.transient = True (no pg table). "
            f"These two are mutually exclusive."
        )
    if w.with_latest:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce().with_latest(...): "
            f"debounce handlers are single-input; .with_latest() not supported"
        )
    if w.predicate is not None:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce().when(...): "
            f"emit() respects predicate but the DebounceReschedule path bypasses it; "
            f"the two paths would behave inconsistently. Filter upstream of "
            f"emit instead, or drop .when() / .debounce() — pick one."
        )
    if w.sinks:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce().to(Sink.*): "
            f"debounce wires must target exactly one @node consumer; "
            f"sinks not supported (the fire signal needs business logic, "
            f"not a passthrough mq publish)"
        )
    if w.sources:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce().from_(Source.*): "
            f"debounce wires are emit-driven; declarative sources not supported"
        )
    if len(w.consumers) != 1:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce(): must have exactly one "
            f"consumer; got {len(w.consumers)} "
            f"({[c.__name__ for c in w.consumers]}). debounce state "
            f"(redis latest+count keyed by DataType+key) cannot be split "
            f"across consumers"
        )
    if w.data_type in seen_debounce_types:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce(): {w.data_type.__name__} "
            f"already declared on another debounce wire; redis state "
            f"(debounce:latest:{{DataType}}:{{key}}) would collide. Each "
            f"DataType can have at most one debounce wire."
        )
    seen_debounce_types.add(w.data_type)
    meta = getattr(w.data_type, "Meta", None)
    if meta is None or not getattr(meta, "transient", False):
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce(): data type must be "
            f"Meta.transient = True (debounce fire signals are not "
            f"persisted to pg)"
        )
```

注意：这段代码替换原 §198-209 的 `unimplemented` 检查（包括 `if w.debounce is not None: unimplemented.append(...)` 那段）。删那段，把上面的校验插到原位置。

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_graph_debounce.py -v`
Expected: 11 PASS

- [ ] **Step 5: 跑现有 graph 测试，确认无 regress**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/ -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/runtime/graph.py apps/agent-service/tests/unit/runtime/test_graph_debounce.py
git commit -m "feat(runtime/graph): compile_graph 接受 .debounce() + 10 项 reject

删 unimplemented raise；加 .debounce() canonical shape 校验：
exactly 1 consumer + transient data + key_by 必填 +
不跟 durable / as_latest / with_latest / when / sinks / sources /
fan-out / 同 DataType 多 wire 组合。

Refs: spec §3.4.2"
```

---

## Task 5: 创建 DriftTrigger / AfterthoughtTrigger Data 类

**Files:**
- Create: `apps/agent-service/app/domain/memory_triggers.py`
- Test: `apps/agent-service/tests/unit/domain/test_memory_triggers.py`

- [ ] **Step 1: 写测试**

```python
# apps/agent-service/tests/unit/domain/test_memory_triggers.py
from app.domain.memory_triggers import DriftTrigger, AfterthoughtTrigger


def test_drift_trigger_is_transient():
    assert getattr(DriftTrigger.Meta, "transient", False) is True


def test_afterthought_trigger_is_transient():
    assert getattr(AfterthoughtTrigger.Meta, "transient", False) is True


def test_drift_trigger_dump_load():
    t = DriftTrigger(chat_id="c1", persona_id="p1")
    payload = t.model_dump(mode="json")
    assert payload == {"chat_id": "c1", "persona_id": "p1"}
    t2 = DriftTrigger(**payload)
    assert t2 == t


def test_afterthought_trigger_dump_load():
    t = AfterthoughtTrigger(chat_id="c1", persona_id="p1")
    payload = t.model_dump(mode="json")
    assert payload == {"chat_id": "c1", "persona_id": "p1"}
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/domain/test_memory_triggers.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 创建 memory_triggers.py**

```python
# apps/agent-service/app/domain/memory_triggers.py
"""Data types for memory pipeline debounce triggers (drift / afterthought).

Both are ``Meta.transient = True`` — fire signals are not persisted to pg.
The runtime keeps state in mq (delay messages) + redis (latest trigger_id),
not in a table.
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


class DriftTrigger(Data):
    """Emitted by chat post_actions when an assistant reply lands.

    drift_check (debounced) reads the recent persona reply window from db
    and decides whether to regenerate base reply_style.
    """

    chat_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True


class AfterthoughtTrigger(Data):
    """Emitted by chat post_actions when a qualifying message lands.

    afterthought_check (debounced 300s / max_buffer 15) summarises the
    recent chat history into a v4 conversation Fragment.
    """

    chat_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/domain/test_memory_triggers.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/domain/memory_triggers.py apps/agent-service/tests/unit/domain/test_memory_triggers.py
git commit -m "feat(domain): DriftTrigger / AfterthoughtTrigger transient data types

Phase 3 fire-signal data types。Meta.transient=True：状态在 mq + redis
不进 pg；drift_check / afterthought_check 节点拿 (chat_id, persona_id)
信号，自己去 db 拉时间窗口数据。

Refs: spec §3.1"
```

---

## Task 6: runtime/debounce.py 骨架（常量 + Lua + DebounceReschedule + _route_for）

**Files:**
- Create: `apps/agent-service/app/runtime/debounce.py`
- Test: `apps/agent-service/tests/unit/runtime/test_debounce.py`

- [ ] **Step 1: 写骨架测试**

```python
# apps/agent-service/tests/unit/runtime/test_debounce.py
from app.runtime.debounce import (
    DebounceReschedule, _route_for, _DEFAULT_TTL_SECONDS,
)
from app.runtime.wire import WireSpec
from app.domain.memory_triggers import DriftTrigger


def _drift_check_stub(t: DriftTrigger) -> None:
    return None


def test_default_ttl_is_24h():
    assert _DEFAULT_TTL_SECONDS == 86400


def test_route_for_uses_lane_fallback_false():
    spec = WireSpec(
        data_type=DriftTrigger,
        consumers=[_drift_check_stub],
        debounce={"seconds": 60, "max_buffer": 5},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )
    route = _route_for(spec, _drift_check_stub)
    assert route.queue == "debounce_drift_trigger__drift_check_stub"
    assert route.rk == "debounce.drift_trigger.__drift_check_stub" or \
           route.rk == "debounce.drift_trigger._drift_check_stub"  # 取决于 to_snake 实现
    assert route.lane_fallback is False


def test_debounce_reschedule_carries_data():
    t = DriftTrigger(chat_id="c1", persona_id="p1")
    exc = DebounceReschedule(t)
    assert exc.data == t
    assert "DriftTrigger" in str(exc)


def test_no_module_level_reschedule_function():
    """API 边界：业务节点不应拿到 module-level reschedule()，
    避免 contextvar 泄漏到 background task (reviewer round-7 M1)."""
    import app.runtime.debounce as mod
    assert not hasattr(mod, "reschedule")
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v`
Expected: FAIL — module 不存在

- [ ] **Step 3: 创建 debounce.py 骨架**

```python
# apps/agent-service/app/runtime/debounce.py
"""Runtime support for ``wire(T).debounce(...)``.

Pipeline shape:

    upstream emit -> publish_debounce
        |
        SET latest = trigger_id (redis, atomic with INCR count) +
        mq.publish delayed body (carries trigger_id + data)
        |
    handler picks up the delayed message
        |
        atomic-claim: stale-check (latest == trigger_id?) +
                      clear count = 0 in one Lua script
        |
    consumer (e.g. drift_check) runs
        |
        either returns normally (handler conditional-DELs latest+count)
        or raises DebounceReschedule(SameTrigger)
        (handler runs _do_reschedule with its own trigger_id)
        or raises any other exception (DLQ)

Reschedule API is intentionally not exposed at module level; business
nodes signal a reschedule by raising DebounceReschedule, the handler
holds the trigger_id and runs the CAS swap. Anything else (calling a
reschedule function from a background task that inherited a contextvar,
say) is unrepresentable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.api.middleware import lane_var, trace_id_var
from app.infra.rabbitmq import Route, current_lane, lane_queue, mq
from app.infra.redis import get_redis
from app.runtime.data import Data
from app.runtime.naming import to_snake
from app.runtime.node import inputs_of
from app.runtime.placement import nodes_for_app
from app.runtime.wire import WireSpec

logger = logging.getLogger(__name__)

# 24h covers typical outage windows; redis state expires past that, but
# at-least-once delivery from mq + business pipelines that auto-recover on
# next event make the cliff acceptable (see spec §4.1).
_DEFAULT_TTL_SECONDS = 86400


# ---------------------------------------------------------------------------
# Lua scripts
# ---------------------------------------------------------------------------

# publish_debounce: atomic SET latest + INCR count, with max_buffer trip:
# when count crosses the threshold, atomically reset count to 0 and tell
# the caller to flag this publish as fire_now=1 (immediate-fire path).
_PUBLISH_LUA = """
local ttl = tonumber(ARGV[2])
local max_buffer = tonumber(ARGV[3])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
local n = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ttl)
local fire_now = 0
if n >= max_buffer then
    redis.call('SET', KEYS[2], 0, 'EX', ttl)
    fire_now = 1
end
return {n, fire_now}
"""

# handler atomic claim: stale-check (latest == this trigger?) and clear
# count = 0 in one shot. Returns 1 = claimed, 0 = stale.
_CLAIM_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[2], 0, 'EX', ARGV[2])
return 1
"""

# handler conditional DEL: only delete latest+count if latest is still
# this trigger_id. If a reschedule swap or a real new event has overwritten
# latest, leave it alone.
_CONDITIONAL_DEL_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('DEL', KEYS[2])
    return 1
end
return 0
"""

# reschedule CAS swap: only set latest = new trigger_id when latest is
# still trigger_id_orig (handler's). If a real new event has already taken
# over, no-op and let that timer fire.
_RESCHEDULE_CAS_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


# ---------------------------------------------------------------------------
# Public exception sentinel (signals lock contention from a business node)
# ---------------------------------------------------------------------------


class DebounceReschedule(Exception):
    """Raised by a debounce consumer when it can't process this fire and
    wants the handler to schedule another one.

    Usage::

        if not await redis.set(lock_key, token, nx=True, ex=600):
            raise DebounceReschedule(SameTrigger(...))

    The handler catches this, runs ``_do_reschedule(...)`` with its own
    trigger_id, and skips the conditional DEL so the fresh latest survives.

    Why a sentinel exception and not a public ``reschedule()`` function:
    Python copies contextvars into ``asyncio.create_task()``-spawned tasks.
    A public reschedule that read the handler's trigger_id from a contextvar
    could be called from a background task that inherited the var and run
    well after the handler's lifecycle ended. Sentinel raise from inside
    the consumer call keeps trigger_id confined to the handler's local
    scope.
    """

    def __init__(self, data: Data) -> None:
        super().__init__(f"debounce reschedule: {type(data).__name__}")
        self.data = data


# Note: there is intentionally no module-level `reschedule(data)` function.
# CAS swap + publish lives inside the handler via _do_reschedule below.


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_consumer_tags: list[tuple[Any, str]] = []


def _route_for(w: WireSpec, consumer: Callable) -> Route:
    """Build the (queue, routing_key, lane_fallback=False) Route for a debounce wire.

    debounce route ALWAYS sets lane_fallback=False — long delays
    (300s afterthought) cannot be short-circuited to prod by the lane
    queue's x-message-ttl=10000.
    """
    data_snake = to_snake(w.data_type.__name__)
    return Route(
        queue=f"debounce_{data_snake}_{consumer.__name__}",
        rk=f"debounce.{data_snake}.{consumer.__name__}",
        lane_fallback=False,
    )
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/runtime/debounce.py apps/agent-service/tests/unit/runtime/test_debounce.py
git commit -m "feat(runtime/debounce): module 骨架 + DebounceReschedule sentinel

常量 + 4 个 Lua 脚本 + DebounceReschedule(Exception) + _route_for。
没有 module-level reschedule() 公开函数（business node 通过 raise
sentinel 让 handler 在自己持有 trigger_id 的同步路径里跑 CAS swap，
避免 contextvar 跨 task 泄漏）。

Refs: spec §3.4.3 / §3.4.5 / reviewer round-7 M1"
```

---

## Task 7: publish_debounce 实现 + 单测

**Files:**
- Modify: `apps/agent-service/app/runtime/debounce.py`（加 publish_debounce）
- Test: `apps/agent-service/tests/unit/runtime/test_debounce.py`

- [ ] **Step 1: 写测试**

```python
# apps/agent-service/tests/unit/runtime/test_debounce.py 加
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.domain.memory_triggers import DriftTrigger
from app.runtime.debounce import publish_debounce
from app.runtime.wire import WireSpec


def _make_wire():
    return WireSpec(
        data_type=DriftTrigger,
        consumers=[_drift_check_stub],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"drift:{e.chat_id}:{e.persona_id}",
    )


@pytest.mark.asyncio
async def test_publish_debounce_single_event(monkeypatch):
    """单事件：写 latest + INCR count=1 + publish delay=60s 消息。"""
    fake_redis = AsyncMock()
    # _PUBLISH_LUA 返回 [count, fire_now] = [1, 0]
    fake_redis.eval = AsyncMock(return_value=[1, 0])
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.mq",
                        MagicMock(publish=fake_publish))
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: "tr-1"))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    w = _make_wire()
    await publish_debounce(w, _drift_check_stub, DriftTrigger(chat_id="c1", persona_id="p1"))

    # redis.eval 被调用一次 (publish_debounce 内的 _PUBLISH_LUA)
    fake_redis.eval.assert_awaited_once()
    args = fake_redis.eval.call_args
    assert args.args[1] == 2  # numkeys
    # KEYS[1] = latest, KEYS[2] = count
    assert "debounce:latest:DriftTrigger:drift:c1:p1" in args.args
    assert "debounce:count:DriftTrigger:drift:c1:p1" in args.args
    # ARGV[2] = ttl, ARGV[3] = max_buffer
    assert 86400 in args.args  # max(60*2, 86400)
    assert 3 in args.args      # max_buffer

    # mq.publish 一次
    fake_publish.assert_awaited_once()
    pub_args = fake_publish.call_args
    body = pub_args.args[1]
    assert body["fire_now"] is False
    assert body["data"] == {"chat_id": "c1", "persona_id": "p1"}
    assert body["key"] == "drift:c1:p1"
    assert pub_args.kwargs["delay_ms"] == 60_000


@pytest.mark.asyncio
async def test_publish_debounce_max_buffer_triggers_fire_now(monkeypatch):
    """count 达 max_buffer 时 publish_debounce 拿到 fire_now=1，
    publish delay=0 + body.fire_now=True。"""
    fake_redis = AsyncMock()
    # Lua 返回 [3, 1]：count 到 3 (max_buffer)，fire_now=1
    fake_redis.eval = AsyncMock(return_value=[3, 1])
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.mq",
                        MagicMock(publish=fake_publish))
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    w = _make_wire()
    await publish_debounce(w, _drift_check_stub, DriftTrigger(chat_id="c1", persona_id="p1"))

    pub_args = fake_publish.call_args
    body = pub_args.args[1]
    assert body["fire_now"] is True
    assert pub_args.kwargs["delay_ms"] == 0
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v -k publish_debounce`
Expected: FAIL — `publish_debounce` 还没实现

- [ ] **Step 3: 实现 publish_debounce**

加到 `apps/agent-service/app/runtime/debounce.py`：

```python
async def publish_debounce(w: WireSpec, consumer: Callable, data: Data) -> None:
    """Upstream emit path for a debounced wire.

    Atomically SETs latest = trigger_id (overwriting any older one) and
    INCRs count. If count crosses max_buffer, the Lua resets count to 0
    and flags this publish as fire_now=1 (delay=0 immediate fire).
    """
    key = w.debounce_key_by(data)
    seconds = w.debounce["seconds"]
    max_buffer = w.debounce["max_buffer"]
    trigger_id = uuid.uuid4().hex
    redis = await get_redis()
    redis_latest = f"debounce:latest:{w.data_type.__name__}:{key}"
    redis_count = f"debounce:count:{w.data_type.__name__}:{key}"
    ttl_seconds = max(seconds * 2, _DEFAULT_TTL_SECONDS)

    result = await redis.eval(
        _PUBLISH_LUA, 2,
        redis_latest, redis_count,
        trigger_id, ttl_seconds, max_buffer,
    )
    new_count, fire_now_flag = int(result[0]), int(result[1])

    body = {
        "trigger_id": trigger_id,
        "data": data.model_dump(mode="json"),
        "key": key,
        "fire_now": bool(fire_now_flag),
    }
    headers = {
        "trace_id": trace_id_var.get() or "",
        "lane": lane_var.get() or "",
        "data_type": type(data).__name__,
    }
    delay_ms = 0 if body["fire_now"] else seconds * 1000
    await mq.publish(_route_for(w, consumer), body, headers=headers, delay_ms=delay_ms)
    logger.debug(
        "debounce publish: %s key=%s count=%d fire_now=%s",
        w.data_type.__name__, key, new_count, body["fire_now"],
    )
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v -k publish_debounce`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/runtime/debounce.py apps/agent-service/tests/unit/runtime/test_debounce.py
git commit -m "feat(runtime/debounce): publish_debounce + atomic max_buffer reset

每次 emit 写 redis latest + INCR count + publish delay 消息；
count 达 max_buffer 时 Lua 原子重置 count = 0 并通知 publish 端
发 fire_now=True + delay=0 消息（每轮只一条 immediate fire 携带正确 trigger_id）。

Refs: spec §3.4.3 publish_debounce"
```

---

## Task 8: _do_reschedule 实现 + 单测

**Files:**
- Modify: `apps/agent-service/app/runtime/debounce.py`（加 `_do_reschedule`）
- Test: `apps/agent-service/tests/unit/runtime/test_debounce.py`

- [ ] **Step 1: 写测试**

```python
# apps/agent-service/tests/unit/runtime/test_debounce.py 加
from app.runtime.debounce import _do_reschedule


@pytest.mark.asyncio
async def test_do_reschedule_cas_swap_success(monkeypatch):
    """latest == trigger_id_orig → swap to new + publish delay."""
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=1)  # CAS swap 成功
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.mq",
                        MagicMock(publish=fake_publish))
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    w = _make_wire()
    data = DriftTrigger(chat_id="c1", persona_id="p1")
    await _do_reschedule(w, _drift_check_stub, data, trigger_id_orig="orig-123")

    fake_redis.eval.assert_awaited_once()
    fake_publish.assert_awaited_once()
    pub_args = fake_publish.call_args
    assert pub_args.args[1]["fire_now"] is False
    assert pub_args.kwargs["delay_ms"] == 60_000


@pytest.mark.asyncio
async def test_do_reschedule_cas_swap_failure_noop(monkeypatch):
    """latest 已被新事件覆盖（!= trigger_id_orig）→ no-op，不 publish。"""
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=0)  # CAS swap 失败
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.mq",
                        MagicMock(publish=fake_publish))

    w = _make_wire()
    data = DriftTrigger(chat_id="c1", persona_id="p1")
    await _do_reschedule(w, _drift_check_stub, data, trigger_id_orig="orig-123")

    fake_redis.eval.assert_awaited_once()
    fake_publish.assert_not_awaited()  # 不 publish
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v -k do_reschedule`
Expected: FAIL — `_do_reschedule` 不存在

- [ ] **Step 3: 实现 _do_reschedule**

加到 `apps/agent-service/app/runtime/debounce.py`（放在 `publish_debounce` 之后、`_build_handler` 之前）：

```python
async def _do_reschedule(
    w: WireSpec, consumer: Callable, data: Data, trigger_id_orig: str,
) -> None:
    """Handler-internal reschedule: CAS swap latest + publish delay.

    Called by ``_build_handler`` when the consumer raises
    ``DebounceReschedule``. ``trigger_id_orig`` is the handler's local
    trigger_id (the one whose atomic-claim succeeded), passed in as a plain
    parameter so it never escapes through a contextvar.

    The Lua CAS swap only writes the new trigger_id when latest is still
    trigger_id_orig (no real new event has taken over). On collision, this
    no-ops and lets the new event's timer drive the next fire.
    """
    key = w.debounce_key_by(data)
    seconds = w.debounce["seconds"]
    new_trigger_id = uuid.uuid4().hex
    redis = await get_redis()
    redis_latest = f"debounce:latest:{w.data_type.__name__}:{key}"
    ttl_seconds = max(seconds * 2, _DEFAULT_TTL_SECONDS)

    swapped = await redis.eval(
        _RESCHEDULE_CAS_LUA, 1,
        redis_latest, trigger_id_orig, new_trigger_id, ttl_seconds,
    )
    if not int(swapped):
        logger.debug(
            "_do_reschedule no-op: latest already replaced for %s key=%s",
            type(data).__name__, key,
        )
        return

    body = {
        "trigger_id": new_trigger_id,
        "data": data.model_dump(mode="json"),
        "key": key,
        "fire_now": False,
    }
    headers = {
        "trace_id": trace_id_var.get() or "",
        "lane": lane_var.get() or "",
        "data_type": type(data).__name__,
    }
    await mq.publish(_route_for(w, consumer), body, headers=headers,
                     delay_ms=seconds * 1000)
    logger.info(
        "debounce reschedule: %s key=%s new_trigger_id=%s",
        type(data).__name__, key, new_trigger_id,
    )
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v -k do_reschedule`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/runtime/debounce.py apps/agent-service/tests/unit/runtime/test_debounce.py
git commit -m "feat(runtime/debounce): _do_reschedule CAS swap + publish

handler-internal reschedule: 仅当 latest == trigger_id_orig 才 swap 成
new_trigger_id + publish delay 消息；swap 失败 no-op 让真实新事件 timer 接管。
trigger_id_orig 由 handler 局部变量提供，不依赖 contextvar
（reviewer round-7 M1：避免 background task 跨 task 误用）。

Refs: spec §3.4.3 _do_reschedule"
```

---

## Task 9: _build_handler 实现 + 单测（atomic claim + DebounceReschedule + conditional DEL）

**Files:**
- Modify: `apps/agent-service/app/runtime/debounce.py`（加 `_build_handler`）
- Test: `apps/agent-service/tests/unit/runtime/test_debounce.py`

- [ ] **Step 1: 写测试 — 7 case**

```python
# apps/agent-service/tests/unit/runtime/test_debounce.py 加
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, call
from app.runtime.debounce import _build_handler, DebounceReschedule


class _FakeMessage:
    """Minimal aio_pika message stub。"""
    def __init__(self, body: bytes, headers: dict):
        self.body = body
        self.headers = headers
        self.processed_with_requeue = None
        self.exception_raised = None

    @asynccontextmanager
    async def process(self, *, requeue: bool):
        self.processed_with_requeue = requeue
        try:
            yield
        except Exception as e:
            self.exception_raised = e
            raise


def _make_message(trigger_id, data, key, fire_now=False, headers=None):
    body = {
        "trigger_id": trigger_id,
        "data": data,
        "key": key,
        "fire_now": fire_now,
    }
    return _FakeMessage(
        body=json.dumps(body).encode("utf-8"),
        headers=headers or {"trace_id": "tr-1", "lane": "", "data_type": "DriftTrigger"},
    )


@pytest.mark.asyncio
async def test_handler_atomic_claim_success_runs_consumer_then_conditional_del(monkeypatch):
    fake_redis = AsyncMock()
    # _CLAIM_LUA = 1 (claimed); _CONDITIONAL_DEL_LUA = 1 (deleted)
    fake_redis.eval = AsyncMock(side_effect=[1, 1])
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    consumer_called = []

    async def consumer(trigger):
        consumer_called.append(trigger)

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )
    handler = _build_handler(w, consumer)
    msg = _make_message("trig-1",
                        {"chat_id": "c1", "persona_id": "p1"},
                        "k:c1")
    await handler(msg)

    # consumer 调用且 conditional DEL 跑了
    assert len(consumer_called) == 1
    assert consumer_called[0].chat_id == "c1"
    # 两次 redis.eval：claim + conditional DEL
    assert fake_redis.eval.await_count == 2


@pytest.mark.asyncio
async def test_handler_stale_trigger_id_drops(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=0)  # _CLAIM_LUA = 0 (stale)
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    consumer_called = []

    async def consumer(trigger):
        consumer_called.append(trigger)

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )
    handler = _build_handler(w, consumer)
    msg = _make_message("trig-stale",
                        {"chat_id": "c1", "persona_id": "p1"},
                        "k:c1")
    await handler(msg)

    # consumer 不被调
    assert len(consumer_called) == 0
    # claim 失败后不会再做 conditional DEL
    assert fake_redis.eval.await_count == 1


@pytest.mark.asyncio
async def test_handler_fire_now_still_runs_atomic_claim(monkeypatch):
    """fire_now=True 仍走 atomic claim（防 backlog 旧 fire_now 重复触发，round-1 H3）。"""
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(side_effect=[1, 1])
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    consumer_called = []

    async def consumer(trigger):
        consumer_called.append(trigger)

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )
    handler = _build_handler(w, consumer)
    msg = _make_message("trig-now",
                        {"chat_id": "c1", "persona_id": "p1"},
                        "k:c1", fire_now=True)
    await handler(msg)

    # claim 仍然执行
    assert fake_redis.eval.await_count == 2
    assert len(consumer_called) == 1


@pytest.mark.asyncio
async def test_handler_consumer_raises_skips_conditional_del(monkeypatch):
    """Consumer 抛非 DebounceReschedule 异常 → 跳过 conditional DEL → DLQ 路径。"""
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=1)  # claim succeeded
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    async def consumer(trigger):
        raise RuntimeError("boom")

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )
    handler = _build_handler(w, consumer)
    msg = _make_message("trig-err",
                        {"chat_id": "c1", "persona_id": "p1"},
                        "k:c1")
    with pytest.raises(RuntimeError):
        await handler(msg)

    # 只 claim 一次，没跑 conditional DEL（latest 保留供 DLQ replay）
    assert fake_redis.eval.await_count == 1
    assert msg.processed_with_requeue is False  # nack to DLX


@pytest.mark.asyncio
async def test_handler_consumer_raises_debounce_reschedule_runs_do_reschedule(monkeypatch):
    """业务节点 raise DebounceReschedule → handler catch + _do_reschedule。"""
    fake_redis = AsyncMock()
    # claim succeeded, 然后 _do_reschedule 内部 CAS swap = 1
    fake_redis.eval = AsyncMock(side_effect=[1, 1])
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.mq",
                        MagicMock(publish=fake_publish))
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    new_data = DriftTrigger(chat_id="c1", persona_id="p1")

    async def consumer(trigger):
        raise DebounceReschedule(new_data)

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )
    handler = _build_handler(w, consumer)
    msg = _make_message("trig-resched",
                        {"chat_id": "c1", "persona_id": "p1"},
                        "k:c1")
    await handler(msg)

    # claim + CAS swap，没跑 conditional DEL
    assert fake_redis.eval.await_count == 2
    fake_publish.assert_awaited_once()
    # mq.process 正常 ack（DebounceReschedule 不冒泡进 DLQ）
    assert msg.exception_raised is None
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v -k handler`
Expected: FAIL — `_build_handler` 不存在

- [ ] **Step 3: 实现 _build_handler**

加到 `apps/agent-service/app/runtime/debounce.py`（放在 `_do_reschedule` 之后）：

```python
def _build_handler(w: WireSpec, consumer: Callable):
    """Build the aio-pika message handler for one ``(wire, consumer)`` pair.

    Flow per message:
      1. Restore trace_id / lane contextvars from headers
      2. Decode body, extract trigger_id / data / key
      3. Atomic claim: stale check + clear count = 0 (Lua)
         - claim returns 0 → drop (stale, message ack-ed by message.process)
      4. Decode Data, call consumer:
         (a) returns normally → conditional DEL latest+count if still ours
         (b) raises DebounceReschedule(new_data) → run _do_reschedule with
             our trigger_id; skip conditional DEL (CAS already wrote new
             latest, or CAS no-op'd because a real new event took over —
             either way we don't want to clobber)
         (c) raises any other exception → propagate out of message.process,
             which nacks (requeue=False) to DLX. Skip conditional DEL so
             latest survives for a manual DLQ replay (count is gone — see
             §4.1; replay restores fire signal only, not count)
    """
    data_cls = w.data_type
    param_name = next(iter(inputs_of(consumer)))

    async def handler(message: AbstractIncomingMessage) -> None:
        async with message.process(requeue=False):
            headers = message.headers or {}
            raw_trace = headers.get("trace_id")
            trace_id = raw_trace if isinstance(raw_trace, str) and raw_trace else None
            raw_lane = headers.get("lane")
            lane = raw_lane if isinstance(raw_lane, str) and raw_lane else None
            t_tok = trace_id_var.set(trace_id)
            l_tok = lane_var.set(lane)
            try:
                payload = json.loads(message.body)
                trigger_id = payload["trigger_id"]
                data_dict = payload["data"]
                key = payload["key"]

                redis = await get_redis()
                redis_latest = f"debounce:latest:{data_cls.__name__}:{key}"
                redis_count = f"debounce:count:{data_cls.__name__}:{key}"
                ttl_seconds = max(w.debounce["seconds"] * 2, _DEFAULT_TTL_SECONDS)

                claimed = await redis.eval(
                    _CLAIM_LUA, 2,
                    redis_latest, redis_count,
                    trigger_id, ttl_seconds,
                )
                if not int(claimed):
                    logger.debug(
                        "debounce drop stale: %s key=%s trigger_id=%s",
                        data_cls.__name__, key, trigger_id,
                    )
                    return

                obj = data_cls(**data_dict)
                logger.info(
                    "debounce fire: %s key=%s trigger_id=%s",
                    data_cls.__name__, key, trigger_id,
                )

                try:
                    await consumer(**{param_name: obj})
                except DebounceReschedule as resched:
                    logger.info(
                        "debounce reschedule: %s key=%s old_trigger_id=%s",
                        data_cls.__name__, key, trigger_id,
                    )
                    await _do_reschedule(w, consumer, resched.data, trigger_id)
                    return  # 不走 conditional DEL：CAS 已处理 latest

                # consumer 正常 return → conditional DEL
                await redis.eval(
                    _CONDITIONAL_DEL_LUA, 2,
                    redis_latest, redis_count,
                    trigger_id,
                )
            finally:
                trace_id_var.reset(t_tok)
                lane_var.reset(l_tok)

    return handler
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v -k handler`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/runtime/debounce.py apps/agent-service/tests/unit/runtime/test_debounce.py
git commit -m "feat(runtime/debounce): _build_handler atomic claim + DebounceReschedule catch

Handler 三种结束路径：
  (a) 正常 return → conditional DEL（仅删自己 trigger_id）
  (b) raise DebounceReschedule(data) → handler 调 _do_reschedule（trigger_id
      由 handler 局部变量提供），不 conditional DEL
  (c) 其他异常 → message.process(requeue=False) 进 DLX，跳过 conditional
      DEL，latest 保留供 DLQ replay
Atomic claim Lua（stale check + clear count=0）保证 phase2 期间业务真新事件
不会跟旧积累混在一起。

Refs: spec §3.4.3 _build_handler"
```

---

## Task 10: start_debounce_consumers / stop_debounce_consumers

**Files:**
- Modify: `apps/agent-service/app/runtime/debounce.py`（加 start/stop）
- Test: `apps/agent-service/tests/unit/runtime/test_debounce.py`

- [ ] **Step 1: 写测试**

```python
# apps/agent-service/tests/unit/runtime/test_debounce.py 加
@pytest.mark.asyncio
async def test_start_debounce_consumers_filters_by_app_name(monkeypatch):
    """start_debounce_consumers(app_name) 用 nodes_for_app 过滤；
    其他 app 的 wire 不启动 consumer。"""
    from app.runtime.wire import clear_wiring, wire
    from app.runtime.node import clear_nodes, node
    from app.runtime.placement import clear_bindings, bind
    from app.runtime.debounce import (
        start_debounce_consumers, stop_debounce_consumers,
    )

    clear_wiring()
    clear_nodes()
    clear_bindings()

    @node
    async def my_drift_check(t: DriftTrigger) -> None:
        return None

    wire(DriftTrigger).debounce(
        seconds=60, max_buffer=5,
        key_by=lambda e: f"k:{e.chat_id}",
    ).to(my_drift_check)

    # mock mq
    fake_mq = MagicMock()
    fake_mq.connect = AsyncMock()
    fake_mq.declare_topology = AsyncMock()
    fake_mq.declare_route = AsyncMock()
    fake_queue = MagicMock()
    fake_queue.cancel = AsyncMock()
    fake_mq.consume = AsyncMock(return_value=(fake_queue, "tag-1"))
    monkeypatch.setattr("app.runtime.debounce.mq", fake_mq)
    monkeypatch.setattr("app.runtime.debounce.current_lane", lambda: "")

    await start_debounce_consumers(app_name="agent-service")

    fake_mq.connect.assert_awaited_once()
    fake_mq.declare_route.assert_awaited_once()
    fake_mq.consume.assert_awaited_once()

    # 启动到不同 app（vectorize-worker）→ wire 被过滤掉
    await stop_debounce_consumers()
    fake_mq.connect.reset_mock()
    fake_mq.consume.reset_mock()

    await start_debounce_consumers(app_name="vectorize-worker")

    fake_mq.connect.assert_not_awaited()
    fake_mq.consume.assert_not_awaited()

    await stop_debounce_consumers()
    clear_wiring()
    clear_nodes()
    clear_bindings()
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v -k consumers`
Expected: FAIL — `start_debounce_consumers` 不存在

- [ ] **Step 3: 实现 start/stop_debounce_consumers**

加到 `apps/agent-service/app/runtime/debounce.py` 文件末尾：

```python
async def start_debounce_consumers(app_name: str | None = None) -> None:
    """Declare and start consumers for every ``.debounce()`` wire.

    Filters by ``app_name`` via ``nodes_for_app`` (wires whose consumers
    aren't bound to this app are skipped). compile_graph layer-4 already
    rejects mixed-app wires.
    """
    if _consumer_tags:
        raise RuntimeError(
            "debounce consumers already started; call stop_debounce_consumers() first"
        )
    from app.runtime.graph import compile_graph
    graph = compile_graph()

    allowed: set | None = None
    if app_name is not None:
        allowed = nodes_for_app(app_name)

    has_debounce = any(
        w.debounce is not None
        and (allowed is None or all(c in allowed for c in w.consumers))
        for w in graph.wires
    )
    if has_debounce:
        await mq.connect()
        await mq.declare_topology()

    for w in graph.wires:
        if w.debounce is None:
            continue
        if allowed is not None and not all(c in allowed for c in w.consumers):
            continue
        for consumer in w.consumers:
            route = _route_for(w, consumer)
            await mq.declare_route(route)
            handler = _build_handler(w, consumer)
            actual_queue = lane_queue(route.queue, current_lane())
            queue, tag = await mq.consume(actual_queue, handler)
            _consumer_tags.append((queue, tag))
            logger.info(
                "debounce consumer started: %s -> %s",
                actual_queue, consumer.__name__,
            )


async def stop_debounce_consumers() -> None:
    """Cancel every debounce consumer started by start_debounce_consumers."""
    for queue, tag in _consumer_tags:
        try:
            await queue.cancel(tag)
        except Exception as e:  # pragma: no cover — best effort on teardown
            logger.warning("failed to cancel debounce consumer %s: %s", tag, e)
    _consumer_tags.clear()
    await asyncio.sleep(0)
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_debounce.py -v -k consumers`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/runtime/debounce.py apps/agent-service/tests/unit/runtime/test_debounce.py
git commit -m "feat(runtime/debounce): start/stop_debounce_consumers + app_name 过滤

start_debounce_consumers(app_name='agent-service') 用 nodes_for_app 过滤
非本 app 的 wire；arq-worker / vectorize-worker 启动时传自己的 app_name
不会启 debounce consumer。

Refs: spec §3.4.3 / §3.5"
```

---

## Task 11: emit.py 加 debounce 分支

**Files:**
- Modify: `apps/agent-service/app/runtime/emit.py`（在 wire dispatch 循环加分支）
- Test: `apps/agent-service/tests/unit/runtime/test_emit.py`（已有；新增 case）

- [ ] **Step 1: 看 emit.py 当前结构**

Run: `cd apps/agent-service && grep -n "for w in\|w.durable\|w.sinks" app/runtime/emit.py`

记下 wire dispatch 循环的位置和结构（这步不改代码，只看清楚怎么插入 debounce 分支）。

- [ ] **Step 2: 写测试**

```python
# apps/agent-service/tests/unit/runtime/test_emit.py 加
import pytest
from unittest.mock import AsyncMock, patch
from app.runtime.emit import emit
from app.runtime.wire import wire, clear_wiring
from app.runtime.node import node, clear_nodes
from app.runtime.placement import clear_bindings
from app.domain.memory_triggers import DriftTrigger


@pytest.fixture
def _reset():
    clear_wiring()
    clear_nodes()
    clear_bindings()
    yield
    clear_wiring()
    clear_nodes()
    clear_bindings()


@pytest.mark.asyncio
async def test_emit_debounce_wire_calls_publish_debounce(_reset, monkeypatch):
    @node
    async def my_drift_check(t: DriftTrigger) -> None:
        return None

    wire(DriftTrigger).debounce(
        seconds=60, max_buffer=5,
        key_by=lambda e: f"k:{e.chat_id}",
    ).to(my_drift_check)

    # mock publish_debounce — 验证 emit 路由到它，而不是 in-process dispatch
    fake_publish_debounce = AsyncMock()
    monkeypatch.setattr("app.runtime.debounce.publish_debounce",
                        fake_publish_debounce)

    t = DriftTrigger(chat_id="c1", persona_id="p1")
    await emit(t)

    fake_publish_debounce.assert_awaited_once()
    args = fake_publish_debounce.call_args.args
    assert args[1] is my_drift_check
    assert args[2] == t
```

- [ ] **Step 3: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_emit.py -v -k debounce_wire`
Expected: FAIL — emit 还没 debounce 分支

- [ ] **Step 4: 修 emit.py 加 debounce 分支**

在 `apps/agent-service/app/runtime/emit.py` 的 wire dispatch 循环顶部加一支（具体位置基于 Step 1 的扫描结果，加在 `if w.durable:` 之前）：

```python
# emit.py wire dispatch 循环内
for w in [x for x in WIRING_REGISTRY if x.data_type == type(data)]:
    # debounce wire 走独立的 mq publish 路径，不参与 in-process / sink dispatch
    if w.debounce is not None:
        from app.runtime.debounce import publish_debounce
        for consumer in w.consumers:
            await publish_debounce(w, consumer, data)
        continue
    if w.durable:
        # ...existing durable dispatch...
        continue
    if w.sinks:
        # ...existing sink dispatch...
    # ...in-process consumer dispatch...
```

注意：**保留所有现有分支语义不变**，只在循环开头加一个 `if w.debounce is not None` 早返回。具体的 `continue` / 分支结构跟 emit.py 现有写法一致。

- [ ] **Step 5: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_emit.py -v -k debounce_wire`
Expected: PASS

- [ ] **Step 6: 跑全 emit 测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/runtime/test_emit.py -v`
Expected: 全部 PASS（含原有 case）

- [ ] **Step 7: Commit**

```bash
git add apps/agent-service/app/runtime/emit.py apps/agent-service/tests/unit/runtime/test_emit.py
git commit -m "feat(runtime/emit): wire dispatch 加 debounce 分支

debounce wire 走独立的 mq publish 路径（publish_debounce），不参与
in-process / sink / durable dispatch。

Refs: spec §3.4.4"
```

---

## Task 12: 创建 nodes/memory_pipelines.py 骨架 + 搬迁业务 helper

**Files:**
- Create: `apps/agent-service/app/nodes/memory_pipelines.py`
- Test: `apps/agent-service/tests/unit/nodes/test_memory_pipelines.py`（新增）

- [ ] **Step 1: 创建 memory_pipelines.py 骨架（搬现有 helper）**

把 `app/memory/drift.py` 里的 `_run_drift` / `_recent_timeline` / `_recent_persona_replies` 整段搬过来；把 `app/memory/afterthought.py` 里的 `_generate_fragment` / `_build_scene` / 常量整段搬过来。**仅搬迁，不改业务逻辑**。

```python
# apps/agent-service/app/nodes/memory_pipelines.py
"""Memory pipeline @node consumers (drift / afterthought).

Single-flight via redis SETNX + uuid token + Lua compare-and-delete release.
Lock contention raises ``DebounceReschedule`` so the debounce handler
schedules another fire after phase2 completes.

Helpers (_run_drift, _recent_timeline, _recent_persona_replies,
_generate_fragment, _build_scene) are migrated verbatim from the old
app/memory/drift.py / app/memory/afterthought.py — Phase 3 only changes
the dispatch layer, not the business logic.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig, extract_text
from app.chat.content_parser import parse_content
from app.data.ids import new_id
from app.data.queries import (
    find_group_name,
    find_messages_in_range,
    find_username,
    insert_fragment,
    resolve_bot_name_for_persona,
)
from app.data.session import get_session
from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
from app.infra.config import settings
from app.infra.redis import get_redis
from app.memory._persona import load_persona
from app.memory._timeline import format_timeline
from app.memory.vectorize_memory import enqueue_fragment_vectorize
from app.runtime.debounce import DebounceReschedule
from app.runtime.node import node

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_LOOKBACK_HOURS = 2

_AFTERTHOUGHT_CFG = AgentConfig(
    "afterthought_conversation", "offline-model", "afterthought"
)

# Lua: compare-and-delete release lock。仅当 redis 上还是自己 token 时才 DEL，
# 避免 LLM 卡过 TTL 后旧 finally 误删新 fire 拿到的同 key 锁（reviewer round-2 H2）
_LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
end
return 0
"""


# ---------------------------------------------------------------------------
# Drift helpers (migrated from app/memory/drift.py)
# ---------------------------------------------------------------------------

async def _run_drift(chat_id: str, persona_id: str) -> None:
    """Event-driven drift — call unified voice generation with recent context."""
    pc = await load_persona(persona_id)
    recent_messages = await _recent_timeline(chat_id, persona_name=pc.display_name)
    recent_replies = await _recent_persona_replies(chat_id, persona_id)

    if not recent_messages:
        logger.info("[%s] No recent messages for %s, skip drift", persona_id, chat_id)
        return

    parts: list[str] = []
    if recent_messages:
        parts.append(f"群里刚才发生的事：\n{recent_messages}")
    if recent_replies:
        parts.append(f"你最近的回复：\n{recent_replies}")
    recent_context = "\n\n".join(parts)

    from app.memory.voice import generate_voice
    await generate_voice(persona_id, recent_context=recent_context, source="drift")


async def _recent_timeline(
    chat_id: str, persona_name: str = "bot", max_messages: int = 50
) -> str:
    """Last 1 hour of messages formatted as timeline."""
    start_dt = datetime.now(_CST) - timedelta(hours=1)
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(datetime.now(_CST).timestamp() * 1000)

    async with get_session() as s:
        messages = await find_messages_in_range(s, chat_id, start_ts, end_ts)
    if not messages:
        return ""

    return await format_timeline(
        messages, persona_name, tz=_CST, max_messages=max_messages
    )


async def _recent_persona_replies(
    chat_id: str, persona_id: str, max_replies: int = 10
) -> str:
    """Recent bot replies for drift diagnosis (matched by bot_name)."""
    now = datetime.now(_CST)
    start_ts = int((now - timedelta(hours=2)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    async with get_session() as s:
        messages = await find_messages_in_range(s, chat_id, start_ts, end_ts)
        if not messages:
            return ""
        bot_name = await resolve_bot_name_for_persona(s, persona_id, chat_id)

    persona_msgs = [
        m for m in messages if m.role == "assistant" and m.bot_name == bot_name
    ]
    persona_msgs = persona_msgs[-max_replies:]

    lines: list[str] = []
    for i, msg in enumerate(persona_msgs, 1):
        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"{i}. {rendered[:200]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Afterthought helpers (migrated from app/memory/afterthought.py)
# ---------------------------------------------------------------------------

async def _generate_fragment(chat_id: str, persona_id: str) -> None:
    """Generate a conversation-grain experience fragment."""
    now = datetime.now(_CST)
    start_ts = int((now - timedelta(hours=_LOOKBACK_HOURS)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    async with get_session() as s:
        messages = await find_messages_in_range(s, chat_id, start_ts, end_ts)

    if not messages:
        logger.info(
            "[%s] No messages in last %dh for %s, skip",
            persona_id, _LOOKBACK_HOURS, chat_id,
        )
        return

    chat_type = messages[0].chat_type if messages else "group"

    pc = await load_persona(persona_id)
    scene = await _build_scene(chat_id, chat_type, messages)
    timeline = await format_timeline(messages, pc.display_name, tz=_CST)
    if not timeline:
        logger.info("[%s] Empty timeline for %s, skip", persona_id, chat_id)
        return

    result = await Agent(_AFTERTHOUGHT_CFG).run(
        prompt_vars={
            "persona_name": pc.display_name,
            "persona_lite": pc.persona_lite,
            "scene": scene,
            "messages": timeline,
        },
        messages=[HumanMessage(content="生成经历碎片")],
    )
    content = extract_text(result.content)

    if not content:
        logger.warning(
            "[%s] Afterthought LLM returned empty for %s", persona_id, chat_id
        )
        return

    fid = new_id("f")
    async with get_session() as s:
        await insert_fragment(
            s,
            id=fid,
            persona_id=persona_id,
            content=content,
            source="afterthought",
            chat_id=chat_id,
        )
    await enqueue_fragment_vectorize(fid)
    logger.info(
        "[%s] Conversation fragment created for %s: %s...",
        persona_id, chat_id, content[:60],
    )


async def _build_scene(chat_id: str, chat_type: str, messages: list) -> str:
    """Build scene description for the prompt."""
    if chat_type == "p2p":
        for msg in messages:
            if msg.role == "user" and msg.user_id:
                async with get_session() as s:
                    name = await find_username(s, msg.user_id)
                if name:
                    return f"和{name}的私聊"
        return "一段私聊"

    try:
        async with get_session() as s:
            group_name = await find_group_name(s, chat_id)
        if group_name:
            return f"在「{group_name}」群里"
    except Exception:
        pass
    return "在群里"
```

- [ ] **Step 2: 复制原单元测试到新位置**

```bash
# 把 tests/unit/memory/test_drift.py 拷到 tests/unit/nodes/test_memory_pipelines_helpers.py
# 把 import 路径从 app.memory.drift 改成 app.nodes.memory_pipelines
mkdir -p apps/agent-service/tests/unit/nodes
cp apps/agent-service/tests/unit/memory/test_drift.py \
   apps/agent-service/tests/unit/nodes/test_memory_pipelines_helpers.py
# 同样拷 afterthought 测试（如果有）
```

然后用 sed 或手工把 import 改成 `from app.nodes.memory_pipelines import _run_drift, _recent_timeline, ...`。

如果原 tests/unit/memory/ 没有专门测试，跳过这一步。

- [ ] **Step 3: 跑搬迁后的 helper 测试**

Run: `cd apps/agent-service && uv run pytest tests/unit/nodes/test_memory_pipelines_helpers.py -v`
Expected: 全部 PASS（业务逻辑没改，仅 import 路径换）

- [ ] **Step 4: Commit（仅搬迁，节点还没加）**

```bash
git add apps/agent-service/app/nodes/memory_pipelines.py apps/agent-service/tests/unit/nodes/
git commit -m "refactor(nodes): 搬 _run_drift / _generate_fragment / 等 helper 进 memory_pipelines

从 app/memory/drift.py + afterthought.py 整段搬迁到 nodes/memory_pipelines.py，
业务逻辑零改动，准备 Phase 3 节点化。

Refs: spec §3.2 / §3.7"
```

---

## Task 13: drift_check / afterthought_check 节点 + 单测

**Files:**
- Modify: `apps/agent-service/app/nodes/memory_pipelines.py`（加节点）
- Test: `apps/agent-service/tests/unit/nodes/test_memory_pipelines.py`

- [ ] **Step 1: 写节点测试**

```python
# apps/agent-service/tests/unit/nodes/test_memory_pipelines.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
from app.nodes.memory_pipelines import (
    _LOCK_RELEASE_LUA, afterthought_check, drift_check,
)
from app.runtime.debounce import DebounceReschedule


@pytest.mark.asyncio
async def test_drift_check_lock_acquired_runs_run_drift_and_releases(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)  # SETNX 成功
    fake_redis.eval = AsyncMock(return_value=1)  # release 删掉了
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_run_drift = AsyncMock()
    monkeypatch.setattr("app.nodes.memory_pipelines._run_drift", fake_run_drift)

    await drift_check(DriftTrigger(chat_id="c1", persona_id="p1"))

    fake_run_drift.assert_awaited_once_with("c1", "p1")
    # release 用 Lua compare-and-delete
    fake_redis.eval.assert_awaited_once()
    assert fake_redis.eval.call_args.args[0] == _LOCK_RELEASE_LUA


@pytest.mark.asyncio
async def test_drift_check_lock_busy_raises_debounce_reschedule(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=False)  # SETNX 失败
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_run_drift = AsyncMock()
    monkeypatch.setattr("app.nodes.memory_pipelines._run_drift", fake_run_drift)

    with pytest.raises(DebounceReschedule) as exc_info:
        await drift_check(DriftTrigger(chat_id="c1", persona_id="p1"))

    # 携带的 data 是同类型 trigger
    assert isinstance(exc_info.value.data, DriftTrigger)
    assert exc_info.value.data.chat_id == "c1"
    assert exc_info.value.data.persona_id == "p1"
    # _run_drift 没被调
    fake_run_drift.assert_not_awaited()
    # release 没跑（锁没拿到）
    fake_redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_drift_check_run_drift_raises_still_releases_lock(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.eval = AsyncMock(return_value=1)
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    monkeypatch.setattr("app.nodes.memory_pipelines._run_drift",
                        AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        await drift_check(DriftTrigger(chat_id="c1", persona_id="p1"))

    # finally 释放锁
    fake_redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_drift_check_release_uses_token_compare_and_delete(monkeypatch):
    """LLM 卡过 TTL 后旧 finally 不能误删新锁：Lua compare-and-delete
    在 token 不匹配时返回 0，不动 redis（reviewer round-2 H2）。"""
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.eval = AsyncMock(return_value=0)  # token 已经不是自己 → 不 DEL
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    fake_run_drift = AsyncMock()
    monkeypatch.setattr("app.nodes.memory_pipelines._run_drift", fake_run_drift)

    await drift_check(DriftTrigger(chat_id="c1", persona_id="p1"))

    # release 跑了但 Lua 返回 0（token 不匹配，新锁保留）
    fake_redis.eval.assert_awaited_once()
    fake_run_drift.assert_awaited_once()


# afterthought_check 同模式 4 个 test，省略（直接 mirror 上面）
@pytest.mark.asyncio
async def test_afterthought_check_lock_busy_raises_debounce_reschedule(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=False)
    monkeypatch.setattr("app.nodes.memory_pipelines.get_redis",
                        AsyncMock(return_value=fake_redis))

    monkeypatch.setattr("app.nodes.memory_pipelines._generate_fragment",
                        AsyncMock())

    with pytest.raises(DebounceReschedule) as exc_info:
        await afterthought_check(AfterthoughtTrigger(chat_id="c1", persona_id="p1"))

    assert isinstance(exc_info.value.data, AfterthoughtTrigger)
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/nodes/test_memory_pipelines.py -v`
Expected: FAIL — `drift_check` / `afterthought_check` 不存在

- [ ] **Step 3: 加节点到 memory_pipelines.py**

加到 `apps/agent-service/app/nodes/memory_pipelines.py` 末尾：

```python
# ---------------------------------------------------------------------------
# @node consumers
# ---------------------------------------------------------------------------

@node
async def drift_check(trigger: DriftTrigger) -> None:
    """Single-flight drift detection per (chat, persona).

    Lock contention raises DebounceReschedule(SameTrigger) — the debounce
    handler catches and runs _do_reschedule with its own trigger_id, so a
    fresh delayed fire takes phase2's place after the lock releases.

    Lock release uses compare-and-delete (Lua) keyed on a uuid token, so
    if LLM stalls past TTL and a new fire grabs the lock, the old finally
    sees a different token and leaves the new lock alone (reviewer round-2 H2).
    """
    lock_key = f"phase2:drift:{trigger.chat_id}:{trigger.persona_id}"
    token = uuid.uuid4().hex
    redis = await get_redis()
    if not await redis.set(lock_key, token, nx=True, ex=600):
        logger.info(
            "drift_check: phase2 in flight for chat_id=%s persona=%s, raise DebounceReschedule",
            trigger.chat_id, trigger.persona_id,
        )
        raise DebounceReschedule(DriftTrigger(
            chat_id=trigger.chat_id, persona_id=trigger.persona_id,
        ))
    try:
        await _run_drift(trigger.chat_id, trigger.persona_id)
    finally:
        await redis.eval(_LOCK_RELEASE_LUA, 1, lock_key, token)


@node
async def afterthought_check(trigger: AfterthoughtTrigger) -> None:
    """Single-flight conversation fragment generation per (chat, persona)."""
    lock_key = f"phase2:afterthought:{trigger.chat_id}:{trigger.persona_id}"
    token = uuid.uuid4().hex
    redis = await get_redis()
    if not await redis.set(lock_key, token, nx=True, ex=900):
        logger.info(
            "afterthought_check: phase2 in flight for chat_id=%s persona=%s, raise DebounceReschedule",
            trigger.chat_id, trigger.persona_id,
        )
        raise DebounceReschedule(AfterthoughtTrigger(
            chat_id=trigger.chat_id, persona_id=trigger.persona_id,
        ))
    try:
        await _generate_fragment(trigger.chat_id, trigger.persona_id)
    finally:
        await redis.eval(_LOCK_RELEASE_LUA, 1, lock_key, token)
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/nodes/test_memory_pipelines.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/nodes/memory_pipelines.py apps/agent-service/tests/unit/nodes/test_memory_pipelines.py
git commit -m "feat(nodes): drift_check / afterthought_check single-flight @nodes

SETNX uuid token + Lua compare-and-delete release；锁冲突 raise
DebounceReschedule(SameTrigger) 让 handler 跑 reschedule（不直接调 emit
也不直接调 module-level reschedule，避免 contextvar 跨 task 泄漏）。

Refs: spec §3.2 / reviewer round-2 H2 + round-7 M1"
```

---

## Task 14: wiring/memory.py 声明

**Files:**
- Create: `apps/agent-service/app/wiring/memory.py`
- Test: `apps/agent-service/tests/unit/wiring/test_memory.py`

- [ ] **Step 1: 写测试**

```python
# apps/agent-service/tests/unit/wiring/test_memory.py
def test_wiring_memory_compiles():
    """Importing app.wiring.memory must succeed compile_graph()."""
    from app.runtime.wire import clear_wiring
    from app.runtime.node import clear_nodes
    from app.runtime.placement import clear_bindings
    clear_wiring()
    clear_nodes()
    clear_bindings()

    import importlib
    import app.wiring.memory
    importlib.reload(app.wiring.memory)

    from app.runtime.graph import compile_graph
    g = compile_graph()  # 不抛
    from app.domain.memory_triggers import DriftTrigger, AfterthoughtTrigger
    assert DriftTrigger in g.data_types
    assert AfterthoughtTrigger in g.data_types
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/wiring/test_memory.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: 创建 wiring/memory.py**

```python
# apps/agent-service/app/wiring/memory.py
"""Wire declarations for memory pipelines (drift / afterthought).

drift_check and afterthought_check don't need bind() — placement.DEFAULT_APP
== "agent-service" already covers them. start_debounce_consumers(
app_name="agent-service") picks them up via nodes_for_app.
"""

from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
from app.infra.config import settings
from app.nodes.memory_pipelines import afterthought_check, drift_check
from app.runtime.wire import wire

wire(DriftTrigger).debounce(
    seconds=settings.identity_drift_debounce_seconds,
    max_buffer=settings.identity_drift_max_buffer,
    key_by=lambda e: f"drift:{e.chat_id}:{e.persona_id}",
).to(drift_check)

wire(AfterthoughtTrigger).debounce(
    seconds=300,
    max_buffer=15,
    key_by=lambda e: f"afterthought:{e.chat_id}:{e.persona_id}",
).to(afterthought_check)
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/wiring/test_memory.py -v`
Expected: PASS

- [ ] **Step 5: 确认 wiring 在启动时被 import**

Check: `cd apps/agent-service && grep -rn "import app.wiring" app/main.py app/runtime/`

如果 `app.wiring.memory` 没在启动时被 import（其他 wiring 模块怎么入口的，比如 `app.wiring.safety`），加到同样位置（通常是 `app/main.py` 或 `app/wiring/__init__.py` 一并 import）。

如果 `app/wiring/__init__.py` 已经 `from . import memory`，跳过这步；否则加上。

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/wiring/memory.py apps/agent-service/tests/unit/wiring/test_memory.py
git commit -m "feat(wiring): DriftTrigger / AfterthoughtTrigger debounce wires

DriftTrigger 用 settings.identity_drift_debounce_seconds / max_buffer；
AfterthoughtTrigger 用 300s / 15。drift_check / afterthought_check 走
DEFAULT_APP=agent-service，不需要 bind()。

Refs: spec §3.3"
```

---

## Task 15: main.py lifespan 加 start/stop_debounce_consumers

**Files:**
- Modify: `apps/agent-service/app/main.py`（lifespan startup / shutdown）
- Test: 端到端，没有专门 unit test（lifespan 难单测）

- [ ] **Step 1: 看 main.py 现有 lifespan 结构**

Run: `cd apps/agent-service && grep -n "start_consumers\|stop_consumers\|lifespan" app/main.py`

记下 `start_consumers(app_name=...)` 调用位置和 `stop_consumers()` 调用位置。

- [ ] **Step 2: 在 main.py 加 start_debounce_consumers / stop_debounce_consumers**

修改 `apps/agent-service/app/main.py`：

在 startup 路径中（`start_consumers(app_name="agent-service")` 调用之后）加：

```python
from app.runtime.debounce import start_debounce_consumers
await start_debounce_consumers(app_name="agent-service")
```

在 shutdown 路径中（`stop_consumers()` 调用之前）加：

```python
from app.runtime.debounce import stop_debounce_consumers
await stop_debounce_consumers()
```

完整位置基于 Step 1 的扫描结果决定。

- [ ] **Step 3: 启动 agent-service 本地 smoke test**

Run: `cd apps/agent-service && uv run uvicorn app.main:app --port 8765 &`

等几秒。然后：

Run: `curl -s http://localhost:8765/health || true; kill %1 || true`

Check 启动日志包含：
```
debounce consumer started: debounce_drift_trigger_drift_check -> drift_check
debounce consumer started: debounce_afterthought_trigger_afterthought_check -> afterthought_check
```

如果本地没有 RABBITMQ_URL，启动会跳过 mq.connect（has_debounce 但没有可用 mq）—— 这种情况下需要本地 mq 才能验证。如果本地不可行，跳过 smoke test，靠泳道部署验证（Task 19）。

- [ ] **Step 4: Commit**

```bash
git add apps/agent-service/app/main.py
git commit -m "feat(main): lifespan 启动 start_debounce_consumers + stop_debounce_consumers

跟 start_consumers (durable) 共存：debounce consumer 单独管理 _consumer_tags，
启动 / 关闭独立。

Refs: spec §3.5"
```

---

## Task 16: post_actions.py 切换调用方 + _emit_memory_trigger helper

**Files:**
- Modify: `apps/agent-service/app/chat/post_actions.py:80,88`
- Test: `apps/agent-service/tests/unit/chat/test_post_actions.py`（已有；新增 case）

- [ ] **Step 1: 写 _emit_memory_trigger helper 测试**

```python
# apps/agent-service/tests/unit/chat/test_post_actions.py 加
import pytest
from unittest.mock import AsyncMock, patch
from app.chat.post_actions import _emit_memory_trigger
from app.domain.memory_triggers import DriftTrigger


@pytest.mark.asyncio
async def test_emit_memory_trigger_swallows_exception(monkeypatch, caplog):
    """fire-and-forget 语义：emit 失败被吞 + log error，不冒泡。"""
    fake_emit = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr("app.runtime.emit.emit", fake_emit)

    # 不应该 raise
    await _emit_memory_trigger(DriftTrigger(chat_id="c1", persona_id="p1"))

    # 异常被 logger.exception 吃掉
    assert any("failed to emit memory trigger" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_emit_memory_trigger_calls_emit_on_success(monkeypatch):
    fake_emit = AsyncMock(return_value=None)
    monkeypatch.setattr("app.runtime.emit.emit", fake_emit)

    t = DriftTrigger(chat_id="c1", persona_id="p1")
    await _emit_memory_trigger(t)

    fake_emit.assert_awaited_once_with(t)
```

- [ ] **Step 2: 跑测试验证 fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/chat/test_post_actions.py -v -k emit_memory_trigger`
Expected: FAIL — `_emit_memory_trigger` 不存在

- [ ] **Step 3: 修 post_actions.py**

在 `apps/agent-service/app/chat/post_actions.py` 加 helper，并替换两个 `asyncio.create_task` 调用：

```python
# apps/agent-service/app/chat/post_actions.py 顶部加 import
from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
from app.runtime.data import Data


# 加 module-level helper（建议放在文件靠前的 helper 区）
async def _emit_memory_trigger(trigger: Data) -> None:
    """Fire-and-forget memory trigger emit. Failures are logged, not raised
    (post_actions 调用方语义就是 fire-and-forget；emit 内部任何异常都不该
    污染聊天主链路；reviewer round-1 M6)."""
    try:
        from app.runtime.emit import emit
        await emit(trigger)
    except Exception:
        logger.exception(
            "failed to emit memory trigger %s: chat_id=%s persona_id=%s",
            type(trigger).__name__,
            getattr(trigger, "chat_id", None),
            getattr(trigger, "persona_id", None),
        )


# 替换 line 80：
# 旧：asyncio.create_task(drift.on_event(chat_id, persona_id))
# 新：
asyncio.create_task(_emit_memory_trigger(
    DriftTrigger(chat_id=chat_id, persona_id=persona_id)
))

# 替换 line 88：
# 旧：asyncio.create_task(afterthought.on_event(chat_id, persona_id))
# 新：
asyncio.create_task(_emit_memory_trigger(
    AfterthoughtTrigger(chat_id=chat_id, persona_id=persona_id)
))
```

同时**删掉**：

```python
from app.memory.drift import drift          # 删
from app.memory.afterthought import afterthought  # 删
```

- [ ] **Step 4: 跑测试验证 pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/chat/test_post_actions.py -v`
Expected: 全部 PASS（含原有 case）

- [ ] **Step 5: grep 验证旧入口零残留**

Run:
```bash
cd apps/agent-service && grep -rn "drift\.on_event\|afterthought\.on_event\|from app\.memory\.drift\|from app\.memory\.afterthought" app/
```

Expected: 仅 `app/memory/drift.py` 和 `app/memory/afterthought.py` 自己（Task 17 删）；`app/chat/post_actions.py` 零结果。

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/chat/post_actions.py apps/agent-service/tests/unit/chat/test_post_actions.py
git commit -m "feat(chat/post_actions): 切到 emit(DriftTrigger) / emit(AfterthoughtTrigger)

包一层 _emit_memory_trigger helper try/except，避免
asyncio.create_task(emit(...)) 失败变成事件循环未取回 task exception
（reviewer round-1 M6）。

Refs: spec §3.6"
```

---

## Task 17: 删除旧 app/memory/ 三个文件 + 验证

**Files:**
- Delete: `apps/agent-service/app/memory/debounce.py`
- Delete: `apps/agent-service/app/memory/drift.py`
- Delete: `apps/agent-service/app/memory/afterthought.py`
- Delete: `apps/agent-service/tests/unit/memory/test_drift.py` 和 `test_afterthought.py`（如果原有的话；helper 测试已经搬到 `tests/unit/nodes/`）

- [ ] **Step 1: grep 确认零外部引用**

Run:
```bash
cd apps/agent-service && grep -rn "from app.memory.debounce\|from app.memory.drift\|from app.memory.afterthought\|DebouncedPipeline\|_Drift\|_Afterthought\b" app/ tests/
```

Expected: 仅以下三类引用：
1. `app/memory/debounce.py` / `drift.py` / `afterthought.py` 自身（要删）
2. `tests/unit/memory/test_*.py`（要删）
3. **零其他引用**

如果有其他引用，先把它们改了（应该已经在 Task 16 改完，但 grep 二次确认）。

- [ ] **Step 2: 删文件**

```bash
cd apps/agent-service
rm app/memory/debounce.py
rm app/memory/drift.py
rm app/memory/afterthought.py
# 旧测试已搬到 tests/unit/nodes/test_memory_pipelines_helpers.py，删原版
rm -f tests/unit/memory/test_drift.py
rm -f tests/unit/memory/test_afterthought.py
rm -f tests/unit/memory/test_debounce.py
```

- [ ] **Step 3: 跑全测试套件**

Run: `cd apps/agent-service && uv run pytest tests/unit -v`
Expected: 全部 PASS

- [ ] **Step 4: grep 验证目标 pattern 零残留**

Run:
```bash
cd apps/agent-service && \
grep -rn "DebouncedPipeline" app/ && \
grep -rn "drift\.on_event\|afterthought\.on_event" app/ && \
grep -rn "_phase2_running\|_buffers\b\|_timers\b" app/
```

Expected: 全部 grep 零输出（grep 找不到匹配会 exit 1，命令组合用 `; true` 避免误判；或者目视确认）。

- [ ] **Step 5: Commit**

```bash
git add -A apps/agent-service/app/memory/ apps/agent-service/tests/
git commit -m "chore: 删除 app/memory/{debounce,drift,afterthought}.py

DebouncedPipeline / _Drift / _Afterthought 全部废弃；in-memory dict 状态机
被 dataflow .debounce() runtime 替代，业务逻辑搬到 nodes/memory_pipelines.py。

Refs: spec §3.7 验收 checklist 8.1"
```

---

## Task 18: 端到端集成测试（in-memory mq + redis fake）

**Files:**
- Create: `apps/agent-service/tests/integration/test_phase3_e2e.py`

- [ ] **Step 1: 写端到端测试**

```python
# apps/agent-service/tests/integration/test_phase3_e2e.py
"""End-to-end: emit -> publish_debounce -> handler -> consumer for drift / afterthought.

Uses fake redis + fake mq stubs. Covers spec §5.5 cases:
- single emit -> single fire after debounce_seconds
- multiple emits within window -> single fire
- max_buffer hit -> immediate fire
- phase2 contention -> raise DebounceReschedule -> _do_reschedule -> next fire
- reschedule does not pollute count
- per-(chat, persona) isolation
"""
import asyncio
import json
import pytest
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock

from app.domain.memory_triggers import DriftTrigger
from app.runtime.debounce import (
    DebounceReschedule, _build_handler, publish_debounce, _do_reschedule,
)
from app.runtime.wire import WireSpec


class _FakeRedis:
    """In-memory redis stub for atomic Lua scripts used by debounce."""
    def __init__(self):
        self.kv = {}
        self.ttls = {}

    async def eval(self, script, numkeys, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        # Differentiate by script content / first line
        if "INCR" in script:  # _PUBLISH_LUA
            ttl = int(argv[1])
            max_buffer = int(argv[2])
            self.kv[keys[0]] = argv[0]
            self.ttls[keys[0]] = ttl
            n = self.kv.get(keys[1], 0)
            n = (int(n) if isinstance(n, (int, str)) else 0) + 1
            self.kv[keys[1]] = n
            self.ttls[keys[1]] = ttl
            fire_now = 0
            if n >= max_buffer:
                self.kv[keys[1]] = 0
                fire_now = 1
            return [n, fire_now]
        if "GET" in script and "SET" in script and "DEL" not in script and len(keys) == 2:
            # _CLAIM_LUA
            current = self.kv.get(keys[0])
            if current != argv[0]:
                return 0
            self.kv[keys[1]] = 0
            self.ttls[keys[1]] = int(argv[1])
            return 1
        if "DEL" in script and "redis.call('DEL'" in script:
            # _CONDITIONAL_DEL_LUA
            current = self.kv.get(keys[0])
            if current == argv[0]:
                self.kv.pop(keys[0], None)
                self.kv.pop(keys[1], None)
                return 1
            return 0
        if "GET" in script and "SET" in script and len(keys) == 1:
            # _RESCHEDULE_CAS_LUA
            current = self.kv.get(keys[0])
            if current != argv[0]:
                return 0
            self.kv[keys[0]] = argv[1]
            self.ttls[keys[0]] = int(argv[2])
            return 1
        raise NotImplementedError(f"fake redis eval: {script[:50]!r}")

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)


class _FakeMq:
    """In-memory mq stub: stores published bodies in a list per route."""
    def __init__(self):
        self.published = []  # list of (route, body, headers, delay_ms)

    async def publish(self, route, body, headers=None, delay_ms=None):
        self.published.append((route, body, headers, delay_ms))


class _FakeMessage:
    def __init__(self, body, headers):
        self.body = json.dumps(body).encode("utf-8")
        self.headers = headers
        self.processed_with_requeue = None

    def process(self, *, requeue):
        outer = self
        class _Ctx:
            async def __aenter__(self_inner):
                outer.processed_with_requeue = requeue
            async def __aexit__(self_inner, exc_type, exc_val, exc_tb):
                return False
        return _Ctx()


@pytest.mark.asyncio
async def test_e2e_single_emit_one_fire(monkeypatch):
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=redis_fake))
    monkeypatch.setattr("app.runtime.debounce.mq", mq_fake)
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    fired = []
    async def consumer(t: DriftTrigger):
        fired.append(t)

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 5},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )

    # publish 一次
    await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))
    assert len(mq_fake.published) == 1
    assert mq_fake.published[0][3] == 60_000  # delay_ms

    # handler 处理这条 message → fire 一次 + conditional DEL
    handler = _build_handler(w, consumer)
    route, body, headers, _ = mq_fake.published[0]
    msg = _FakeMessage(body, headers or {})
    await handler(msg)

    assert len(fired) == 1
    assert fired[0].chat_id == "c1"


@pytest.mark.asyncio
async def test_e2e_multiple_emits_one_fire(monkeypatch):
    """Within debounce window, only the latest publish's fire wins."""
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=redis_fake))
    monkeypatch.setattr("app.runtime.debounce.mq", mq_fake)
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    fired = []
    async def consumer(t: DriftTrigger):
        fired.append(t)

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 100},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )

    for i in range(3):
        await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))

    handler = _build_handler(w, consumer)
    # 处理前 2 条 message：被新 publish 作废，atomic claim fail → drop
    for i, (route, body, headers, _) in enumerate(mq_fake.published[:2]):
        msg = _FakeMessage(body, headers or {})
        await handler(msg)
    assert len(fired) == 0

    # 处理最后一条：latest match → fire
    route, body, headers, _ = mq_fake.published[2]
    msg = _FakeMessage(body, headers or {})
    await handler(msg)
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_e2e_max_buffer_immediate_fire(monkeypatch):
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=redis_fake))
    monkeypatch.setattr("app.runtime.debounce.mq", mq_fake)
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    async def consumer(t: DriftTrigger):
        pass

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )

    for _ in range(3):
        await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))

    # 第三条应该是 fire_now=True + delay=0
    last = mq_fake.published[-1]
    assert last[1]["fire_now"] is True
    assert last[3] == 0  # delay_ms = 0

    # 之后再 publish 一条：count 从 0 重新攒，不再 fire_now
    await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))
    assert mq_fake.published[-1][1]["fire_now"] is False
    assert mq_fake.published[-1][3] == 60_000


@pytest.mark.asyncio
async def test_e2e_phase2_contention_via_debounce_reschedule(monkeypatch):
    """phase2 跑期间收到第二个 fire → consumer raise DebounceReschedule →
    handler 跑 _do_reschedule → 之后第三个 fire 拿到锁正常处理。"""
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=redis_fake))
    monkeypatch.setattr("app.runtime.debounce.mq", mq_fake)
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    call_log = []

    async def consumer(t: DriftTrigger):
        call_log.append(t.chat_id)
        if len(call_log) == 1:
            raise DebounceReschedule(DriftTrigger(chat_id=t.chat_id, persona_id=t.persona_id))
        # 第二次正常 return

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 100},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )

    handler = _build_handler(w, consumer)

    # 第一次 publish
    await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))
    msg1 = _FakeMessage(mq_fake.published[0][1], mq_fake.published[0][2] or {})
    await handler(msg1)

    # consumer 被 call 一次（raise DebounceReschedule）
    assert len(call_log) == 1
    # _do_reschedule publish 一条新 delay 消息
    assert len(mq_fake.published) == 2

    # 处理 reschedule 出来的 message
    msg2 = _FakeMessage(mq_fake.published[1][1], mq_fake.published[1][2] or {})
    await handler(msg2)

    # consumer 第二次正常完成
    assert len(call_log) == 2


@pytest.mark.asyncio
async def test_e2e_per_key_isolation(monkeypatch):
    """两个 (chat_id, persona_id) 各自的 debounce 状态互不干扰。"""
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=redis_fake))
    monkeypatch.setattr("app.runtime.debounce.mq", mq_fake)
    monkeypatch.setattr("app.runtime.debounce.trace_id_var",
                        MagicMock(get=lambda: ""))
    monkeypatch.setattr("app.runtime.debounce.lane_var",
                        MagicMock(get=lambda: ""))

    fired = []
    async def consumer(t: DriftTrigger):
        fired.append((t.chat_id, t.persona_id))

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 100},
        debounce_key_by=lambda e: f"k:{e.chat_id}:{e.persona_id}",
    )

    await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))
    await publish_debounce(w, consumer, DriftTrigger(chat_id="c2", persona_id="p2"))

    handler = _build_handler(w, consumer)
    for route, body, headers, _ in mq_fake.published:
        await handler(_FakeMessage(body, headers or {}))

    assert sorted(fired) == [("c1", "p1"), ("c2", "p2")]
```

- [ ] **Step 2: 跑端到端测试**

Run: `cd apps/agent-service && uv run pytest tests/integration/test_phase3_e2e.py -v`
Expected: 5 PASS

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/tests/integration/test_phase3_e2e.py
git commit -m "test(integration): Phase 3 端到端 — emit -> handler -> consumer 全链路

5 个 case 覆盖：单 emit / 多 emit 收敛 / max_buffer immediate fire /
phase2 抢占 reschedule / per-key 隔离。用 in-memory redis + mq fake，
跑 publish_debounce + _build_handler + _do_reschedule 真实代码路径。

Refs: spec §5.5"
```

---

## Task 19: 泳道部署 + 5 类场景验证

**This task is human-driven** —— 不是写代码，是按清单跑命令、看日志、看 redis、看 mq。

Reference: spec §5.6 + §6 切换步骤。

- [ ] **Step 1: 部署到泳道 phase3-debounce**

```bash
make deploy APP=agent-service LANE=phase3-debounce GIT_REF=$(git rev-parse HEAD)
```

确认 3 个 Deployment 全部 Running 0 restarts：

```bash
/ops pods agent-service phase3-debounce
/ops pods arq-worker phase3-debounce
/ops pods vectorize-worker phase3-debounce
```

- [ ] **Step 2: bind dev bot 到泳道**

```bash
/ops bind TYPE=bot KEY=dev LANE=phase3-debounce
```

- [ ] **Step 3: 验证 consumer 启动日志**

```bash
make logs APP=agent-service KEYWORD="debounce consumer started"
```

Expected：出现两条
```
debounce consumer started: debounce_drift_trigger_drift_check_phase3-debounce -> drift_check
debounce consumer started: debounce_afterthought_trigger_afterthought_check_phase3-debounce -> afterthought_check
```

- [ ] **Step 4: 跑 5 类场景**

按 spec §5.6 五类：

  1. **silence debounce**：飞书 dev bot 私聊或群聊发 1 句话 → 等 N 秒（drift `settings.identity_drift_debounce_seconds` / afterthought 300s）
     - Verify: `make logs APP=agent-service KEYWORD="debounce fire"` 出现 1 条 + drift_check / afterthought_check 调用日志 1 次

  2. **drift max_buffer**：连续发 N 条赤尾消息（>= identity_drift_max_buffer，比如 10 条快速）
     - Verify: drift_check 立即触发（`fire_now` 路径），日志 `fire_now=True`

  3. **afterthought max_buffer**：群里发 ≥ 15 条消息
     - Verify: afterthought_check 立即触发

  4. **phase2 抢占**：drift_check 跑期间（mock LLM 慢一点的方法是趁泳道环境 LLM 慢的窗口）再发消息
     - Verify: 看到 "phase2 in flight … raise DebounceReschedule" 日志 + 之后第二轮 fire 拿到锁正常跑

  5. **重启不丢**：触发 drift（步骤 1）后立即 `/ops` 重启泳道 Pod → 确认 mq 上 delay 消息存活，重启完 consumer 接管 → drift_check 仍按时被触发
     - Verify: redis 上 `debounce:latest:DriftTrigger:*` key 在重启前后保持，重启后 mq 投递 → consumer 触发

  6. **token 化锁释放**（spec §3.2 / round-2 H2）：模拟 LLM 卡死场景，等 lock TTL 过期，新 fire 拿到锁开始处理后旧 finally 跑 → 检查 redis 上新锁没被误删

- [ ] **Step 5: redis 状态检查**

每次 fire 完成后，确认 redis 上 `debounce:latest:DriftTrigger:*` 和 `debounce:count:DriftTrigger:*` key 应该被 conditional DEL 清掉。如果 reschedule 路径 / 异常路径，状态应该按 spec §4.1 表格保留或 24h TTL 自然清。

通过 `/ops-db @chiwei` 查不了 redis，需要 prod redis 客户端或 dashboard。如果没有便捷工具：跳过这步，靠 mq + 日志间接验证。

- [ ] **Step 6: mq 队列状态**

```bash
/ops 或 RabbitMQ Management UI 查看：
- debounce_drift_trigger_drift_check_phase3-debounce 队列消息进 / 出比例
- debounce_afterthought_trigger_afterthought_check_phase3-debounce 同理
- dead_letters 队列：确认无 debounce 消息进入
```

- [ ] **Step 7: 全部通过后清理**

```bash
/ops unbind TYPE=bot KEY=dev
make undeploy APP=agent-service LANE=phase3-debounce
```

- [ ] **Step 8: 5 类场景检查清单填表 + 提交记录到 PR description（or 验收文档）**

记录每一类场景的验证结果（日志片段、计数器、redis 状态）到 PR description 或泳道验证文档。

---

## Task 20: 告警 regex 扩展（运维 followup）

**This task is human-driven + ops-only** —— 不在 PR diff 里。

Reference: spec §4.6 + §8.4

- [ ] **Step 1: 扩展 RabbitmqConsumerDown regex**

通过 `/ops` 或运维流程把 `RabbitmqConsumerDown` 告警的 queue 正则扩展包含 `debounce_*`（同 PR #202 模式 —— 当前覆盖 `durable_*` + `memory_fragment_vectorize` + `memory_abstract_vectorize`，缺 `debounce_*`）。

不直接 `kubectl apply`（违反 CLAUDE.md 基础设施约束）。

- [ ] **Step 2: 验证告警生效**

部署完 prod 后等 1h 让 Prometheus / 告警系统拉取最新规则。然后人为 stop debounce consumer（或通过观察自然 idle 期）验证告警是否触发。

或者：观察 alerts 系统看 `debounce_*` queue 出现在 active rule list 里。

---

## Self-Review Checklist

写完计划后过一遍 spec 看覆盖：

| Spec 章节 | 对应 Task |
|---|---|
| §3.1 Data 类（DriftTrigger / AfterthoughtTrigger） | Task 5 |
| §3.2 节点（drift_check / afterthought_check + 锁 token） | Task 12 + 13 |
| §3.3 Wiring | Task 14 |
| §3.4.1 wire.py DSL（key_by） | Task 3 |
| §3.4.2 graph.py 校验（10 项 reject） | Task 4 |
| §3.4.3 runtime/debounce.py（publish + handler + reschedule + DebounceReschedule） | Task 6 / 7 / 8 / 9 |
| §3.4.4 emit.py 集成 + Route.lane_fallback | Task 1 + 2 + 11 |
| §3.4.5 关键决策记录 | 体现在所有 task 注释 + commit message |
| §3.5 main.py lifespan | Task 15 |
| §3.6 post_actions 接入 + _emit_memory_trigger | Task 16 |
| §3.7 旧 memory/ 文件清理 | Task 17 |
| §3.8 Settings / 常量 | Task 12 / 14（settings.identity_drift_* 已用，afterthought 字面量 300/15 已写在 wiring） |
| §4.1 失败模式 / 重启不丢 / DLQ replay 边界 | 体现在 Task 9 测试 + spec 引用 |
| §4.5 Lane TTL fallback | Task 1 + 2 |
| §5 测试 | Task 1-18（每个 task TDD） |
| §6 部署 | Task 19 |
| §8 验收 checklist | 散在 Task 17 grep 验证 + Task 19 泳道场景 + Task 20 告警 |

---

## Execution Notes

**频繁 commit 原则**：每个 Task 内最后一步是 commit。一次提交包含一个完整可测试的小功能（test + impl + 跑 pass）。

**TDD 严格性**：先写测试 → 跑 fail → 实现 → 跑 pass → commit。不允许跳过 fail 验证那一步。

**Self-flight 风险**：Task 9 + 13 + 18 涉及 atomic claim / DebounceReschedule / reschedule 链路；这是 Phase 3 最容易写错的地方。每个测试 case 都要严格按 spec §3.4.3 + §3.2 行为编排。

**回滚**：单 PR 改动较多，但都是替换同一职责 + 删旧文件。回滚 = revert PR；schema 没动（transient），无 schema 影响。

**部署铁律**：单镜像 agent-service 三 Deployment（agent-service / arq-worker / vectorize-worker）必须同步发布。Task 19 部署命令已包含。

**禁止操作**：
- 不直接 `kubectl rollout restart` / `kubectl apply`（CLAUDE.md 基础设施约束）
- 不未经许可 deploy 到 prod（先泳道 + 5 类全过 + 用户验收 + ship）
- 不在 PR 中带 alert-rules.yaml 改动（运维通过 `/ops` 单独下发）
