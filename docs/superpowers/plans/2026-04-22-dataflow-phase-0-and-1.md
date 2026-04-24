# Agent Service Dataflow Phase 0 + Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 agent-service dataflow 抽象的 runtime 骨架（Phase 0）并把 vectorize 管线迁到新框架（Phase 1）。旧 chat pipeline 保持原状，只在写 `conversation_messages` 之后加一行 `await emit_legacy_message(cm)`（Message Bridge 入口）；Phase 5 chat 迁完后这一行和 Bridge 一起删除。

**Architecture:**
- `app/runtime/`：框架代码（Data base、Marker、@node、wire DSL、graph、migrator、query、engine、placement、emit）—— **不含任何业务 import**，保持 business-agnostic
- `app/capabilities/`：六个 capability 薄 adapter（LLMClient / AgentRunner / EmbedderClient / VectorStore / HTTPClient / query）
- `app/domain/`：业务 Data 类（`Message`、`Fragment` 等，逐步接管旧 `app/data/models.py` 的表）
- `app/nodes/`：`@node async def ...` 业务函数
- `app/bridges/`：业务胶水层 —— 把老 ORM 对象升格为 Domain Data 并 `emit()`。Phase 5 后整体删除。
- `app/wiring/`：按域拆分的 `wire(...)` 声明文件
- `app/deployment.py`：`bind(Node).to_app("...")` 归属声明
- `apps/paas-engine/internal/adapter/kubernetes/deployer.go`：deployer 改动，注入 `APP_NAME` 环境变量

Phase 0 交付：runtime 能跑一个 toy Node；所有 capability adapter 通过；Schema Migrator 能接管旧表。Phase 1 交付：vectorize 跑在新框架的 `vectorize-worker` App 里，Message Bridge 接入老写入点，旧 cron/queue/publish 全部删除，一次性回扫脚本处理存量。

**Tech Stack:** Python 3.12 / Pydantic v2 / SQLAlchemy 2 async / aio-pika (RabbitMQ) / asyncpg / redis / qdrant-client / volcengine ark / langchain + langgraph / pytest-asyncio ; PaaS Engine (Go) / K8s / Harbor

---

## File Structure

**新建（apps/agent-service/app/）：**

```
runtime/
    __init__.py         # 公开 API：Data, Key, DedupKey, Version, AdminOnly, node, wire, Stream, emit, runtime, bind, query, Source, Sink
    data.py             # Data base + Marker + 字段反射工具
    stream.py           # Stream[T]
    node.py             # @node 装饰器 + NODE_REGISTRY
    wire.py             # wire(T) DSL + WIRING_REGISTRY
    source.py           # Source.http / Source.cron / Source.mq / Source.feishu_webhook / Source.manual
    sink.py             # Sink.feishu_send / Sink.http_callback / Sink.langfuse_trace
    graph.py            # compile_graph() + GLOBAL_GRAPH + 启动校验
    migrator.py         # Schema Migrator（Pydantic → pg DDL）
    query.py            # query(T) 泛型查询
    engine.py           # runtime.run()
    placement.py        # bind() + nodes_for_app()
    emit.py             # runtime.emit(data) — Bridge 和原生 producer 共用入口

capabilities/
    __init__.py
    llm.py              # LLMClient
    agent.py            # AgentRunner
    embed.py            # EmbedderClient
    vector_store.py     # VectorStore
    http.py             # HTTPClient

domain/
    __init__.py         # 导出所有 Data 类
    message.py          # Message
    fragment.py         # Fragment

bridges/
    __init__.py
    message_bridge.py   # emit_legacy_message(cm: ConversationMessage) -> emit(Message(...))
                        # Phase 5 整体删除

nodes/
    __init__.py
    vectorize.py        # @node vectorize(msg: Message) -> Fragment
    save_fragment.py    # @node save_fragment(frag: Fragment) -> None

wiring/
    __init__.py         # import 所有子 module，compile_graph
    memory.py           # vectorize wire + bind

deployment.py           # bind(vectorize).to_app("vectorize-worker") ...

workers/runtime_entry.py  # 统一 worker 入口：python -m app.workers.runtime_entry
```

**修改：**
- `apps/paas-engine/internal/adapter/kubernetes/deployer.go` — 在 mergedEnvs 注入 `APP_NAME`
- 所有写 `conversation_messages` 的点（`app/life/proactive.py`、chat pipeline、read、afterthought 等，T1.7 Step 1 的 grep 清单为准） — session.add+commit 之后加 `await emit_legacy_message(cm)`（Phase 1 步骤）
- `apps/agent-service/app/main.py` — lifespan 里调 `register_http_sources(app)`（T0.16 已完成）
- `apps/agent-service/app/workers/runtime_entry.py` — 追加 `import app.wiring; import app.deployment`（T1.5 激活步骤）

**删除（Phase 1 结束时）：**
- `apps/agent-service/app/workers/vectorize.py` 全部（旧 consumer + cron）
- `apps/agent-service/app/workers/arq_settings.py` 中 `cron_scan_pending_messages` 对应的 cron 条目
- `app/infra/rabbitmq.py` 中 `VECTORIZE` / `MEMORY_VECTORIZE` route 常量

**一次性脚本（Phase 1 最后执行，不入 repo 常驻）：**
- `/tmp/backfill_vectorize.py` — 存量 `vector_status='pending'` 行的回扫

---

## Phase 划分原则（重要）

按**依赖方向**（import 指向）划分 Phase，不按概念相似。6 层从底到上：

| 层 | 内容 | 允许 import 的上游 |
|---|---|---|
| **F** framework | `app/runtime/*`, `app/capabilities/*` | stdlib / 三方库 |
| **D** domain | `app/domain/*`（业务 Data 类） | F |
| **L** logic | `app/nodes/*`（@node 函数） | F + D |
| **B** bridge | `app/bridges/*`（老对象 → Domain Data → emit） | F + D |
| **W** wiring | `app/wiring/*`, `app/deployment.py`, `workers/runtime_entry.py` 里的 wiring import | F + D + L |
| **I** integration | 老代码 call-site / PaaS config / 泳道部署 | 任意 |

**Phase 0 只允许 F 层**。任何 Phase 0 task 的 task-level import 如果出现 `app.domain.*` / `app.nodes.*` / `app.bridges.*` / `app.wiring.*`，就是错归类，必须挪到 Phase 1。

快速验证命令：

```bash
# Phase 0 task 的 Step 代码块里不应出现这些 import
grep -nE "from app\.(domain|nodes|bridges|wiring)" <phase0-task-code-blocks>
```

---

## Phase 0 — Runtime 骨架

> **状态（2026-04-23）**：本 Phase 全部 task（T0.1–T0.16 + T0.7.5）已完成。下方伪代码是**历史设计快照**，与真实实现有若干签名偏差（例如 T0.11 最终是 consumer-side dedup、没有 `mq.publish_raw`；T0.13 capabilities 实际签名见 `/tmp/dataflow-exec-state.md`）。
>
> **如果要看"实际做了什么"**：以 git log 和源码为准，辅助参考 `/tmp/dataflow-exec-state.md` 的"Recent plan deviations"章节。Plan 文字**不修**，因为 Phase 0 task 都 completed，修改只会模糊历史。
>
> **如果你是 Phase 1 的执行者**：直接跳到 `## Phase 1 — Vectorize 管线迁移`。Phase 0 代码块不是 Phase 1 的接口契约。

### Task 0.1: PaaS Engine 注入 APP_NAME

**Files:**
- Modify: `apps/paas-engine/internal/adapter/kubernetes/deployer.go`
- Test: `apps/paas-engine/internal/adapter/kubernetes/deployer_test.go`

- [ ] **Step 1: 找到现有 env 注入点**

读 `deployer.go:205-210`（已调研，大致是）：

```go
mergedEnvs := map[string]string{}
for k, v := range app.Envs { mergedEnvs[k] = v }
for k, v := range release.Envs { mergedEnvs[k] = v }
mergedEnvs["VERSION"] = release.Version
mergedEnvs["LANE"] = release.Lane
```

- [ ] **Step 2: 在同目录的 deployer_test.go 写失败测试**

```go
func TestDeploy_InjectsAppName(t *testing.T) {
    app := &domain.App{Name: "vectorize-worker", ...}
    release := &domain.Release{AppName: "vectorize-worker", Version: "1.0.0.1", Lane: "prod", ...}
    dep := buildDeployment(app, release)
    envs := findContainerEnv(dep, "app")
    require.Equal(t, "vectorize-worker", envs["APP_NAME"])
}
```

运行：`cd apps/paas-engine && go test ./internal/adapter/kubernetes/ -run TestDeploy_InjectsAppName -v`  
预期：FAIL（APP_NAME 缺失）。

- [ ] **Step 3: 修改 deployer.go 加一行**

```go
mergedEnvs["APP_NAME"] = release.AppName
```

- [ ] **Step 4: 测试通过**

预期：PASS。同时跑 `make test` 确保没回归。

- [ ] **Step 5: Commit**

```bash
git add apps/paas-engine/internal/adapter/kubernetes/
git commit -m "feat(paas): inject APP_NAME env var into deployments"
```

---

### Task 0.2: Data base + Marker

**Files:**
- Create: `apps/agent-service/app/runtime/data.py`
- Create: `apps/agent-service/tests/runtime/test_data.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_data.py
from typing import Annotated
from app.runtime.data import Data, Key, DedupKey, Version, AdminOnly, key_fields, dedup_fields, version_field, is_admin_only

class Sample(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    gen: Annotated[int, DedupKey] = 0
    text: str

def test_key_fields():
    assert key_fields(Sample) == ("pid",)

def test_dedup_fields_defaults_to_key_plus_extra():
    assert dedup_fields(Sample) == ("pid", "gen")

def test_version_field_detected():
    assert version_field(Sample) == "ver"

def test_is_admin_only_false_by_default():
    assert is_admin_only(Sample) is False

class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict

def test_admin_only_detected():
    assert is_admin_only(Cfg) is True

def test_registry_populated():
    from app.runtime.data import DATA_REGISTRY
    assert Sample in DATA_REGISTRY
    assert Cfg in DATA_REGISTRY

def test_data_without_key_rejected():
    import pytest
    with pytest.raises(TypeError, match="must declare at least one Key"):
        class Bad(Data):
            text: str  # no Key
```

- [ ] **Step 2: 运行测试验证失败**

`uv run pytest apps/agent-service/tests/runtime/test_data.py -v` → ImportError。

- [ ] **Step 3: 实现 data.py**

```python
# app/runtime/data.py
from typing import Annotated, Any, get_type_hints, get_origin, get_args
from pydantic import BaseModel, ConfigDict

class Key: """Marker: field is part of the natural key."""
class DedupKey: """Marker: field joins dedup hash (Key ∪ DedupKey)."""
class Version: """Marker: append-only version column (runtime-maintained)."""
class AdminOnly: """Class-level mixin: business code may not produce this Data."""

DATA_REGISTRY: set[type["Data"]] = set()

class Data(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        # Pydantic v2 fires this AFTER model_fields is built.
        # NOTE: DO NOT use __init_subclass__ — that fires before model_fields
        # is populated, so reflection (key_fields / DATA_REGISTRY add) will
        # silently be a no-op on every subclass.
        if not cls.model_fields:
            return  # skip Data itself and pure mixin intermediates
        if not key_fields(cls):
            raise TypeError(f"{cls.__name__} must declare at least one Key field")
        DATA_REGISTRY.add(cls)

def _metadata(cls: type[Data], name: str) -> tuple:
    return tuple(cls.model_fields[name].metadata)

def key_fields(cls: type[Data]) -> tuple[str, ...]:
    return tuple(n for n, f in cls.model_fields.items() if Key in f.metadata)

def dedup_fields(cls: type[Data]) -> tuple[str, ...]:
    keys = key_fields(cls)
    extras = tuple(n for n, f in cls.model_fields.items()
                   if DedupKey in f.metadata and n not in keys)
    return keys + extras if extras else keys  # default: dedup == key

def version_field(cls: type[Data]) -> str | None:
    for name, f in cls.model_fields.items():
        if Version in f.metadata:
            return name
    return None

def is_admin_only(cls: type[Data]) -> bool:
    return issubclass(cls, AdminOnly)
```

- [ ] **Step 4: 测试通过**

`uv run pytest apps/agent-service/tests/runtime/test_data.py -v` → 全绿。

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/runtime/data.py apps/agent-service/tests/runtime/test_data.py
git commit -m "feat(runtime): Data base + Key/DedupKey/Version/AdminOnly markers"
```

---

### Task 0.3: Stream[T]

**Files:**
- Create: `apps/agent-service/app/runtime/stream.py`
- Test: `apps/agent-service/tests/runtime/test_stream.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_stream.py
from typing import Annotated
from app.runtime.data import Data, Key
from app.runtime.stream import Stream, is_stream, element_type

class Chunk(Data):
    sid: Annotated[str, Key]
    seq: Annotated[int, Key]
    text: str
    is_final: bool = False

def test_stream_is_generic_alias():
    anno = Stream[Chunk]
    assert is_stream(anno)
    assert element_type(anno) is Chunk

def test_non_stream_detected():
    assert is_stream(Chunk) is False
    assert is_stream(int) is False

def test_final_marker_default_false():
    c = Chunk(sid="s1", seq=0, text="hi")
    assert c.is_final is False
```

- [ ] **Step 2: 运行测试验证失败**

- [ ] **Step 3: 实现 stream.py**

```python
# app/runtime/stream.py
from typing import Generic, TypeVar, get_origin, get_args

T = TypeVar("T")

class Stream(Generic[T]):
    """Type-only marker for 'stream of T'. Never instantiated directly;
    runtime sees a @node returning Stream[X] and wires it as async iterable of X."""

def is_stream(annotation) -> bool:
    return get_origin(annotation) is Stream

def element_type(annotation):
    args = get_args(annotation)
    return args[0] if args else None
```

- [ ] **Step 4: 测试通过**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(runtime): Stream[T] typing marker for streaming Data"
```

---

### Task 0.4: @node 装饰器 + 反射

**Files:**
- Create: `apps/agent-service/app/runtime/node.py`
- Test: `apps/agent-service/tests/runtime/test_node.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_node.py
from typing import Annotated
import pytest
from app.runtime.data import Data, Key, AdminOnly
from app.runtime.node import node, NODE_REGISTRY, inputs_of, output_of

class Msg(Data):
    mid: Annotated[str, Key]
    text: str

class Frag(Data):
    fid: Annotated[str, Key]
    vec: list[float]

class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict

@node
async def vectorize(msg: Msg) -> Frag:
    return Frag(fid="f1", vec=[0.0])

def test_registered():
    assert vectorize in NODE_REGISTRY

def test_inputs_reflection():
    assert inputs_of(vectorize) == {"msg": Msg}

def test_output_reflection():
    assert output_of(vectorize) is Frag

def test_admin_only_output_rejected():
    with pytest.raises(TypeError, match="AdminOnly"):
        @node
        async def bad() -> Cfg:
            return Cfg(cid="c1", v={})

def test_non_data_input_rejected():
    with pytest.raises(TypeError, match="must be a Data subclass or Stream"):
        @node
        async def bad2(x: int) -> Frag: ...
```

- [ ] **Step 2: 运行测试验证失败**

- [ ] **Step 3: 实现 node.py**

```python
# app/runtime/node.py
from typing import Callable, get_type_hints
from app.runtime.data import Data, is_admin_only
from app.runtime.stream import is_stream, element_type

NODE_REGISTRY: set[Callable] = set()
_NODE_META: dict[Callable, dict] = {}

def node(fn: Callable) -> Callable:
    hints = get_type_hints(fn)
    ret = hints.pop("return", None)
    inputs = {}
    for name, t in hints.items():
        if is_stream(t):
            et = element_type(t)
            if not (isinstance(et, type) and issubclass(et, Data)):
                raise TypeError(f"{fn.__name__}.{name}: Stream[X] requires X be a Data subclass")
        elif not (isinstance(t, type) and issubclass(t, Data)):
            raise TypeError(f"{fn.__name__}.{name} must be a Data subclass or Stream[Data]")
        inputs[name] = t
    # NOTE: `get_type_hints` returns `type(None)` (the NoneType class) for
    # `-> None`, NOT Python's None literal — so we check both.
    if ret is not None and ret is not type(None):
        tgt = element_type(ret) if is_stream(ret) else ret
        if not (isinstance(tgt, type) and issubclass(tgt, Data)):
            raise TypeError(f"{fn.__name__} return must be Data | Stream[Data] | None")
        if is_admin_only(tgt):
            raise TypeError(f"{fn.__name__} returns AdminOnly Data {tgt.__name__}: forbidden")
    _NODE_META[fn] = {"inputs": inputs, "output": ret}
    NODE_REGISTRY.add(fn)
    return fn

def inputs_of(fn: Callable) -> dict[str, type]:
    return _NODE_META[fn]["inputs"]

def output_of(fn: Callable):
    return _NODE_META[fn]["output"]
```

- [ ] **Step 4: 测试通过**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(runtime): @node decorator with signature reflection + AdminOnly guard"
```

---

### Task 0.5: wire() DSL + WIRING_REGISTRY

**Files:**
- Create: `apps/agent-service/app/runtime/wire.py`
- Create: `apps/agent-service/app/runtime/source.py`
- Create: `apps/agent-service/app/runtime/sink.py`
- Test: `apps/agent-service/tests/runtime/test_wire.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_wire.py
from typing import Annotated
from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.wire import wire, WIRING_REGISTRY, clear_wiring
from app.runtime.source import Source

class Msg(Data):
    mid: Annotated[str, Key]

class State(Data):
    pid: Annotated[str, Key]
    v: int

@node
async def f(msg: Msg) -> None: ...

@node
async def g(msg: Msg, state: State) -> None: ...

def setup_function():
    clear_wiring()

def test_wire_to_registers():
    wire(Msg).to(f)
    assert len(WIRING_REGISTRY) == 1
    w = WIRING_REGISTRY[0]
    assert w.data_type is Msg
    assert w.consumers == [f]

def test_wire_durable():
    wire(Msg).to(f).durable()
    assert WIRING_REGISTRY[0].durable is True

def test_wire_as_latest():
    wire(State).to(f).as_latest()
    assert WIRING_REGISTRY[0].as_latest is True

def test_wire_from_source():
    wire(Msg).from_(Source.cron("*/5 * * * *"))
    assert WIRING_REGISTRY[0].sources[0].kind == "cron"

def test_wire_with_latest_pulls_extra_data():
    wire(Msg).to(g).with_latest(State)
    assert WIRING_REGISTRY[0].with_latest == (State,)

def test_wire_when_predicate():
    wire(Msg).to(f).when(lambda m: m.mid == "x")
    w = WIRING_REGISTRY[0]
    assert w.predicate is not None
    assert w.predicate(Msg(mid="x")) is True
    assert w.predicate(Msg(mid="y")) is False

def test_wire_debounce():
    wire(Msg).to(f).debounce(seconds=10, max_buffer=5)
    w = WIRING_REGISTRY[0]
    assert w.debounce == {"seconds": 10, "max_buffer": 5}

def test_wire_broadcast():
    wire(Msg).to(f).broadcast()
    assert WIRING_REGISTRY[0].broadcast is True
```

- [ ] **Step 2: 运行测试验证失败**

- [ ] **Step 3: 实现 source.py（最小骨架）**

```python
# app/runtime/source.py
from dataclasses import dataclass, field

@dataclass(frozen=True)
class SourceSpec:
    kind: str
    params: dict = field(default_factory=dict)

class Source:
    @staticmethod
    def http(path: str) -> SourceSpec: return SourceSpec("http", {"path": path})
    @staticmethod
    def cron(expr: str) -> SourceSpec: return SourceSpec("cron", {"expr": expr})
    @staticmethod
    def mq(queue: str) -> SourceSpec: return SourceSpec("mq", {"queue": queue})
    @staticmethod
    def feishu_webhook() -> SourceSpec: return SourceSpec("feishu_webhook")
    @staticmethod
    def manual(path: str) -> SourceSpec: return SourceSpec("manual", {"path": path})
```

- [ ] **Step 4: 实现 sink.py（最小骨架）**

```python
# app/runtime/sink.py
from dataclasses import dataclass, field

@dataclass(frozen=True)
class SinkSpec:
    kind: str
    params: dict = field(default_factory=dict)

class Sink:
    @staticmethod
    def feishu_send() -> SinkSpec: return SinkSpec("feishu_send")
    @staticmethod
    def http_callback(url: str) -> SinkSpec: return SinkSpec("http_callback", {"url": url})
    @staticmethod
    def langfuse_trace() -> SinkSpec: return SinkSpec("langfuse_trace")
```

- [ ] **Step 5: 实现 wire.py**

```python
# app/runtime/wire.py
from dataclasses import dataclass, field
from typing import Callable
from app.runtime.data import Data
from app.runtime.source import SourceSpec
from app.runtime.sink import SinkSpec

@dataclass
class WireSpec:
    data_type: type[Data]
    consumers: list[Callable] = field(default_factory=list)
    sinks: list[SinkSpec] = field(default_factory=list)
    sources: list[SourceSpec] = field(default_factory=list)
    durable: bool = False
    as_latest: bool = False
    broadcast: bool = False
    predicate: Callable | None = None
    debounce: dict | None = None
    with_latest: tuple[type[Data], ...] = ()

WIRING_REGISTRY: list[WireSpec] = []

def clear_wiring() -> None:
    WIRING_REGISTRY.clear()

class WireBuilder:
    def __init__(self, data_type: type[Data]):
        self._spec = WireSpec(data_type=data_type)
        WIRING_REGISTRY.append(self._spec)

    def to(self, *targets) -> "WireBuilder":
        for t in targets:
            if isinstance(t, SinkSpec):
                self._spec.sinks.append(t)
            else:
                self._spec.consumers.append(t)
        return self

    def from_(self, *sources: SourceSpec) -> "WireBuilder":
        self._spec.sources.extend(sources)
        return self

    def durable(self) -> "WireBuilder": self._spec.durable = True; return self
    def as_latest(self) -> "WireBuilder": self._spec.as_latest = True; return self
    def broadcast(self) -> "WireBuilder": self._spec.broadcast = True; return self
    def when(self, pred: Callable) -> "WireBuilder": self._spec.predicate = pred; return self
    def debounce(self, *, seconds: int, max_buffer: int) -> "WireBuilder":
        self._spec.debounce = {"seconds": seconds, "max_buffer": max_buffer}
        return self
    def with_latest(self, *types: type[Data]) -> "WireBuilder":
        self._spec.with_latest = types
        return self

def wire(data_type: type[Data]) -> WireBuilder:
    return WireBuilder(data_type)
```

- [ ] **Step 6: 测试通过**

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(runtime): wire() DSL + Source/Sink specs"
```

---

### Task 0.6: compile_graph() + 启动校验

**Files:**
- Create: `apps/agent-service/app/runtime/graph.py`
- Test: `apps/agent-service/tests/runtime/test_graph.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_graph.py
import pytest
from typing import Annotated
from app.runtime.data import Data, Key, AdminOnly
from app.runtime.node import node
from app.runtime.wire import wire, clear_wiring
from app.runtime.graph import compile_graph, GraphError

class M(Data):
    mid: Annotated[str, Key]

class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict

def setup_function():
    clear_wiring()

def test_compile_success():
    @node
    async def f(m: M) -> None: ...
    wire(M).to(f)
    g = compile_graph()
    assert M in g.data_types
    assert f in g.nodes

def test_consumer_signature_mismatch_rejected():
    @node
    async def takes_m(m: M) -> None: ...
    # Wire declares M -> takes_m, but consumer needs M as input. Should pass.
    wire(M).to(takes_m)
    compile_graph()  # no error

def test_admin_only_consumer_ok():
    # AdminOnly can be consumed (read-only), just not produced.
    @node
    async def reads_cfg(c: Cfg) -> None: ...
    wire(Cfg).to(reads_cfg)
    compile_graph()  # ok

def test_wire_to_unknown_node_rejected():
    async def not_a_node(m: M) -> None: ...
    wire(M).to(not_a_node)
    with pytest.raises(GraphError, match="not registered"):
        compile_graph()

def test_with_latest_requires_as_latest_declared():
    @node
    async def f(m: M, s: "S") -> None: ...
    class S(Data):
        sid: Annotated[str, Key]
        v: int
    wire(M).to(f).with_latest(S)
    # S has no wire(S).as_latest() declaration anywhere
    with pytest.raises(GraphError, match="with_latest.*requires.*as_latest"):
        compile_graph()
```

- [ ] **Step 2: 运行测试验证失败**

- [ ] **Step 3: 实现 graph.py**

```python
# app/runtime/graph.py
from dataclasses import dataclass
from app.runtime.data import Data
from app.runtime.node import NODE_REGISTRY, inputs_of
from app.runtime.wire import WIRING_REGISTRY, WireSpec

class GraphError(Exception): pass

@dataclass
class CompiledGraph:
    data_types: set[type[Data]]
    nodes: set
    wires: list[WireSpec]

def compile_graph() -> CompiledGraph:
    wires = list(WIRING_REGISTRY)
    # 1) every consumer in wires must be @node-registered
    for w in wires:
        for c in w.consumers:
            if c not in NODE_REGISTRY:
                raise GraphError(f"wire(to={c.__name__}): not registered as @node")
    # 2) .with_latest(X) requires some wire(X).as_latest() to exist
    latest_types = {w.data_type for w in wires if w.as_latest}
    for w in wires:
        for t in w.with_latest:
            if t not in latest_types:
                raise GraphError(
                    f"wire({w.data_type.__name__}).with_latest({t.__name__}) requires "
                    f"wire({t.__name__}).as_latest() declared somewhere"
                )
    # 3) consumer signature compatibility
    for w in wires:
        for c in w.consumers:
            ins = inputs_of(c)
            param_types = set(ins.values())
            needed = {w.data_type, *w.with_latest}
            if not needed.issubset(param_types):
                raise GraphError(
                    f"wire({w.data_type.__name__}).to({c.__name__}): consumer signature "
                    f"{ins} does not accept {needed}"
                )
    data_types = {w.data_type for w in wires} | {t for w in wires for t in w.with_latest}
    nodes = {c for w in wires for c in w.consumers}
    return CompiledGraph(data_types=data_types, nodes=nodes, wires=wires)
```

- [ ] **Step 4: 测试通过**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(runtime): compile_graph() with startup validation"
```

---

### Task 0.7: Schema Migrator

**Files:**
- Create: `apps/agent-service/app/runtime/migrator.py`
- Test: `apps/agent-service/tests/runtime/test_migrator.py`

审计要求：**只允许 CREATE TABLE IF NOT EXISTS 和 ALTER TABLE ADD COLUMN（带默认值或 nullable）**。DROP COLUMN / 改 type / DROP TABLE 全部拒绝启动，打 log 提示写显式迁移脚本。

- [ ] **Step 1: 写失败测试（用 pytest-postgresql 起临时 pg）**

```python
# tests/runtime/test_migrator.py
import pytest
from typing import Annotated
from app.runtime.data import Data, Key, DedupKey, Version, AdminOnly
from app.runtime.migrator import plan_migration, apply_migration, MigrationError

class Msg(Data):
    mid: Annotated[str, Key, DedupKey]
    gen: Annotated[int, DedupKey] = 0
    text: str

class State(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    mood: str

class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict

def test_plan_creates_table_for_new_data():
    plan = plan_migration([Msg], existing_schema={})
    stmts = [s.sql for s in plan.stmts]
    assert any("CREATE TABLE IF NOT EXISTS data_msg" in s for s in stmts)
    assert any("mid VARCHAR" in s or "mid TEXT" in s for s in stmts)
    # dedup_hash UNIQUE for idempotent durable writes
    assert any("dedup_hash" in s for s in stmts)

def test_plan_adds_index_on_key_version_for_append_only():
    plan = plan_migration([State], existing_schema={})
    stmts = " ".join(s.sql for s in plan.stmts)
    # State has Version -> index (pid, ver DESC)
    assert "CREATE INDEX" in stmts
    assert "pid" in stmts and "ver" in stmts

def test_plan_add_column_on_existing_table():
    existing = {"data_msg": {"mid": "text", "text": "text"}}  # gen missing
    plan = plan_migration([Msg], existing_schema=existing)
    stmts = [s.sql for s in plan.stmts]
    assert any("ALTER TABLE data_msg ADD COLUMN gen" in s for s in stmts)

def test_plan_rejects_breaking_change():
    existing = {"data_msg": {"mid": "text", "text": "text", "obsolete": "int"}}
    with pytest.raises(MigrationError, match="column data_msg.obsolete dropped"):
        plan_migration([Msg], existing_schema=existing)

def test_existing_table_mapping_skips_create():
    class Legacy(Data):
        mid: Annotated[str, Key]
        text: str
        class Meta:
            existing_table = "conversation_messages"
    existing = {"conversation_messages": {"mid": "text", "text": "text"}}
    plan = plan_migration([Legacy], existing_schema=existing)
    stmts = [s.sql for s in plan.stmts]
    assert not any("CREATE TABLE" in s for s in stmts)

def test_admin_only_not_migrated_by_business_code():
    # AdminOnly tables are managed externally; business migrator skips them.
    plan = plan_migration([Cfg], existing_schema={})
    stmts = [s.sql for s in plan.stmts]
    assert stmts == []  # skip entirely
```

- [ ] **Step 2: 运行测试验证失败**

- [ ] **Step 3: 实现 migrator.py（pydantic → DDL 的核心翻译）**

```python
# app/runtime/migrator.py
from dataclasses import dataclass
from typing import Any
from pydantic.fields import FieldInfo
from app.runtime.data import Data, key_fields, version_field, dedup_fields, is_admin_only

class MigrationError(Exception): pass

@dataclass
class Stmt:
    sql: str
    params: tuple = ()

@dataclass
class Plan:
    stmts: list[Stmt]

PY_TO_PG = {
    str: "TEXT", int: "BIGINT", float: "DOUBLE PRECISION",
    bool: "BOOLEAN", bytes: "BYTEA", dict: "JSONB", list: "JSONB",
}

def _table_name(cls: type[Data]) -> str:
    meta = getattr(cls, "Meta", None)
    if meta and getattr(meta, "existing_table", None):
        return meta.existing_table
    snake = cls.__name__[0].lower() + "".join(
        "_" + c.lower() if c.isupper() else c for c in cls.__name__[1:]
    )
    return f"data_{snake}"

def _pg_type(field: FieldInfo) -> str:
    import datetime
    t = field.annotation
    origin = getattr(t, "__origin__", t)
    if origin in PY_TO_PG:
        return PY_TO_PG[origin]
    if t is datetime.datetime:
        return "TIMESTAMPTZ"
    if hasattr(t, "__metadata__"):  # Annotated
        return _pg_type(type("F", (), {"annotation": t.__origin__})())
    return "TEXT"

def plan_migration(data_classes: list[type[Data]], existing_schema: dict[str, dict[str, str]]) -> Plan:
    stmts: list[Stmt] = []
    for cls in data_classes:
        if is_admin_only(cls):
            continue
        table = _table_name(cls)
        desired_cols = {name: _pg_type(f) for name, f in cls.model_fields.items()}
        if version_field(cls):
            desired_cols.setdefault(version_field(cls), "BIGINT")
        desired_cols["dedup_hash"] = "TEXT"
        desired_cols["created_at"] = "TIMESTAMPTZ DEFAULT now()"

        if table in existing_schema:
            existing_cols = existing_schema[table]
            # breaking check: no column drop
            for col in existing_cols:
                if col not in desired_cols and col not in ("id", "created_at", "updated_at"):
                    raise MigrationError(
                        f"column {table}.{col} dropped from {cls.__name__}; "
                        f"write explicit migration script"
                    )
            # additive: add missing columns (nullable or default)
            for col, typ in desired_cols.items():
                if col not in existing_cols:
                    default = " DEFAULT now()" if "DEFAULT" in typ else ""
                    stmts.append(Stmt(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ.split(' DEFAULT ')[0]}{default}"))
        else:
            # skip CREATE for existing_table mapping that points at missing table (pg owns it)
            if getattr(getattr(cls, "Meta", None), "existing_table", None):
                continue
            col_ddl = ", ".join(f"{n} {t}" for n, t in desired_cols.items())
            stmts.append(Stmt(f"CREATE TABLE IF NOT EXISTS {table} ({col_ddl})"))
            stmts.append(Stmt(f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_dedup ON {table}(dedup_hash)"))
            ver = version_field(cls)
            if ver:
                keys = key_fields(cls)
                cols = ", ".join(keys) + f", {ver} DESC"
                stmts.append(Stmt(f"CREATE INDEX IF NOT EXISTS ix_{table}_key_ver ON {table}({cols})"))
    return Plan(stmts=stmts)

async def apply_migration(plan: Plan, conn) -> None:
    for s in plan.stmts:
        await conn.execute(s.sql, *s.params)
```

- [ ] **Step 4: 测试通过**

- [ ] **Step 5: 追加集成测试（用真实 pg）**

```python
# tests/runtime/test_migrator_integration.py
import pytest
from app.runtime.migrator import plan_migration, apply_migration
from app.data.session import get_session
from sqlalchemy import text

@pytest.mark.asyncio
async def test_apply_creates_real_table(test_db):  # fixture drops/creates schema per test
    from tests.runtime.test_migrator import Msg
    plan = plan_migration([Msg], existing_schema={})
    async with get_session() as s:
        for stmt in plan.stmts:
            await s.execute(text(stmt.sql))
        # verify
        r = await s.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='data_msg'"))
        cols = {row[0] for row in r}
        assert {"mid", "gen", "text", "dedup_hash"}.issubset(cols)
```

- [ ] **Step 6: 测试通过**

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(runtime): Schema Migrator (pydantic -> pg DDL, additive only)"
```

---

### Task 0.8: append-only 写 + version 自增 + as_latest 读

**Files:**
- Create: `apps/agent-service/app/runtime/persist.py`
- Test: `apps/agent-service/tests/runtime/test_persist.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_persist.py
import pytest
from typing import Annotated
from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest, select_all_versions, insert_idempotent

class S(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    mood: str

@pytest.mark.asyncio
async def test_insert_append_auto_versions(test_db):
    await insert_append(S(pid="p1", mood="happy"))
    await insert_append(S(pid="p1", mood="sad"))
    latest = await select_latest(S, {"pid": "p1"})
    assert latest.mood == "sad"
    all_rows = await select_all_versions(S, {"pid": "p1"})
    assert [r.ver for r in all_rows] == [1, 2]  # monotonic

@pytest.mark.asyncio
async def test_multi_replica_concurrency(test_db):
    """Two concurrent inserts must not collide on version."""
    import asyncio
    await asyncio.gather(*[insert_append(S(pid="p1", mood=f"m{i}")) for i in range(20)])
    rows = await select_all_versions(S, {"pid": "p1"})
    assert len(rows) == 20
    versions = [r.ver for r in rows]
    assert versions == sorted(versions) and len(set(versions)) == 20

@pytest.mark.asyncio
async def test_insert_idempotent_on_conflict_do_nothing(test_db):
    # durable edge: same dedup_hash INSERTs → first wins, others silently skipped
    from typing import Annotated
    from app.runtime.data import Data, Key, DedupKey
    class M(Data):
        mid: Annotated[str, Key, DedupKey]
        gen: Annotated[int, DedupKey] = 0
        text: str
    n1 = await insert_idempotent(M(mid="m1", text="first"))
    n2 = await insert_idempotent(M(mid="m1", text="second"))  # same dedup_hash
    assert n1 == 1
    assert n2 == 0  # skipped
    rows = await select_all_versions(M, {"mid": "m1"})
    assert len(rows) == 1
    assert rows[0].text == "first"  # history preserved, no overwrite
```

- [ ] **Step 2: 实现 persist.py**

```python
# app/runtime/persist.py
import hashlib, json
from sqlalchemy import text
from app.runtime.data import Data, key_fields, dedup_fields, version_field
from app.runtime.migrator import _table_name
from app.data.session import get_session

def _dedup_hash(obj: Data) -> str:
    cols = dedup_fields(type(obj))
    payload = json.dumps({c: getattr(obj, c) for c in cols}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()

async def insert_append(obj: Data) -> int:
    """Append a new version. Version = max(version)+1 for same Key, atomic via advisory lock."""
    cls = type(obj)
    table = _table_name(cls)
    ver_col = version_field(cls)
    keys = key_fields(cls)
    dedup = _dedup_hash(obj)
    cols_map = {c: getattr(obj, c) for c in cls.model_fields}
    cols_map["dedup_hash"] = dedup
    async with get_session() as s:
        # lock by key hash to serialize version computation
        key_tuple = tuple(getattr(obj, k) for k in keys)
        lock_key = int(hashlib.md5(str(key_tuple).encode()).hexdigest()[:15], 16) % (2**31)
        await s.execute(text(f"SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})
        if ver_col:
            where = " AND ".join(f"{k} = :{k}" for k in keys)
            r = await s.execute(text(f"SELECT COALESCE(MAX({ver_col}), 0) FROM {table} WHERE {where}"),
                               {k: getattr(obj, k) for k in keys})
            cols_map[ver_col] = r.scalar() + 1
        cols = list(cols_map.keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        await s.execute(text(sql), cols_map)
    return 1

async def insert_idempotent(obj: Data) -> int:
    """INSERT ... ON CONFLICT (dedup_hash) DO NOTHING. Returns 1 if inserted, 0 if skipped."""
    cls = type(obj)
    table = _table_name(cls)
    dedup = _dedup_hash(obj)
    cols_map = {c: getattr(obj, c) for c in cls.model_fields}
    cols_map["dedup_hash"] = dedup
    cols = list(cols_map.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT (dedup_hash) DO NOTHING")
    async with get_session() as s:
        r = await s.execute(text(sql), cols_map)
        return r.rowcount or 0

async def select_latest(cls: type[Data], keys_values: dict) -> Data | None:
    table = _table_name(cls)
    keys = key_fields(cls)
    ver = version_field(cls)
    where = " AND ".join(f"{k} = :{k}" for k in keys)
    order = f"{', '.join(keys)}, {ver} DESC" if ver else f"{', '.join(keys)}"
    sql = f"SELECT DISTINCT ON ({', '.join(keys)}) * FROM {table} WHERE {where} ORDER BY {order}"
    async with get_session() as s:
        r = await s.execute(text(sql), keys_values)
        row = r.mappings().first()
        return cls(**{k: row[k] for k in cls.model_fields}) if row else None

async def select_all_versions(cls: type[Data], keys_values: dict) -> list[Data]:
    table = _table_name(cls)
    keys = key_fields(cls)
    ver = version_field(cls)
    where = " AND ".join(f"{k} = :{k}" for k in keys)
    order = ver if ver else "created_at"
    sql = f"SELECT * FROM {table} WHERE {where} ORDER BY {order}"
    async with get_session() as s:
        r = await s.execute(text(sql), keys_values)
        return [cls(**{k: row[k] for k in cls.model_fields}) for row in r.mappings().all()]
```

- [ ] **Step 3: 测试通过**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(runtime): append-only persist (version auto-increment + DISTINCT ON read + ON CONFLICT DO NOTHING)"
```

---

### Task 0.9: query(T) 通用查询

**Files:**
- Create: `apps/agent-service/app/runtime/query.py`
- Test: `apps/agent-service/tests/runtime/test_query.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_query.py
import pytest
from typing import Annotated
from app.runtime.data import Data, Key
from app.runtime.query import query
from app.runtime.persist import insert_idempotent

class M(Data):
    mid: Annotated[str, Key]
    chat_id: str
    text: str

@pytest.mark.asyncio
async def test_query_where_limit(test_db):
    await insert_idempotent(M(mid="m1", chat_id="c1", text="a"))
    await insert_idempotent(M(mid="m2", chat_id="c1", text="b"))
    await insert_idempotent(M(mid="m3", chat_id="c2", text="c"))
    rows = await query(M).where(chat_id="c1").all()
    assert len(rows) == 2
    rows = await query(M).where(chat_id="c1").limit(1).all()
    assert len(rows) == 1

@pytest.mark.asyncio
async def test_query_order_by_desc(test_db):
    # assumes created_at auto-populated by migrator
    rows = await query(M).where(chat_id="c1").order_by_desc("mid").all()
    assert [r.mid for r in rows] == ["m2", "m1"]
```

- [ ] **Step 2: 实现 query.py**

```python
# app/runtime/query.py
from sqlalchemy import text
from app.runtime.data import Data, key_fields, version_field
from app.runtime.migrator import _table_name
from app.data.session import get_session

class Query:
    def __init__(self, cls: type[Data]):
        self.cls = cls
        self._where: dict = {}
        self._limit: int | None = None
        self._order: tuple[str, bool] | None = None  # (col, desc)
        self._all_versions: bool = False

    def where(self, **kv) -> "Query":
        self._where.update(kv); return self

    def limit(self, n: int) -> "Query":
        self._limit = n; return self

    def order_by_desc(self, col: str) -> "Query":
        self._order = (col, True); return self

    def order_by_asc(self, col: str) -> "Query":
        self._order = (col, False); return self

    def all_versions(self) -> "Query":
        self._all_versions = True; return self

    async def all(self) -> list[Data]:
        table = _table_name(self.cls)
        keys = key_fields(self.cls)
        ver = version_field(self.cls)
        where_sql = " AND ".join(f"{k} = :{k}" for k in self._where) or "TRUE"
        if ver and not self._all_versions:
            base = f"SELECT DISTINCT ON ({', '.join(keys)}) * FROM {table} WHERE {where_sql} ORDER BY {', '.join(keys)}, {ver} DESC"
        else:
            base = f"SELECT * FROM {table} WHERE {where_sql}"
        if self._order:
            col, desc = self._order
            base += f" ORDER BY {col} {'DESC' if desc else 'ASC'}"
        if self._limit:
            base += f" LIMIT {self._limit}"
        async with get_session() as s:
            r = await s.execute(text(base), self._where)
            return [self.cls(**{k: row[k] for k in self.cls.model_fields}) for row in r.mappings().all()]

def query(cls: type[Data]) -> Query:
    return Query(cls)
```

- [ ] **Step 3: 测试通过**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(runtime): query(T) generic query builder"
```

---

### Task 0.10: runtime.emit() + 进程内默认边

**Files:**
- Create: `apps/agent-service/app/runtime/emit.py`
- Test: `apps/agent-service/tests/runtime/test_emit_inprocess.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_emit_inprocess.py
import pytest
from typing import Annotated
from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.wire import wire, clear_wiring
from app.runtime.graph import compile_graph
from app.runtime.emit import emit, reset_emit_runtime

class M(Data):
    mid: Annotated[str, Key]
    text: str

calls: list = []

@node
async def recorder(m: M) -> None:
    calls.append(m)

def setup_function():
    clear_wiring()
    calls.clear()
    reset_emit_runtime()

@pytest.mark.asyncio
async def test_emit_default_edge_awaits_consumer():
    wire(M).to(recorder)  # default (in-process)
    compile_graph()
    await emit(M(mid="m1", text="hi"))
    assert len(calls) == 1
    assert calls[0].text == "hi"

@pytest.mark.asyncio
async def test_emit_when_predicate_filters():
    @node
    async def only_x(m: M) -> None: calls.append(m)
    wire(M).to(only_x).when(lambda m: m.mid == "x")
    compile_graph()
    await emit(M(mid="y", text="skip"))
    await emit(M(mid="x", text="keep"))
    assert [c.text for c in calls] == ["keep"]
```

- [ ] **Step 2: 实现 emit.py（只实现 in-process + when predicate；durable 在 Task 0.11）**

```python
# app/runtime/emit.py
from app.runtime.data import Data
from app.runtime.graph import compile_graph, CompiledGraph

_graph: CompiledGraph | None = None

def reset_emit_runtime() -> None:
    global _graph
    _graph = None

def _get_graph() -> CompiledGraph:
    global _graph
    if _graph is None:
        _graph = compile_graph()
    return _graph

async def emit(data: Data) -> None:
    graph = _get_graph()
    cls = type(data)
    for w in graph.wires:
        if w.data_type is not cls:
            continue
        if w.predicate and not w.predicate(data):
            continue
        for c in w.consumers:
            if w.durable:
                # handled by Task 0.11 — durable queue
                from app.runtime.durable import publish_durable
                await publish_durable(w, c, data)
            else:
                # in-process: resolve with_latest, then call
                kwargs = _resolve_inputs(c, data, w)
                await c(**kwargs)

def _resolve_inputs(consumer, data: Data, wire_spec) -> dict:
    from app.runtime.node import inputs_of
    from app.runtime.persist import select_latest
    from app.runtime.data import key_fields
    ins = inputs_of(consumer)
    kwargs = {}
    for name, t in ins.items():
        if t is type(data):
            kwargs[name] = data
        elif t in wire_spec.with_latest:
            # fetch latest X by key — requires data to know key; here we assume
            # persona_id convention: if the Data has a 'persona_id' and X needs it, match by that.
            # Phase 0 MVP: match by first key of X using same-named field on data.
            key = key_fields(t)[0]
            val = getattr(data, key, None)
            if val is None:
                raise RuntimeError(f"with_latest({t.__name__}) needs {key} on {type(data).__name__}")
            kwargs[name] = await select_latest(t, {key: val})
    return kwargs
```

- [ ] **Step 3: 测试通过**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(runtime): emit() + in-process default edge + when() predicate"
```

---

### Task 0.11: durable 边（RabbitMQ 适配）+ trace/lane context 穿越

**Files:**
- Create: `apps/agent-service/app/runtime/durable.py`
- Test: `apps/agent-service/tests/runtime/test_durable.py`

> **TODO follow-up（2026-04-23 vectorize paper-read 发现）：** 现 durable consumer 靠 RabbitMQ
> `prefetch_count=10` 限背压；旧 vectorize worker 另外用 `asyncio.Semaphore(10)` 限并发
> `process_message()`。Phase 1 真接入时若发现 Ark embedding / Qdrant 资源不够，可能需要在
> `_consume_loop` 内部加一层 per-node Semaphore，或在 `@node(concurrency=...)` 上给出装饰参数。
> 先不在 T0.11 加，等 Phase 1 有真实资源压力证据再决定怎么加。

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_durable.py
import pytest, asyncio
from typing import Annotated
from contextvars import ContextVar
from app.runtime.data import Data, Key, DedupKey
from app.runtime.node import node
from app.runtime.wire import wire, clear_wiring
from app.runtime.graph import compile_graph
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.durable import start_consumers, stop_consumers
from app.api.middleware import trace_id_var, lane_var  # existing contextvars

class M(Data):
    mid: Annotated[str, Key, DedupKey]
    text: str

received: list = []
received_trace: list = []
received_lane: list = []

@node
async def sink(m: M) -> None:
    received.append(m)
    received_trace.append(trace_id_var.get())
    received_lane.append(lane_var.get())

@pytest.fixture
async def durable_env(rabbitmq, test_db):
    clear_wiring()
    received.clear(); received_trace.clear(); received_lane.clear()
    reset_emit_runtime()
    wire(M).to(sink).durable()
    compile_graph()
    await start_consumers()
    yield
    await stop_consumers()

@pytest.mark.asyncio
async def test_durable_roundtrip(durable_env):
    trace_id_var.set("T1"); lane_var.set("prod")
    await emit(M(mid="m1", text="hi"))
    for _ in range(20):
        if received: break
        await asyncio.sleep(0.1)
    assert len(received) == 1
    assert received_trace == ["T1"]
    assert received_lane == ["prod"]

@pytest.mark.asyncio
async def test_durable_idempotent(durable_env):
    # same dedup_hash twice → only 1 row, consumer sees it once (via DB dedup)
    trace_id_var.set("T2")
    await emit(M(mid="m1", text="first"))
    await emit(M(mid="m1", text="first"))  # same payload
    for _ in range(20):
        if received: break
        await asyncio.sleep(0.1)
    assert len(received) == 1  # second insert skipped by ON CONFLICT DO NOTHING
```

- [ ] **Step 2: 实现 durable.py**

复用现有 `app/infra/rabbitmq.py` 的 `mq` 单例；每条 durable wire 用一个专用 queue，queue name 按 `{data_type_snake}_{consumer_name}` 生成。

```python
# app/runtime/durable.py
import json, asyncio
from typing import Callable
from app.runtime.data import Data, dedup_fields
from app.runtime.wire import WireSpec
from app.runtime.persist import insert_idempotent
from app.infra.rabbitmq import mq
from app.api.middleware import trace_id_var, lane_var, session_id_var

_consumers: list = []
_running = False

def _queue_name(w: WireSpec, consumer: Callable) -> str:
    t = w.data_type.__name__
    snake = t[0].lower() + "".join("_" + c.lower() if c.isupper() else c for c in t[1:])
    return f"durable_{snake}_{consumer.__name__}"

async def publish_durable(w: WireSpec, consumer: Callable, data: Data) -> None:
    # pg outbox: INSERT with ON CONFLICT DO NOTHING — if skipped, don't publish (already handled)
    n = await insert_idempotent(data)
    if n == 0:
        return  # duplicate, already processed
    headers = {
        "trace_id": trace_id_var.get(""),
        "lane": lane_var.get(""),
        "session_id": session_id_var.get(""),
        "data_type": type(data).__name__,
    }
    queue = _queue_name(w, consumer)
    body = data.model_dump_json().encode()
    await mq.publish_raw(queue, body, headers=headers)

async def _consume_loop(queue: str, consumer: Callable, data_cls: type[Data]) -> None:
    async def handler(msg):
        headers = msg.headers or {}
        tok = trace_id_var.set(headers.get("trace_id", ""))
        lok = lane_var.set(headers.get("lane", ""))
        sok = session_id_var.set(headers.get("session_id", ""))
        try:
            payload = json.loads(msg.body)
            obj = data_cls(**payload)
            await consumer(**{next(iter(inputs_of(consumer))): obj})
        finally:
            trace_id_var.reset(tok)
            lane_var.reset(lok)
            session_id_var.reset(sok)
    await mq.consume(queue, handler)

async def start_consumers() -> None:
    global _running
    from app.runtime.graph import compile_graph
    from app.runtime.node import inputs_of  # noqa: F401 -- used in handler
    graph = compile_graph()
    _running = True
    for w in graph.wires:
        if not w.durable:
            continue
        await mq.declare_queue(_queue_name(w, w.consumers[0]))  # one queue per consumer
        for c in w.consumers:
            task = asyncio.create_task(_consume_loop(_queue_name(w, c), c, w.data_type))
            _consumers.append(task)

async def stop_consumers() -> None:
    global _running
    _running = False
    for t in _consumers:
        t.cancel()
    _consumers.clear()
```

必要时扩展 `app/infra/rabbitmq.py`：加 `publish_raw(queue, body, headers)` 和 `declare_queue(name)` 如果现有 API 不够用。如果已有等价方法，直接 delegate。

- [ ] **Step 3: 扩展 `app/infra/rabbitmq.py`（如需要）**

Grep 当前 API：`grep -n "async def publish\|async def consume\|declare_queue" apps/agent-service/app/infra/rabbitmq.py`。补齐缺失方法。

- [ ] **Step 4: 测试通过**

需要本地 rabbitmq + pg（docker-compose.test.yml）。运行 `make test-integration` 或 `uv run pytest apps/agent-service/tests/runtime/test_durable.py`.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(runtime): durable edge via rabbitmq + trace/lane context propagation"
```

---

### Task 0.12: Capability - LLMClient

**Files:**
- Create: `apps/agent-service/app/capabilities/__init__.py`
- Create: `apps/agent-service/app/capabilities/llm.py`
- Test: `apps/agent-service/tests/capabilities/test_llm.py`

- [ ] **Step 1: 写失败测试（适配器层，不测真 LLM）**

```python
# tests/capabilities/test_llm.py
import pytest
from unittest.mock import AsyncMock, patch
from app.capabilities.llm import LLMClient

@pytest.mark.asyncio
async def test_complete_delegates_to_langchain():
    with patch("app.capabilities.llm.build_chat_model") as m:
        fake = AsyncMock()
        fake.ainvoke = AsyncMock(return_value=type("R", (), {"content": "ok"})())
        m.return_value = fake
        client = LLMClient(model_id="deepseek-chat")
        out = await client.complete("hi")
    assert out == "ok"
    fake.ainvoke.assert_awaited_once()

@pytest.mark.asyncio
async def test_stream_yields_chunks():
    async def fake_stream(*args, **kwargs):
        for s in ["a", "b", "c"]:
            yield type("C", (), {"content": s})()
    with patch("app.capabilities.llm.build_chat_model") as m:
        m.return_value = type("F", (), {"astream": fake_stream})()
        client = LLMClient(model_id="x")
        out = [c async for c in client.stream("hi")]
    assert out == ["a", "b", "c"]
```

- [ ] **Step 2: 实现 llm.py**

```python
# app/capabilities/llm.py
from typing import AsyncIterator
from app.agent.models import build_chat_model

class LLMClient:
    def __init__(self, model_id: str):
        self._model = build_chat_model(model_id)

    async def complete(self, prompt: str, **kwargs) -> str:
        r = await self._model.ainvoke(prompt, **kwargs)
        return r.content

    async def stream(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        async for chunk in self._model.astream(prompt, **kwargs):
            yield chunk.content
```

- [ ] **Step 3: 测试通过**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(capabilities): LLMClient adapter over build_chat_model"
```

---

### Task 0.13: Capability - EmbedderClient + VectorStore + HTTPClient + AgentRunner

**Files:**
- Create: `apps/agent-service/app/capabilities/embed.py`
- Create: `apps/agent-service/app/capabilities/vector_store.py`
- Create: `apps/agent-service/app/capabilities/http.py`
- Create: `apps/agent-service/app/capabilities/agent.py`
- Tests: 每个 capability 一个 test_*.py

每个 capability 是现有实现的薄 wrap。按同一模式 TDD：每个 1 失败测试 → 1 实现 → 通过 → commit。以下给出实现，测试按 LLM 同 pattern 写。

- [ ] **Step 1: 实现 embed.py**

```python
# app/capabilities/embed.py
from typing import Literal
from app.agent.embedding import embed_dense, embed_hybrid

class EmbedderClient:
    async def encode(self, text: str, images: list[str] = (), *, mode: Literal["dense", "hybrid"] = "dense",
                     instruction: str = "") -> dict:
        if mode == "hybrid":
            return await embed_hybrid(text, list(images), instruction=instruction)
        return {"dense": await embed_dense(text, list(images), instruction=instruction)}
```

- [ ] **Step 2: 实现 vector_store.py**

```python
# app/capabilities/vector_store.py
from app.infra.qdrant import _qdrant  # existing singleton

class VectorStore:
    def __init__(self, collection: str):
        self.collection = collection

    async def upsert(self, fragment_id: str, vectors: dict, payload: dict) -> None:
        await _qdrant.upsert_hybrid_vectors(self.collection, fragment_id, vectors, payload)

    async def search(self, vec, k: int, filter: dict | None = None):
        return await _qdrant.hybrid_search(self.collection, vec, k, filter)
```

- [ ] **Step 3: 实现 http.py**

```python
# app/capabilities/http.py
import httpx
from app.infra.lane import lane_router
from app.api.middleware import trace_id_var, lane_var

class HTTPClient:
    def __init__(self, service: str | None = None, timeout: float = 30.0):
        self.service = service
        self._client = httpx.AsyncClient(timeout=timeout)

    def _headers(self, extra: dict | None = None) -> dict:
        h = {}
        if tid := trace_id_var.get(""):
            h["X-Trace-Id"] = tid
        if lane := lane_var.get(""):
            h["x-lane"] = lane
        if self.service:
            h.update(lane_router.get_headers(self.service, lane))
        if extra:
            h.update(extra)
        return h

    def _url(self, path: str) -> str:
        if self.service and not path.startswith("http"):
            return lane_router.base_url(self.service, lane_var.get("")) + path
        return path

    async def get(self, path: str, **kw): return await self._client.get(self._url(path), headers=self._headers(), **kw)
    async def post(self, path: str, **kw): return await self._client.post(self._url(path), headers=self._headers(), **kw)

    async def close(self): await self._client.aclose()
```

- [ ] **Step 4: 实现 agent.py**

```python
# app/capabilities/agent.py
from app.agent.core import Agent

class AgentRunner:
    def __init__(self, agent_name: str, **cfg):
        self._agent = Agent(agent_name, **cfg)

    async def run(self, ctx: dict): return await self._agent.run(ctx)
    async def stream(self, ctx: dict, tools=None):
        async for chunk in self._agent.stream(ctx, tools=tools): yield chunk
    async def extract(self, ctx: dict, schema): return await self._agent.extract(ctx, schema)
```

- [ ] **Step 5: 对每个 capability 写测试并通过**（参照 Task 0.12 pattern）

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(capabilities): EmbedderClient / VectorStore / HTTPClient / AgentRunner adapters"
```

---

### Task 0.14: Deployment layer - bind() + nodes_for_app()

**Files:**
- Create: `apps/agent-service/app/runtime/placement.py`
- Test: `apps/agent-service/tests/runtime/test_placement.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_placement.py
import pytest
from typing import Annotated
from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.placement import bind, nodes_for_app, clear_bindings

class M(Data): mid: Annotated[str, Key]

@node
async def worker_node(m: M) -> None: ...

@node
async def main_node(m: M) -> None: ...

def setup_function():
    clear_bindings()

def test_bind_to_app():
    bind(worker_node).to_app("vectorize-worker")
    assert worker_node in nodes_for_app("vectorize-worker")

def test_unbound_nodes_go_to_agent_service():
    bind(worker_node).to_app("vectorize-worker")
    # main_node was not bound → goes to "agent-service" by default
    assert main_node in nodes_for_app("agent-service")
    assert main_node not in nodes_for_app("vectorize-worker")

def test_rebind_rejected():
    bind(worker_node).to_app("vectorize-worker")
    with pytest.raises(RuntimeError, match="already bound"):
        bind(worker_node).to_app("arq-worker")
```

- [ ] **Step 2: 实现 placement.py**

```python
# app/runtime/placement.py
from typing import Callable
from app.runtime.node import NODE_REGISTRY

DEFAULT_APP = "agent-service"
_BINDINGS: dict[Callable, str] = {}

def clear_bindings() -> None:
    _BINDINGS.clear()

class _Binder:
    def __init__(self, fn: Callable): self._fn = fn
    def to_app(self, app_name: str) -> None:
        if self._fn in _BINDINGS:
            raise RuntimeError(f"{self._fn.__name__} already bound to {_BINDINGS[self._fn]}")
        _BINDINGS[self._fn] = app_name

def bind(fn: Callable) -> _Binder:
    return _Binder(fn)

def nodes_for_app(app_name: str) -> set[Callable]:
    explicit = {n for n, a in _BINDINGS.items() if a == app_name}
    if app_name == DEFAULT_APP:
        unbound = NODE_REGISTRY - set(_BINDINGS.keys())
        return explicit | unbound
    return explicit
```

- [ ] **Step 3: 测试通过**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(runtime): bind() / nodes_for_app() placement layer"
```

---

### Task 0.15: runtime.run() + Source scheduling

**Files:**
- Create: `apps/agent-service/app/runtime/engine.py`
- Create: `apps/agent-service/app/workers/runtime_entry.py`
- Test: `apps/agent-service/tests/runtime/test_engine.py`

> **集成注意（2026-04-23 vectorize paper-read 发现）：** 旧 `cron_scan_pending_messages()` 用
> Redis `SET NX EX` 做分布式锁，防止多 vectorize-worker 实例重复扫。CronSource 要么内置
> `lock_key: str | None` 参数自动走 Redis 锁，要么让 @node 内部自己加锁（后者会在每个 cron node
> 里重复加锁代码）。倾向前者 —— 在 `Source(kind="cron", ..., lock_key="vectorize_scan")` 处
> 加一层 lock capability。真实现时确认一下是否有其他 cron 也需要锁，一次性抽出来。

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_engine.py
import pytest, asyncio
from typing import Annotated
from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.wire import wire, clear_wiring
from app.runtime.source import Source
from app.runtime.engine import Runtime
from app.runtime.placement import bind, clear_bindings

class Tick(Data): ts: Annotated[str, Key]

fires: list = []

@node
async def ticker(t: Tick) -> None:
    fires.append(t)

def setup_function():
    clear_wiring(); clear_bindings(); fires.clear()

@pytest.mark.asyncio
async def test_cron_source_fires_consumer(freeze_time):
    wire(Tick).from_(Source.cron("*/1 * * * * *")).to(ticker)  # every second (test only)
    bind(ticker).to_app("agent-service")
    rt = Runtime(app_name="agent-service")
    task = asyncio.create_task(rt.run())
    await asyncio.sleep(2.2)
    task.cancel()
    assert len(fires) >= 2
```

- [ ] **Step 2: 实现 engine.py**

```python
# app/runtime/engine.py
import asyncio, os
from crontab import CronTab  # or similar
from app.runtime.graph import compile_graph
from app.runtime.placement import nodes_for_app
from app.runtime.durable import start_consumers, stop_consumers
from app.runtime.migrator import plan_migration, apply_migration
from app.runtime.data import DATA_REGISTRY

class Runtime:
    def __init__(self, app_name: str | None = None):
        self.app_name = app_name or os.environ["APP_NAME"]
        self._tasks: list[asyncio.Task] = []
        self._running = False

    async def migrate_schema(self) -> None:
        # load existing schema from information_schema, then plan+apply
        from app.data.session import get_session
        from sqlalchemy import text
        async with get_session() as s:
            r = await s.execute(text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public'"
            ))
            existing: dict[str, dict[str, str]] = {}
            for t, c, typ in r.all():
                existing.setdefault(t, {})[c] = typ
            plan = plan_migration(list(DATA_REGISTRY), existing)
            from app.runtime.migrator import apply_migration
            async with get_session() as s2:
                await apply_migration(plan, s2)

    async def run(self) -> None:
        await self.migrate_schema()
        graph = compile_graph()
        my_nodes = nodes_for_app(self.app_name)
        # filter durable consumers to my nodes only
        await start_consumers()  # durable consumers for all my-bound wires
        # Source schedulers: cron only for my-bound wires
        for w in graph.wires:
            if not any(c in my_nodes for c in w.consumers):
                continue
            for src in w.sources:
                if src.kind == "cron":
                    self._tasks.append(asyncio.create_task(self._cron_loop(src, w)))
                # http / feishu_webhook are registered by FastAPI app (see Task 0.16)
        self._running = True
        try:
            while self._running: await asyncio.sleep(1)
        finally:
            await stop_consumers()
            for t in self._tasks: t.cancel()

    async def _cron_loop(self, src, wire_spec):
        from app.runtime.emit import emit
        # use croniter or similar for next-fire scheduling
        from croniter import croniter
        import datetime
        it = croniter(src.params["expr"], datetime.datetime.now())
        while self._running:
            next_t = it.get_next(datetime.datetime)
            sleep_s = (next_t - datetime.datetime.now()).total_seconds()
            if sleep_s > 0: await asyncio.sleep(sleep_s)
            # synthesize a Tick-like Data: wire authors use Source.cron + a Data class whose
            # fields all have defaults → runtime can construct empty instance with only timestamp
            payload = wire_spec.data_type(ts=next_t.isoformat())  # 约定：cron Data 需要 ts 字段
            await emit(payload)
```

注：`crontab` / `croniter` 二选一（用 `croniter` 因为更简单；加进 pyproject.toml）。

- [ ] **Step 3: 实现 workers/runtime_entry.py**

```python
# app/workers/runtime_entry.py
"""Unified worker entry. Reads APP_NAME from env, runs runtime."""
import asyncio
from app.runtime.engine import Runtime

# Trigger all registrations by importing wiring package
import app.wiring  # noqa: F401
import app.deployment  # noqa: F401

def main() -> None:
    asyncio.run(Runtime().run())

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 测试通过**（用 fake cron expr 加速或把 cron 改成 interval）

- [ ] **Step 5: 加 croniter 到依赖**

```bash
cd apps/agent-service && uv add croniter
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(runtime): Runtime.run() + cron source scheduler + worker entry"
```

---

### Task 0.16: HTTP Source 注册到 FastAPI

**Files:**
- Modify: `apps/agent-service/app/main.py`
- Create: `apps/agent-service/app/runtime/http_source.py`
- Test: `apps/agent-service/tests/runtime/test_http_source.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_http_source.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typing import Annotated
from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.wire import wire, clear_wiring
from app.runtime.source import Source
from app.runtime.http_source import register_http_sources

class Req(Data):
    rid: Annotated[str, Key]
    payload: dict

received: list = []

@node
async def handler(r: Req) -> None:
    received.append(r)

def test_http_source_registers_endpoint():
    clear_wiring(); received.clear()
    wire(Req).from_(Source.http("/chat")).to(handler)
    app = FastAPI()
    register_http_sources(app)
    c = TestClient(app)
    r = c.post("/chat", json={"rid": "r1", "payload": {"x": 1}})
    assert r.status_code == 202
    assert len(received) == 1
```

- [ ] **Step 2: 实现 http_source.py**

```python
# app/runtime/http_source.py
from fastapi import FastAPI, Request
from app.runtime.wire import WIRING_REGISTRY
from app.runtime.emit import emit

def register_http_sources(app: FastAPI) -> None:
    for w in WIRING_REGISTRY:
        for src in w.sources:
            if src.kind != "http":
                continue
            path = src.params["path"]
            data_cls = w.data_type
            async def ep(req: Request, cls=data_cls):
                payload = await req.json()
                await emit(cls(**payload))
                return {"accepted": True}
            app.post(path, status_code=202)(ep)
```

- [ ] **Step 3: 修改 main.py 在 startup 调用**

```python
# app/main.py (existing FastAPI app)
from app.runtime.http_source import register_http_sources
from app.runtime.engine import Runtime
import app.wiring  # noqa
import app.deployment  # noqa

@app.on_event("startup")
async def _runtime_startup():
    rt = Runtime()
    await rt.migrate_schema()
    register_http_sources(app)
    # HTTP pod: durable consumers can also run here if bound to "agent-service"
    from app.runtime.durable import start_consumers
    await start_consumers()
```

- [ ] **Step 4: 测试通过**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(runtime): HTTP Source -> FastAPI endpoint registration"
```

---

*Phase 0 到此结束。原 T0.17 Legacy Bridge 已按"依赖方向"原则挪到 Phase 1 —— 它依赖 `app.domain.message.Message`，属于 B 层（bridge）不是 F 层（framework）。新编号为 **Task 1.2.5: Message Bridge**，位于 `app/bridges/`。*

---

## Phase 1 — Vectorize 管线迁移

### Task 1.1: Message Data（接管 conversation_messages 表）

> **⚠️ 字段决策（2026-04-23，对照真实 ORM 修订）**：Message Data 的字段**严格**对齐真实 `conversation_messages` 列（见 `apps/agent-service/app/data/models.py::ConversationMessage`），不画饼加新字段。原 plan 的 `text/images/persona_id/generation` 全部去除：
> - `text` → 改名 `content`（和真实列一致）
> - `images` → 去掉（vectorize @node 里 `parse_content(content).image_keys` 拿）
> - `persona_id` → 去掉（vectorize 里从 `bot_name` 推导或不使用）
> - `generation` → 去掉（conversation_messages 语义上 PK 唯一，不需要多版本）
>
> 加上真实表所有非自增字段：`user_id / role / root_message_id / reply_message_id / chat_id / chat_type / create_time / message_type / vector_status / bot_name / response_id`。
>
> **migrator 行为**：`Meta.existing_table` 让 migrator 进入 adoption-mode，**完全跳过** Message 的 DDL（`migrator.py` 文件头 docstring 明写）。所以**不需要扩 migrator**，migration plan 对 Message 返回零 DDL。
>
> **persist 行为**：`insert_idempotent` 当前硬绑 `dedup_hash` 列。Message 的真实表没这列，必须扩 F 层支持 `Meta.dedup_column="message_id"` —— 让 INSERT 跳过 `dedup_hash` 列写入，且 `ON CONFLICT` 目标改成 `message_id`。

**Files:**
- Create: `apps/agent-service/app/domain/__init__.py`
- Create: `apps/agent-service/app/domain/message.py`
- Modify: `apps/agent-service/app/runtime/persist.py`（F 扩展:支持 `Meta.dedup_column`)
- Test: `apps/agent-service/tests/domain/test_message.py`
- Test: `apps/agent-service/tests/runtime/test_persist.py`（新增 dedup_column 用例）

- [ ] **Step 1: 写 Message Data 失败测试**

```python
# tests/domain/test_message.py
from app.domain.message import Message
from app.runtime.data import key_fields, dedup_fields


def test_message_key_is_message_id():
    assert key_fields(Message) == ("message_id",)


def test_message_dedup_column_is_message_id():
    # Meta.dedup_column="message_id" → insert_idempotent ON CONFLICT (message_id)
    assert Message.Meta.dedup_column == "message_id"


def test_message_existing_table():
    assert Message.Meta.existing_table == "conversation_messages"


def test_message_instance_matches_real_schema():
    m = Message(
        message_id="m1",
        user_id="u1",
        content="hi",
        role="user",
        root_message_id="r1",
        reply_message_id=None,
        chat_id="c1",
        chat_type="p2p",
        create_time=1234567890,
        message_type="text",
        vector_status="pending",
        bot_name=None,
        response_id=None,
    )
    assert m.message_id == "m1"
    assert m.content == "hi"
```

- [ ] **Step 2: 实现 message.py（字段对齐真实 `conversation_messages`)**

```python
# app/domain/message.py
"""Message Data — takes over the legacy conversation_messages table.

Fields mirror ``app.data.models.ConversationMessage`` 1:1 (minus the auto-
increment ``id`` column, which the migrator's adoption-mode ignores). The
table is owned by pre-existing migrations; this Data class is the new typed
interface for reading/writing through runtime.persist / runtime.query.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


class Message(Data):
    message_id: Annotated[str, Key]
    user_id: str
    content: str
    role: str
    root_message_id: str
    reply_message_id: str | None = None
    chat_id: str
    chat_type: str
    create_time: int
    message_type: str | None = "text"
    vector_status: str = "pending"
    bot_name: str | None = None
    response_id: str | None = None

    class Meta:
        existing_table = "conversation_messages"
        # Real PK is ``message_id``; there is no ``dedup_hash`` column, so
        # the persist layer must ON CONFLICT on message_id instead.
        dedup_column = "message_id"
```

- [ ] **Step 3: 更新 `app/domain/__init__.py`**

```python
# app/domain/__init__.py
"""Business Data classes — pydantic models carried through the runtime graph."""
from app.domain.message import Message

__all__ = ["Message"]
```

- [ ] **Step 4: F 扩展 — `persist.insert_idempotent` 支持 `Meta.dedup_column`**

当 Data 类 `Meta.dedup_column` 指定时：
- `INSERT` 的列列表**不包含** `dedup_hash`（真实表没这列）
- `ON CONFLICT` 目标改成 `Meta.dedup_column` 指定的列

```python
# app/runtime/persist.py::insert_idempotent (修改)
cls = type(obj)
table = _table_name(cls)

meta = getattr(cls, "Meta", None)
dedup_col = getattr(meta, "dedup_column", None) if meta else None

cols_map: dict[str, Any] = {c: getattr(obj, c) for c in cls.model_fields}
if not dedup_col:
    cols_map["dedup_hash"] = _dedup_hash(obj)

cols = list(cols_map.keys())
placeholders = ", ".join(f":{c}" for c in cols)
conflict_target = dedup_col or "dedup_hash"
sql = (
    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
    f"ON CONFLICT ({conflict_target}) DO NOTHING RETURNING 1"
)
# rest unchanged
```

> **分层提示**：这步改 F 层（`app/runtime/persist.py`），F 层对外接口不变（`insert_idempotent(obj)`），只是内部按 `Meta.dedup_column` 分叉。测试放 `tests/runtime/test_persist.py`（**不是** `tests/domain/`）。

- [ ] **Step 5: 写 F 扩展测试**

```python
# tests/runtime/test_persist.py (新增)
# 用一个临时 Data 类绑到已有 pg 表（或通过 migrate() helper 创建）
# 验证 insert_idempotent：
#   1. 不带 dedup_column：INSERT 包含 dedup_hash，ON CONFLICT (dedup_hash)
#   2. 带 Meta.dedup_column="xxx"：INSERT 不含 dedup_hash，ON CONFLICT (xxx)
#   3. 重复插入同一条返回 0 行，去重生效
# 测试细节以现有 test_persist.py 风格为准（用 test_db fixture + migrate()）。
```

- [ ] **Step 6: 验证 Message 加入后 migration plan 对它返回零 DDL**

```bash
cd apps/agent-service && uv run python -c "
from app.runtime.migrator import plan_migration
from app.runtime.data import DATA_REGISTRY
import app.domain  # register Message
# Pretend conversation_messages exists with any columns — adoption mode ignores them
existing = {'conversation_messages': {'dummy': 'text'}}
plan = plan_migration(list(DATA_REGISTRY), existing)
msg_stmts = [s.sql for s in plan.stmts if 'conversation_messages' in s.sql]
print('Message DDL statements:', len(msg_stmts))
assert len(msg_stmts) == 0, f'Expected 0 DDL for conversation_messages (adoption mode), got: {msg_stmts}'
print('OK: Message is in adoption mode')
"
```

- [ ] **Step 7: 测试通过**

```bash
cd apps/agent-service && uv run pytest tests/domain/test_message.py tests/runtime/test_persist.py -v
```

- [ ] **Step 8: Commit**

```bash
git commit -m "feat(domain+runtime): Message Data (adopts conversation_messages) + persist supports Meta.dedup_column"
```

---

### Task 1.2: Fragment Data

**Files:**
- Create: `apps/agent-service/app/domain/fragment.py`
- Test: `apps/agent-service/tests/domain/test_fragment.py`

Fragment 是 vectorize 的产出——一个已算好向量的段落。**不需要 pg 表**（业务代码从不查），但需要是 Data 类以便流经 wire。Fragment 走 `.broadcast()` 发给 VectorStore Sink；不落 pg。

> **字段说明（2026-04-23 paper-read 修订）**：recall 和 cluster 两个 collection 的 payload 不同（见 T1.3 warning #6），Fragment 要拆成 `recall_payload` + `cluster_payload` 两个字段，不能合并。`fragment_id` 存 UUID5 字符串（见 T1.3 warning #5），由 vectorize @node 从 `message_id` 派生。

- [ ] **Step 1: 写失败测试**

```python
# tests/domain/test_fragment.py
from app.domain.fragment import Fragment
from app.runtime.data import key_fields

def test_fragment_key():
    assert key_fields(Fragment) == ("fragment_id",)

def test_fragment_transient():
    assert getattr(Fragment.Meta, "transient", False) is True

def test_fragment_instance():
    f = Fragment(
        fragment_id="c9d05a5e-...",  # UUID5 from message_id
        message_id="m1",
        chat_id="c1",
        dense=[0.0]*1024,
        sparse={"indices": [1], "values": [0.5]},
        dense_cluster=[0.0]*1024,
        recall_payload={"message_id": "m1", "user_id": "u1", "chat_id": "c1",
                        "timestamp": 1, "root_message_id": "r1", "original_text": "hi"},
        cluster_payload={"message_id": "m1", "user_id": "u1", "chat_id": "c1",
                         "timestamp": 1},
    )
    assert f.message_id == "m1"
    assert "original_text" in f.recall_payload
    assert "original_text" not in f.cluster_payload
```

- [ ] **Step 2: 实现 fragment.py**

```python
# app/domain/fragment.py
from typing import Annotated

from app.runtime.data import Data, Key


class Fragment(Data):
    fragment_id: Annotated[str, Key]  # UUID5(NAMESPACE_DNS, message_id), str form
    message_id: str
    chat_id: str
    dense: list[float]
    sparse: dict  # {"indices": [...], "values": [...]}
    dense_cluster: list[float]
    recall_payload: dict   # full payload for messages_recall collection
    cluster_payload: dict  # reduced payload for messages_cluster collection

    class Meta:
        transient = True  # not persisted to pg; goes straight to VectorStore
```

- [ ] **Step 3: migrator.py 跳过 `Meta.transient=True` 的类（F 扩展）**

> **分层提示**：这步改动的是 F 层（`app/runtime/migrator.py`），为 F 层**添加**"支持 transient Data 变体"的能力。它本身不 import 任何业务代码，不违反 Phase 0 原则；只是时序上放在 T1.2 里是因为 Fragment 是第一个 transient 需求方。测试应新增到 `tests/runtime/test_migrator.py`（**不是** `tests/domain/`）：用一个临时 `class Tmp(Data): class Meta: transient = True` 验证 `plan_migration` 跳过它。

```python
# migrator.py, plan_migration:
if getattr(getattr(cls, "Meta", None), "transient", False):
    continue
```

- [ ] **Step 4: 测试通过**（`tests/domain/test_fragment.py` + `tests/runtime/test_migrator.py` 新增的 transient 用例）

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(domain+runtime): Fragment (transient) + migrator skips transient Data"
```

---

### Task 1.2.5: Message Bridge（`emit_legacy_message`）

> **挪位说明**：这个 task 原为 Phase 0 的 T0.17，按"依赖方向"原则重归类到 Phase 1 Tier B。它依赖 T1.1 的 `Message` Data 类，所以必须在 T1.1 之后。文件从原计划的 `app/runtime/legacy_bridge.py` 改为 `app/bridges/message_bridge.py`，让 `app/runtime/` 保持 business-agnostic。

**Files:**
- Create: `apps/agent-service/app/bridges/__init__.py`
- Create: `apps/agent-service/app/bridges/message_bridge.py`
- Test: `apps/agent-service/tests/bridges/test_message_bridge.py`

Message Bridge 只是一个极薄的函数：老代码在写完 `conversation_messages` 后调用它，从行对象构造 `Message(Data)` 并 `await emit(msg)`。Phase 5 整体删除这个文件。

- [ ] **Step 1: 写失败测试**

```python
# tests/bridges/test_message_bridge.py
import pytest
from unittest.mock import AsyncMock, patch
from app.data.models import ConversationMessage
from app.bridges.message_bridge import emit_legacy_message

@pytest.mark.asyncio
async def test_emit_legacy_message_lifts_and_emits():
    cm = ConversationMessage(
        message_id="m1", chat_id="c1", content="hi", role="user",
        create_time=1234567890, vector_status="pending",
    )
    with patch("app.bridges.message_bridge.emit", new_callable=AsyncMock) as m:
        await emit_legacy_message(cm)
    m.assert_awaited_once()
    msg = m.call_args.args[0]
    from app.domain.message import Message
    assert isinstance(msg, Message)
    assert msg.message_id == "m1"
    assert msg.chat_id == "c1"
```

- [ ] **Step 2: 实现 `app/bridges/__init__.py`**

```python
# app/bridges/__init__.py
"""Business glue: lift legacy ORM rows into Domain Data and emit().

Bridges are Phase-bound — they exist only while a legacy pipeline is being
migrated. After Phase 5 (full chat migration), this entire package is deleted.
"""
```

- [ ] **Step 3: 实现 `app/bridges/message_bridge.py`**

```python
# app/bridges/message_bridge.py
"""Message Bridge: lift legacy ConversationMessage rows into new Message Data.

Exists during Phases 1-4. After Phase 5 the call sites are deleted, and so is
this file.
"""
from app.domain.message import Message
from app.runtime.emit import emit


async def emit_legacy_message(cm) -> None:  # cm: ConversationMessage
    msg = Message(
        message_id=cm.message_id,
        generation=0,
        chat_id=cm.chat_id,
        persona_id=getattr(cm, "persona_id", "") or "",
        role=cm.role,
        text=cm.content or "",
        images=getattr(cm, "images", []) or [],
        create_time=cm.create_time,
    )
    await emit(msg)
```

**注意**：`Message` 已在 T1.1 就位，这里直接 top-level import（不再需要 forward-ref 绕圈）。不需要 xfail。

- [ ] **Step 4: 测试通过**

```bash
cd apps/agent-service && uv run pytest tests/bridges/test_message_bridge.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(bridges): message_bridge — emit_legacy_message(ConversationMessage) -> Message(Data)"
```

---

### Task 1.3: vectorize Node

**Files:**
- Create: `apps/agent-service/app/nodes/__init__.py`
- Create: `apps/agent-service/app/nodes/vectorize.py`
- Test: `apps/agent-service/tests/nodes/test_vectorize.py`

> **⚠️ 重要（2026-04-23 第二轮 paper-read）：plan 代码块是指示性，不 authoritative。**
> 以 `apps/agent-service/app/workers/vectorize.py::vectorize_message` 的真实实现为准。以下
> 是 plan 伪代码和真实代码的 10 处偏差，全部要以真实代码为准：
>
> **业务语义（不能省）：**
> 1. **输入是 `ConversationMessage` 不是 Message**：plan 里 Message 字段 `text` / `images`
>    对应真实字段 `content` / `image_keys`（经 `parse_content(content)` 返回
>    `ParsedContent.image_keys` 和 `.render()`）。T1.1 Message Data 的字段命名要向
>    真实列看齐，或者在 @node 里做字段 rename（推荐前者，避免 @node 里还要映射）。
> 2. **Empty content 两处 early-return**：
>    - 入口处：`text_content` 和 `image_keys` 都空 → 跳过（返回 False / 不产 Fragment）。
>    - 下载后：`text_content` 空且 `image_base64_list` 全失败 → 跳过。
>    @node 的返回用 `Optional[Fragment]` + runtime drop None；若 runtime 尚未实现 drop-None
>    语义，则在 @node 里不 emit（直接 return 但状态写回 `skipped`）。
> 3. **图片下载前 permission check**：`find_group_download_permission(session, chat_id)`
>    查 `"only_owner"`，是就把 `image_keys` 清空。这个 pg 查询必须保留；省了会违规下载。
> 4. **两份 instruction**：`InstructionBuilder.for_corpus(modality)` 给 hybrid 用，
>    `InstructionBuilder.for_cluster(...)` 给 dense 用。**不是同一份**。
> 5. **vector_id 是 UUID5**：`uuid.uuid5(uuid.NAMESPACE_DNS, message_id)`。Fragment 的
>    `fragment_id` 字段类型应保留 str，但值用 UUID5 stringify。抽到 `app/nodes/_ids.py`
>    共享给其他 @node 用（spec 里规划了 memory 流也要 vectorize）。
> 6. **payload 字段**：recall 和 cluster **payload 不同**。recall 要 `message_id / user_id /
>    chat_id / timestamp / root_message_id / original_text`；cluster 不要 `root_message_id`
>    和 `original_text`。Fragment Data 要容纳这两套数据（加 `recall_payload` + `cluster_payload`
>    两个 dict 字段，或用一个 dict 字段让 save_fragment @node 筛选）。
>
> **依赖/接口（按实际代码签名）：**
> 7. **Image 下载返回 base64 字符串**：`image_client.download_image_as_base64(key, msg_id,
>    persona_id) -> str`。plan 里的 `download_image(url) -> bytes` 不存在；image key 不是 URL。
>    迁 @node 时 `asyncio.gather(*tasks, return_exceptions=True)` 然后 `[r for r in results
>    if isinstance(r, str) and r]` 过滤失败。
> 8. **EmbedderClient 签名**：`embedder.hybrid(*, text, image_base64_list, instructions) ->
>    HybridEmbedding` 和 `.dense(*, text, image_base64_list, instructions) -> list[float]`。
>    **没有 `encode(mode=...)` 方法**。model_id 固定 `"embedding-model"`。
> 9. **HybridEmbedding 对象不是 dict**：字段 `.dense: list[float]` / `.sparse.indices:
>    list[int]` / `.sparse.values: list[float]`。
> 10. **cluster collection 需要 dense-only upsert**：旧代码用 `qdrant.upsert_vectors`（不是
>     `upsert_hybrid_vectors`）。**当前 T0.13 的 `VectorStore.upsert` 只支持
>     `HybridEmbedding`**，cluster 写入需要在 T1.4 顺手给 VectorStore 加
>     `upsert_dense(point_id, dense, payload)` 方法（F 扩展，测试放 `tests/capabilities/`）。
>
> **运行时/架构建议：**
> - **Dual-collection upsert** 放 T1.4 的 `save_fragment` @node 里，用 `asyncio.gather`
>   并发。不要拆两个 @node（拆了之后 durable retry 只会重试失败那一侧，语义反而变复杂）。
> - **vector_status 写回** 在 vectorize @node 末尾直接
>   `UPDATE conversation_messages SET vector_status=...`，不拆独立节点。状态机保持
>   `pending / completed / skipped / failed` 四态。

- [ ] **Step 1: 读旧 vectorize 实现（真实行为来源）**

```bash
sed -n '1,170p' apps/agent-service/app/workers/vectorize.py
```

通读完再动。**以真实代码为准，上方 warning 列的 10 项偏差全部按真实代码处理。**

- [ ] **Step 2: 提取 UUID 派生 helper**

```python
# app/nodes/_ids.py
"""Shared ID helpers for @node functions."""
import uuid

def vector_id_for(message_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, message_id))
```

（测试放 `tests/nodes/test_ids.py`，非常轻。）

- [ ] **Step 3: 写失败测试（mock EmbedderClient 的 dense/hybrid + image_client.download_image_as_base64 + permission check）**

骨架如下，**具体字段按 T1.1 Message 实际定义 + 上方 warning #1 字段映射对齐**：

```python
# tests/nodes/test_vectorize.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.agent.embedding import HybridEmbedding, SparseEmbedding
from app.domain.message import Message
from app.nodes.vectorize import vectorize

def _hybrid(dense_val: float = 0.1) -> HybridEmbedding:
    return HybridEmbedding(
        dense=[dense_val] * 1024,
        sparse=SparseEmbedding(indices=[1], values=[0.5]),
    )

@pytest.mark.asyncio
async def test_vectorize_produces_fragment_text_only():
    m = Message(...)  # T1.1 实际字段；text/images 若被 T1.1 重命名为 content/image_keys 则按那个
    with patch("app.nodes.vectorize.embedder.hybrid", new_callable=AsyncMock, return_value=_hybrid()), \
         patch("app.nodes.vectorize.embedder.dense", new_callable=AsyncMock, return_value=[0.2]*1024):
        frag = await vectorize(m)
    assert frag is not None
    assert len(frag.dense) == 1024
    assert "user_id" in frag.recall_payload   # warning #6

@pytest.mark.asyncio
async def test_vectorize_empty_content_returns_none():
    m = Message(...)  # text 空 + images 空
    with patch("app.nodes.vectorize.embedder.hybrid", new_callable=AsyncMock) as h:
        frag = await vectorize(m)
    assert frag is None
    h.assert_not_awaited()

@pytest.mark.asyncio
async def test_vectorize_with_image_permission_blocked():
    m = Message(...)  # images 非空 + chat only_owner 场景
    with patch("app.nodes.vectorize.find_group_download_permission",
               new_callable=AsyncMock, return_value="only_owner"), \
         patch("app.nodes.vectorize.image_client.download_image_as_base64",
               new_callable=AsyncMock) as dl, \
         patch("app.nodes.vectorize.embedder.hybrid", new_callable=AsyncMock, return_value=_hybrid()), \
         patch("app.nodes.vectorize.embedder.dense", new_callable=AsyncMock, return_value=[0.2]*1024):
        frag = await vectorize(m)
    dl.assert_not_awaited()
    assert frag is not None  # 有 text 的话继续，没 text 的话 None
```

- [ ] **Step 4: 实现 vectorize.py（遵循真实代码 + 上方 warning）**

```python
# app/nodes/vectorize.py
import asyncio
import logging

from app.agent.embedding import HybridEmbedding
from app.capabilities.embed import EmbedderClient
from app.domain.fragment import Fragment
from app.domain.message import Message
from app.nodes._ids import vector_id_for
from app.runtime.node import node

logger = logging.getLogger(__name__)

embedder = EmbedderClient(model_id="embedding-model")

# parse_content / InstructionBuilder / find_group_download_permission / image_client
# 暂时从旧位置 import。Phase 1 尾部 T1.10 把 helpers 挪到 app/nodes/_helpers.py。
from app.agent.content_parser import parse_content  # type: ignore
from app.agent.instructions import InstructionBuilder  # type: ignore
from app.data.session import get_session  # type: ignore
from app.db.group_permissions import find_group_download_permission  # type: ignore
from app.infra.image_client import image_client  # type: ignore


@node
async def vectorize(msg: Message) -> Fragment | None:
    """Lift ConversationMessage → Fragment. Returns None when skip applies.

    Preserves 10 behavioral invariants from legacy vectorize_message — see plan
    T1.3 warning. Summary: parse content, permission-check images, download
    base64, run hybrid + cluster embeddings in parallel, pack dual payloads.
    """
    # TODO: 真实字段名以 T1.1 Message 为准（content vs text / image_keys vs images）
    parsed = parse_content(msg.content)
    text_content = parsed.render()
    image_keys = parsed.image_keys

    if not text_content and not image_keys:
        logger.info("vectorize: message=%s empty, skip", msg.message_id)
        return None

    if image_keys:
        async with get_session() as s:
            perm = await find_group_download_permission(s, msg.chat_id)
        if perm == "only_owner":
            image_keys = []

    image_base64_list: list[str] = []
    if image_keys:
        tasks = [
            image_client.download_image_as_base64(key, msg.message_id, "chiwei")
            for key in image_keys
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        image_base64_list = [r for r in results if isinstance(r, str) and r]

    if not text_content and not image_base64_list:
        logger.info("vectorize: message=%s text+images all empty/failed, skip", msg.message_id)
        return None

    modality = InstructionBuilder.detect_input_modality(text_content, image_base64_list)
    corpus_instructions = InstructionBuilder.for_corpus(modality)
    cluster_instructions = InstructionBuilder.for_cluster(
        target_modality=modality,
        instruction="Retrieve semantically similar content",
    )

    hybrid_emb, cluster_dense = await asyncio.gather(
        embedder.hybrid(
            text=text_content or None,
            image_base64_list=image_base64_list or None,
            instructions=corpus_instructions,
        ),
        embedder.dense(
            text=text_content or None,
            image_base64_list=image_base64_list or None,
            instructions=cluster_instructions,
        ),
    )

    vector_id = vector_id_for(msg.message_id)
    return Fragment(
        fragment_id=vector_id,
        message_id=msg.message_id,
        chat_id=msg.chat_id,
        dense=hybrid_emb.dense,
        sparse={"indices": hybrid_emb.sparse.indices, "values": hybrid_emb.sparse.values},
        dense_cluster=cluster_dense,
        recall_payload={
            "message_id": msg.message_id,
            "user_id": getattr(msg, "user_id", ""),
            "chat_id": msg.chat_id,
            "timestamp": msg.create_time,
            "root_message_id": getattr(msg, "root_message_id", ""),
            "original_text": text_content,
        },
        cluster_payload={
            "message_id": msg.message_id,
            "user_id": getattr(msg, "user_id", ""),
            "chat_id": msg.chat_id,
            "timestamp": msg.create_time,
        },
    )
```

**注意**：
- `Fragment` 的字段 `recall_payload` / `cluster_payload` 要在 T1.2 Fragment 里加上（T1.2 pending，本 task 合入或先在 T1.2 补齐）。原 plan T1.2 只有一个 `payload: dict`，按 warning #6 要拆。
- `vector_status` 写回在**下一个 @node**（一次 emit 一次 UPDATE）还是**本 @node 末尾**？建议本 @node 末尾，原因见 warning 尾部"运行时/架构建议"。如果本 task 不加写回，单独在 T1.4 结束时做。

- [ ] **Step 5: 测试通过**

```bash
cd apps/agent-service && uv run pytest tests/nodes/test_vectorize.py tests/nodes/test_ids.py -v
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(nodes): vectorize @node (Message -> Fragment via EmbedderClient, dual-payload)"
```

---

### Task 1.4: save_fragment Node（写 qdrant）+ VectorStore dense-only F 扩展

> **⚠️ API gap（2026-04-23 paper-read 发现）**：
> 1. `VectorStore.upsert` 当前签名是 `upsert(point_id, embedding: HybridEmbedding, payload)`，**只支持 hybrid**。cluster collection 需要纯 dense upsert（旧代码用 `qdrant.upsert_vectors`，不是 `upsert_hybrid_vectors`）。
> 2. 本 task 顺手给 `VectorStore` 加 `upsert_dense(point_id, dense: list[float], payload)` 方法（F 扩展，测试放 `tests/capabilities/test_vector_store.py`）。
> 3. hybrid 调用侧要把 Fragment 的 `dense` + `sparse dict` 重新包成 `HybridEmbedding` 对象（`VectorStore.upsert` 只收对象不收 dict）。

**Files:**
- Modify: `apps/agent-service/app/capabilities/vector_store.py`（加 `upsert_dense`）
- Modify: `apps/agent-service/tests/capabilities/test_vector_store.py`（新增 dense-only 用例）
- Create: `apps/agent-service/app/nodes/save_fragment.py`
- Test: `apps/agent-service/tests/nodes/test_save_fragment.py`

- [ ] **Step 1: 给 `VectorStore` 加 `upsert_dense`（F 扩展）**

```python
# app/capabilities/vector_store.py  (append)
async def upsert_dense(
    self,
    point_id: str,
    dense: list[float],
    payload: dict[str, Any],
) -> bool:
    return await qdrant.upsert_vectors(
        collection=self._collection,
        vectors=[dense],
        ids=[point_id],
        payloads=[payload],
    )
```

测试：mock `qdrant.upsert_vectors` 验证它被 `await` 一次、参数正确。

- [ ] **Step 2: 写 save_fragment 失败测试**

```python
# tests/nodes/test_save_fragment.py
import pytest
from unittest.mock import AsyncMock, patch

from app.domain.fragment import Fragment
from app.nodes.save_fragment import save_fragment


@pytest.mark.asyncio
async def test_save_fragment_upserts_both_collections_with_correct_payloads():
    f = Fragment(
        fragment_id="c9d05a5e-...",  # UUID5 str
        message_id="m1",
        chat_id="c1",
        dense=[0.1] * 1024,
        sparse={"indices": [1], "values": [0.5]},
        dense_cluster=[0.2] * 1024,
        recall_payload={
            "message_id": "m1", "user_id": "u1", "chat_id": "c1",
            "timestamp": 1, "root_message_id": "r1", "original_text": "hi",
        },
        cluster_payload={
            "message_id": "m1", "user_id": "u1", "chat_id": "c1", "timestamp": 1,
        },
    )
    with patch("app.nodes.save_fragment.recall_store.upsert", new_callable=AsyncMock) as r, \
         patch("app.nodes.save_fragment.cluster_store.upsert_dense", new_callable=AsyncMock) as c:
        await save_fragment(f)

    r.assert_awaited_once()
    # recall 用 hybrid upsert，第二参是 HybridEmbedding 对象
    _, hyb, payload_r = r.await_args.args
    assert hyb.dense == f.dense
    assert hyb.sparse.indices == [1]
    assert "original_text" in payload_r

    c.assert_awaited_once_with(f.fragment_id, f.dense_cluster, f.cluster_payload)
```

- [ ] **Step 3: 实现 save_fragment.py**

```python
# app/nodes/save_fragment.py
"""Persist a Fragment into qdrant: hybrid -> messages_recall, dense -> messages_cluster.

Both upserts run concurrently. Partial failure raises (nack + retry at durable
boundary) so we never have half-populated fragments across collections — the
failed side will be retried, the other side simply re-upserts (qdrant upsert
is idempotent per point_id).
"""
from __future__ import annotations

import asyncio

from app.agent.embedding import HybridEmbedding, SparseEmbedding
from app.capabilities.vector_store import VectorStore
from app.domain.fragment import Fragment
from app.runtime.node import node

recall_store = VectorStore("messages_recall")
cluster_store = VectorStore("messages_cluster")


@node
async def save_fragment(frag: Fragment) -> None:
    hybrid = HybridEmbedding(
        dense=frag.dense,
        sparse=SparseEmbedding(
            indices=frag.sparse["indices"],
            values=frag.sparse["values"],
        ),
    )
    await asyncio.gather(
        recall_store.upsert(frag.fragment_id, hybrid, frag.recall_payload),
        cluster_store.upsert_dense(frag.fragment_id, frag.dense_cluster, frag.cluster_payload),
    )
```

- [ ] **Step 4: 测试通过**

```bash
cd apps/agent-service && uv run pytest \
    tests/capabilities/test_vector_store.py \
    tests/nodes/test_save_fragment.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(nodes+capabilities): save_fragment @node + VectorStore.upsert_dense"
```

---

### Task 1.4.5: engine.py MQSource `_source_loop_mq`（F 层扩展，2026-04-24 加）

> **背景（为何 post-hoc 加这个任务）**：T1.7 paper-read 后发现 agent-service 内只有 `proactive.py:128` 一处写 `conversation_messages`，飞书 chat 主链路的 CM 写入在 **lark-server (TypeScript)**，Python Bridge 够不到。要做 Phase 1 的闭环（T1.10 删老 worker），必须让 agent-service 从老 `vectorize` 队列**消费**，而不是指望外部写入点全部迁移到 Bridge。
>
> `Source.mq(queue)` DSL 早在 T0 就有（`app/runtime/source.py`），但 `engine.py` 只实现了 cron/interval，MQ 是缺的。本任务补齐。

**Files:**
- Modify: `apps/agent-service/app/runtime/engine.py` — 加 `_source_loop_mq`
- Test: `apps/agent-service/tests/runtime/test_source_mq.py`

**协议约定（与老 lark-server publisher 对齐）**：

- 队列 body 是 JSON：`{"message_id": "<id>"}`（老 `handle_vectorize` 消费契约，见 `app/workers/vectorize.py::handle_vectorize`）
- MQSource 的 wire target **必须是一个 @node**，signature 为 `(req: XxxRequest) -> Xxx`，其中 `XxxRequest` 是 1 字段 Data 类（后面 T1.5 会定 `MessageRequest(message_id: str)`）
- engine 把 decoded JSON 直接传给 `XxxRequest(**body)` 构造，失败（ValidationError）→ nack + DLQ 日志

**行为约束：**

1. 订阅时 `prefetch_count = 10`（旧 worker 的 semaphore(10) 迁移到 broker 层背压，不在 @node 层加）
2. **被动获取队列**：`channel.get_queue(name)` —— 队列 topology 由 publisher（lark-server）或 `app/infra/rabbitmq.py::declare_topology` 持有。**不要**自己 `declare_queue(durable=True)`：lane/prod 队列带 DLX + TTL + x-expires 等 args（见 `_build_queue_args`），用不一致的 args 重新声明会 `PRECONDITION_FAILED`。和 `mq.consume()` 内部的做法一致
3. 每条消息 `process(requeue=False)`（与 `app/runtime/durable.py` 和老 `handle_vectorize` 一致；失败消息走 DLX 由 infra 层的 delayed-retry / DLX 机制处理，避免 poison loop）
4. Graceful shutdown：engine stop 时取消消费,等待正在处理的 callbacks 完成再断开 connection
5. Trace/lane context：读 aio-pika headers 里的 `x-trace-id` / `x-lane` 注入 context（复用 T0.11 durable 边的 header 协议）
6. 没有 lane 概念时走 `current_lane()` 默认值

- [ ] **Step 1: 读 T0.11 durable 边 consumer 实现作为模板**

```bash
grep -n "async def\|lane_queue\|prefetch_count\|process(requeue" apps/agent-service/app/runtime/durable.py
sed -n '1,50p' apps/agent-service/app/runtime/durable.py
```

T0.11 durable consumer 已经实现了 "RabbitMQ 消费 + trace/lane context + ack/nack" 的模式，MQSource 直接参照，但两点不同：
- durable consumer 消费的是 wire 自动声明的 `data_<cls>_v<N>` 队列；MQSource 消费**外部指定**队列（`queue` 参数，如 `"vectorize"`）
- durable consumer decode 成 Data class；MQSource decode 成 `req_cls(**json)`（`req_cls` 由 wire target @node 签名反射得到）

- [ ] **Step 2: 写失败测试（testcontainers rabbitmq）**

```python
# tests/runtime/test_source_mq.py
import asyncio
import json
import pytest
from app.runtime.data import Data, Key
from app.runtime.node import node
from app.runtime.wire import wire, clear_wiring
from app.runtime.source import Source
from app.runtime.engine import Runtime
from typing import Annotated


class _Req(Data):
    message_id: Annotated[str, Key]
    class Meta:
        transient = True


@pytest.mark.asyncio
async def test_mq_source_consumes_and_invokes_node(rabbitmq, _queue_pub):
    received = []

    @node
    async def ingest(req: _Req) -> None:
        received.append(req.message_id)

    clear_wiring()
    wire(_Req).to(ingest).from_(Source.mq("test_q"))
    # (from_source is the wiring DSL for binding a Source to a wire target.
    # If T0.15 didn't add it, this task may need to extend wire.py too — verify first.)

    rt = Runtime()
    task = asyncio.create_task(rt.run())
    try:
        await _queue_pub("test_q", json.dumps({"message_id": "m1"}))
        await _queue_pub("test_q", json.dumps({"message_id": "m2"}))
        # give engine a beat to dispatch
        for _ in range(50):
            if len(received) >= 2:
                break
            await asyncio.sleep(0.1)
        assert sorted(received) == ["m1", "m2"]
    finally:
        rt.stop()
        await task


@pytest.mark.asyncio
async def test_mq_source_decode_failure_does_not_crash_loop(rabbitmq, _queue_pub, caplog):
    @node
    async def ingest(req: _Req) -> None: ...

    clear_wiring()
    wire(_Req).to(ingest).from_(Source.mq("test_q_bad"))

    rt = Runtime()
    task = asyncio.create_task(rt.run())
    try:
        await _queue_pub("test_q_bad", "not json")
        await _queue_pub("test_q_bad", json.dumps({"message_id": "m_ok"}))
        await asyncio.sleep(1.0)
        # bad message nack'd; good message processed
    finally:
        rt.stop()
        await task
```

（`_queue_pub` 是需要加的 fixture：给定 rabbitmq url + queue name + body，publish 一条。放 `tests/runtime/conftest.py`。）

> **先验证：T0.15 wire DSL 有没有 `.from_()`？** 没有的话这步要先扩 wire.py。找旧代码里 Source 怎么绑定的 —— T0.15/T0.16 的 HTTP source 注册机制是 `register_http_sources(app)` 用 `WIRING_REGISTRY` 查出带 HTTP source 的 wire，说明 wire 里应该已经能登记 source。如果只支持 http/cron/interval 不支持 mq，先把 `from_source` 或等价方法扩到支持 mq kind。

- [ ] **Step 3: 实现 `_source_loop_mq`**

核心骨架（伪代码，真 API 以 aio-pika + 已有 durable.py 为准）：

```python
async def _source_loop_mq(self, w: WireSpec, src: SourceSpec) -> None:
    from app.infra.rabbitmq import mq, current_lane, lane_queue
    from app.runtime.node import inputs_of

    target = w.consumers[0]  # MQ source 对应的 wire 必须只有 1 consumer（反射期校验）
    ins = inputs_of(target)
    assert len(ins) == 1, f"MQ source target {target.__name__} must take exactly 1 Data arg"
    (_, req_cls), = ins.items()

    queue_name = src.params["queue"]
    # 与老 handle_vectorize 行为对齐：lane_queue 适配（每个 lane 独立队列）
    actual_queue = lane_queue(queue_name)

    await mq.connect()
    channel = await mq.channel()
    await channel.set_qos(prefetch_count=10)
    queue = await channel.declare_queue(actual_queue, durable=True)

    async with queue.iterator() as qit:
        async for incoming in qit:
            async with incoming.process(requeue=False):
                # inject trace/lane context (见 durable.py)
                try:
                    body = json.loads(incoming.body.decode())
                    req = req_cls(**body)
                except (json.JSONDecodeError, ValidationError) as e:
                    logger.warning("MQSource %s decode failed: %s body=%r", queue_name, e, incoming.body[:200])
                    # 不 raise；`process(requeue=False)` 把这条 ack 掉（不会 requeue）
                    continue
                await target(req)
```

**细节**：
- `process(requeue=False)`：decode 失败**不 raise**(log + continue,消息被 ack 丢弃,避免死循环);业务失败(target 抛异常)走 DLX,由 `app/infra/rabbitmq.py` 的 delayed-retry + DLX 机制处理重试。和 `durable.py` 行为完全对齐。
- prefetch_count=10 对应老 semaphore。
- 如果 queue 不存在，`declare_queue(durable=True)` 会自动创建；这跟 lark-server 的 publish 端声明一致不会冲突。
- 注意现有 `lane_queue` 的语义：如果 `current_lane()` 是 prod 返回 `queue_name`，其他 lane 返回 `queue_name_<lane>`。和 durable 边的队列命名保持一致。

- [ ] **Step 4: 在 `Runtime.run()` dispatcher 里加 `kind == "mq"` 分支**

```bash
grep -n "src.kind ==" apps/agent-service/app/runtime/engine.py
```

当前 dispatcher 在 `cron` / `interval` 之间 if/elif。加 `elif src.kind == "mq": task = asyncio.create_task(self._source_loop_mq(w, src))`。

- [ ] **Step 5: 测试通过**

```bash
cd apps/agent-service && uv run pytest tests/runtime/test_source_mq.py -v
```

- [ ] **Step 6: 跑全量回归**

```bash
cd apps/agent-service && uv run pytest
```

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(runtime): MQSource engine loop — consume RabbitMQ queues into @node"
```

---

### Task 1.5: wiring/memory.py + deployment.py + runtime_entry 激活

> **改动说明（2026-04-24 闭环改造）**：
> 1. 原 plan 用 `from app.runtime.placement import _BINDINGS` 读私有成员。T0.15 code-review 修复已加 `iter_bindings()` 公开 API，测试改用公开接口。
> 2. 补一步关键步骤：更新 `app/workers/runtime_entry.py` 追加 `import app.wiring; import app.deployment`，否则 T1.6 改完 PaaS command 部署，Runtime 启动不会加载任何 wire/binding。T0.15 完成时 runtime_entry 是 stub，此处才真正"激活"。
> 3. **闭环入口扩充（2026-04-24）**：原 plan 只 wire `Message -> vectorize.durable()`，默认假设所有 CM 写入点都能调 Bridge。T1.7 发现 chat 主链路 CM 写入在 lark-server (TS)，够不到 Python Bridge。引入**两条入口**：
>    - `proactive.py` 走 Bridge（T1.2.5 已实现）→ emit Message → durable 边 → vectorize
>    - lark-server 继续 publish 老 `vectorize` 队列 → MQSource 消费 → `hydrate_message` @node → Message → durable 边 → vectorize
>    两路在 Message durable 边汇合，然后走同一个 vectorize @node。lark-server 一行 TS 都不用改。

**Files:**
- Create: `apps/agent-service/app/domain/message_request.py` — `MessageRequest` Data（MQ 入口的 1 字段请求类型）
- Create: `apps/agent-service/app/nodes/hydrate_message.py` — `hydrate_message(req: MessageRequest) -> Message | None`
- Create: `apps/agent-service/app/wiring/__init__.py`
- Create: `apps/agent-service/app/wiring/memory.py`
- Create: `apps/agent-service/app/deployment.py`
- Modify: `apps/agent-service/app/workers/runtime_entry.py`（追加 wiring + deployment import）
- Modify: `apps/agent-service/app/domain/__init__.py`（export MessageRequest）
- Modify: `apps/agent-service/app/nodes/__init__.py`（export hydrate_message）
- Test: `apps/agent-service/tests/domain/test_message_request.py`
- Test: `apps/agent-service/tests/nodes/test_hydrate_message.py`
- Test: `apps/agent-service/tests/wiring/test_memory.py`

**图拓扑（最终形态）**：

```
    Source.mq("vectorize")         (lark-server publish path)
              │ {"message_id": X}
              ▼
     hydrate_message(req)          fetch CM from pg, construct Message
              │                    returns None if not found → drop
              ▼
          Message ───────┐
                         │   （也可以从 Bridge 进入：proactive.py → emit_legacy_message）
                         │
                   durable 边（RabbitMQ "message" 队列）
                         │
                         ▼
                      vectorize(msg)
                         │
                      Fragment | None
                         │
                         ▼
                   save_fragment(frag)   (transient; in-process)
                         │
                         ▼
                   qdrant upsert
```

- [ ] **Step 1: `MessageRequest` Data 类**

```python
# app/domain/message_request.py
"""MessageRequest: MQ 入口的请求体 Data。

`Source.mq("vectorize")` 消费老队列 body `{"message_id": X}`，engine
层 decode 成 `MessageRequest(message_id=X)` 交给 `hydrate_message` @node。
"""
from __future__ import annotations
from typing import Annotated
from app.runtime.data import Data, Key


class MessageRequest(Data):
    message_id: Annotated[str, Key]

    class Meta:
        transient = True  # 不落 pg
```

测试（`tests/domain/test_message_request.py`）：确认 transient + Key 元数据生效即可，~5 行。

- [ ] **Step 2: `hydrate_message` @node**

```python
# app/nodes/hydrate_message.py
"""Fetch a ConversationMessage by id, lift to Message Data.

MQ 入口 @node：消费 MessageRequest（从 Source.mq 来），查 pg，
构造 Message 交给下游 vectorize。行为和 T1.2.5 `emit_legacy_message`
一致（字段 1:1 pass-through），只是触发源不同。
"""
from __future__ import annotations
import logging
from app.data.queries import find_message_by_id
from app.data.session import get_session
from app.domain.message import Message
from app.domain.message_request import MessageRequest
from app.runtime.node import node

logger = logging.getLogger(__name__)


@node
async def hydrate_message(req: MessageRequest) -> Message | None:
    async with get_session() as s:
        cm = await find_message_by_id(s, req.message_id)
    if cm is None:
        logger.warning("hydrate_message: message_id=%s not found, drop", req.message_id)
        return None
    return Message(
        message_id=cm.message_id,
        user_id=cm.user_id,
        content=cm.content,
        role=cm.role,
        root_message_id=cm.root_message_id,
        reply_message_id=cm.reply_message_id,
        chat_id=cm.chat_id,
        chat_type=cm.chat_type,
        create_time=cm.create_time,
        message_type=cm.message_type,
        vector_status=cm.vector_status,
        bot_name=cm.bot_name,
        response_id=cm.response_id,
    )
```

> **提醒**：字段映射和 `app/bridges/message_bridge.py::emit_legacy_message` 完全一样。DRY 考虑可以抽一个 `Message.from_cm(cm)` 类方法（放 `app/domain/message.py`），本 @node 和 bridge 都复用。取决于 implementer 判断是否现在做抽象。

测试（`tests/nodes/test_hydrate_message.py`）：
- `test_hydrates_existing_message`：mock `find_message_by_id` 返回假 CM → 断言 Message 字段
- `test_missing_message_returns_none`：mock 返回 None → 断言 None

- [ ] **Step 3: `wiring/memory.py`**

```python
# app/wiring/memory.py
"""Message-pipeline wiring: MQ entry → hydrate → vectorize → save.

Two entry points, one pipeline:
- Source.mq("vectorize") feeds hydrate_message (lark-server publisher path)
- emit_legacy_message() inside proactive.py feeds the Message durable edge directly (Bridge path)

Both converge on the Message durable wire, then vectorize → Fragment → save_fragment.
"""
from app.runtime.wire import wire
from app.runtime.source import Source
from app.domain.message import Message
from app.domain.message_request import MessageRequest
from app.domain.fragment import Fragment
from app.nodes.hydrate_message import hydrate_message
from app.nodes.vectorize import vectorize
from app.nodes.save_fragment import save_fragment

# MQ entry: lark-server publishes {"message_id": X} to "vectorize" queue
wire(MessageRequest).to(hydrate_message).from_(Source.mq("vectorize"))

# Message durable -> vectorize (both entry paths converge here)
wire(Message).to(vectorize).durable()

# Fragment -> save_fragment (in-process within vectorize-worker)
wire(Fragment).to(save_fragment)
```

> **前置依赖**：`wire(...).from_(...)` 的 DSL —— T0.15/T0.16 已为 HTTP/cron/interval source 加过；T1.4.5 会补齐 mq kind 的 engine 支持，`from_source` 本身 DSL 应该统一（如果没统一，T1.4.5 需要先补齐）。

- [ ] **Step 4: `wiring/__init__.py`**

```python
# app/wiring/__init__.py
"""Import all wiring submodules so their wire() calls run on package import."""
from app.wiring import memory  # noqa: F401
```

- [ ] **Step 5: `deployment.py`**

```python
# app/deployment.py
"""Node -> PaaS App bindings.

Every Node not bound here defaults to the "agent-service" (main HTTP) app.
App names must already exist in PaaS (create via /api/paas/apps/ before binding).
"""
from app.runtime.placement import bind
from app.nodes.hydrate_message import hydrate_message
from app.nodes.vectorize import vectorize
from app.nodes.save_fragment import save_fragment

bind(hydrate_message).to_app("vectorize-worker")
bind(vectorize).to_app("vectorize-worker")
bind(save_fragment).to_app("vectorize-worker")
```

- [ ] **Step 6: `tests/wiring/test_memory.py`**

```python
import pytest
from app.runtime.wire import WIRING_REGISTRY, clear_wiring
from app.runtime.placement import iter_bindings, clear_bindings
from app.runtime.graph import compile_graph


def _fresh_import():
    clear_wiring(); clear_bindings()
    import importlib, app.wiring.memory as m, app.deployment as d
    importlib.reload(m); importlib.reload(d)


def test_mq_entry_wired_to_hydrate():
    _fresh_import()
    from app.domain.message_request import MessageRequest
    from app.nodes.hydrate_message import hydrate_message
    wires = [w for w in WIRING_REGISTRY if w.data_type is MessageRequest]
    assert any(hydrate_message in w.consumers and any(s.kind == "mq" for s in w.sources) for w in wires)


def test_message_durable_to_vectorize():
    _fresh_import()
    from app.domain.message import Message
    from app.nodes.vectorize import vectorize
    wires = [w for w in WIRING_REGISTRY if w.data_type is Message]
    assert any(w.durable and vectorize in w.consumers for w in wires)


def test_fragment_to_save_fragment():
    _fresh_import()
    from app.domain.fragment import Fragment
    from app.nodes.save_fragment import save_fragment
    wires = [w for w in WIRING_REGISTRY if w.data_type is Fragment]
    assert any(save_fragment in w.consumers for w in wires)


def test_bindings_set():
    _fresh_import()
    from app.nodes.hydrate_message import hydrate_message
    from app.nodes.vectorize import vectorize
    from app.nodes.save_fragment import save_fragment
    b = dict(iter_bindings())
    assert b[hydrate_message] == "vectorize-worker"
    assert b[vectorize] == "vectorize-worker"
    assert b[save_fragment] == "vectorize-worker"


def test_compile_succeeds():
    _fresh_import()
    compile_graph()
```

- [ ] **Step 7: 激活 runtime_entry.py（本 task 的关键步骤）**

修改 `app/workers/runtime_entry.py`，在 `main()` 调 `Runtime().run()` 之前确保 wiring 和 deployment 都被 import：

```python
# app/workers/runtime_entry.py
"""Unified worker entry. All runtime-managed apps boot through here.

The `import app.wiring` and `import app.deployment` lines are side-effect
imports — they trigger `wire(...)` calls and `bind(...)` calls that register
the graph before Runtime reads it. Without these imports, Runtime starts with
an empty graph.
"""
from __future__ import annotations

import asyncio

import app.wiring  # noqa: F401 — side-effect: register wires
import app.deployment  # noqa: F401 — side-effect: register bindings

from app.runtime.engine import Runtime


async def _main() -> None:
    await Runtime().run()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
```

（如果 T0.15 完成时 `runtime_entry.py` 已有 `main()`，保留原结构，只追加两行 side-effect import。）

- [ ] **Step 8: 测试通过**

```bash
cd apps/agent-service && uv run pytest tests/domain/test_message_request.py tests/nodes/test_hydrate_message.py tests/wiring/test_memory.py -v
```

额外 smoke check：直接 import runtime_entry 不报错：

```bash
cd apps/agent-service && uv run python -c "import app.workers.runtime_entry; print('runtime_entry imports clean')"
```

再跑一遍全量：

```bash
cd apps/agent-service && uv run pytest
```

- [ ] **Step 9: Commit**

```bash
git commit -m "feat(wiring): MQ entry + durable Message + save_fragment pipeline (vectorize-worker)"
```

---

### Task 1.6: 切换 vectorize-worker entry command（PaaS API）

**不改代码，改 PaaS 侧 App 配置。** 手动操作。

- [ ] **Step 1: 读当前 vectorize-worker command**

```bash
curl -s -H "Authorization: Bearer $PAAS_TOKEN" "$PAAS_API/api/paas/apps/vectorize-worker" | jq .command
```

预期输出（根据 app.yaml 推断）：
```
["uv", "run", "--no-sync", "python", "-m", "app.workers.vectorize"]
```

- [ ] **Step 2: 用 /api-test skill 修改 command 到新 entry**

通过 `/api-test` skill 调 `PUT /api/paas/apps/vectorize-worker`，payload：

```json
{"command": ["uv", "run", "--no-sync", "python", "-m", "app.workers.runtime_entry"]}
```

（PaaS `PUT /apps/{name}/` 是 merge 语义，只传 command 字段即可。）

- [ ] **Step 3: 验证配置生效**

```bash
/ops resolved-config vectorize-worker prod
```

确认 `command` 已更新。

- [ ] **Step 4: 不部署。先让下一个 Task 把 Message Bridge 接完、存量脚本写完，再一次性部署泳道验证。**

---

### Task 1.7: 接入 Message Bridge（老代码写入点加 emit）

**Files:**
- Modify: `apps/agent-service/app/life/proactive.py:142`（起点；完整清单以 Step 1 grep 为准）
- Modify: 所有其他 `ConversationMessage` 写入点
- Test: integration test in 泳道（无单元测试，老代码链太重）

- [ ] **Step 1: 穷举所有写 `conversation_messages` 的点**

用下面三条 grep **并集**（单一模式会漏），把每个命中（排除 tests/）列入临时清单：

```bash
# 1) ORM 构造（可能立刻 session.add，也可能赋给变量稍后 add）
grep -rnE "ConversationMessage\s*\(" apps/agent-service/app/ --include="*.py" \
  | grep -v "tests/\|_test\.py"

# 2) session.add(...) 收口（用于确认 add() 调用点；对照 1) 的结果）
grep -rnE "session\.add\(|\.add\(.*\bconv" apps/agent-service/app/ --include="*.py" \
  | grep -v "tests/\|_test\.py"

# 3) bulk / merge / upsert 路径（防止 session.add 以外的写法遗漏）
grep -rnE "session\.(add_all|merge)\(|INSERT INTO conversation_messages|ON CONFLICT.*conversation_messages" \
  apps/agent-service/app/ --include="*.py" | grep -v "tests/\|_test\.py"
```

**必须把三条 grep 的结果交叉比对**，对每个 `ConversationMessage(...)` 构造点追到它的 commit 路径（add + commit、或 add_all + commit、或裸 SQL）。清单至少应覆盖：

- `app/life/proactive.py`
- chat pipeline 主写入点（在 `app/chat/` 下）
- read 路径（`app/read/` 或类似；paper-read 阶段遗漏的点）
- afterthought / rebuild 路径（如有）

把清单写入 `/tmp/cm_write_sites.md` 供下一步逐个修改。

- [ ] **Step 2: 在每个写入点的"commit 后"加 emit**

每处改动的 pattern：

```python
# Before
session.add(conv_msg)
await session.commit()

# After
session.add(conv_msg)
await session.commit()
from app.bridges.message_bridge import emit_legacy_message  # local import to avoid boot-time cycles
await emit_legacy_message(conv_msg)
```

**关键细节**：
- `emit` 必须在 `commit()` **之后**，否则 Message 发出后 Node 去查 pg 可能查不到（读到未提交的行为 pg 默认 READ COMMITTED 隔离下不会发生，但 emit→durable→消费端可能跨事务）。
- `add_all([m1, m2, ...])` 场景：对列表里**每条** Message 都调一次 `emit_legacy_message`，不能合并。
- 裸 SQL 写路径（如果有）：先把裸 SQL 改成 ORM，再加 emit；ORM 化比 emit 优先。

- [ ] **Step 3: 跑 unit tests 确保没回归**

```bash
cd apps/agent-service && uv run pytest
```

- [ ] **Step 4: grep 验证清单全覆盖**

重跑 Step 1 的三条 grep，对每个 `ConversationMessage(...)` 构造点确认紧随的 commit 路径后都有 `emit_legacy_message`。如果遗漏，补上。

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(bridge): emit_legacy_message() at every ConversationMessage write site"
```

---

### ~~Task 1.8: 存量回扫脚本~~ **SKIPPED (2026-04-24)**

> **跳过原因**：bezhai 2026-04-24 明确"不需要关注历史消息,直接用 MQ"。lark-server 的新消息会继续 publish 到 `vectorize` 队列,Phase 1 上线后由新 MQSource 消费;历史 `vector_status=pending` 的行被**视作过期数据不再处理**(也不再扫)。
>
> `vector_status` 字段在新架构下没有写回维护者(T1.3 决定不在 @node 里写),本任务原本用它做扫描谓词,现在这个谓词本身没意义。T1.10 会把 `vector_status` 字段一并退役。
>
> 以下内容保留作为历史参考,不执行。

**Files:**
- ~~Create (tmp, do NOT commit to repo): `/tmp/backfill_vectorize.py`~~

- [ ] ~~**Step 1: 写脚本**~~

```python
# /tmp/backfill_vectorize.py
"""One-off: re-emit all pending conversation_messages into new runtime.

Run after the new vectorize-worker is deployed to a lane and verified working
for a few fresh messages. Run ONCE, then delete.
"""
import asyncio
from sqlalchemy import select
from app.data.session import get_session
from app.data.models import ConversationMessage
from app.bridges.message_bridge import emit_legacy_message
import app.wiring  # noqa: side-effect register
import app.deployment  # noqa

async def main():
    async with get_session() as s:
        r = await s.execute(
            select(ConversationMessage).where(ConversationMessage.vector_status == "pending")
        )
        pending = r.scalars().all()
    print(f"Backfilling {len(pending)} messages...")
    for i, cm in enumerate(pending, 1):
        await emit_legacy_message(cm)
        if i % 100 == 0:
            print(f"  {i}/{len(pending)}")
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 暂不执行**——等 Task 1.9 泳道部署验证过才跑。

---

### Task 1.9: 泳道部署 + 端到端验证

- [ ] **Step 1: 部署 vectorize-worker 到测试泳道**

```bash
make deploy APP=vectorize-worker LANE=df-v0 GIT_REF=refactor/agent-dataflow-abstraction
```

（如果 App 的 command 只在 prod 改过，dev 泳道继承 prod 配置即可；否则需要先改 dev lane 的 command。）

- [ ] **Step 2: 写一条新消息触发流水线**

```bash
/ops bind bot dev df-v0
```

在 dev bot 发一条测试消息。

- [ ] **Step 3: 验证 vectorize-worker 日志**

```bash
make logs APP=vectorize-worker LANE=df-v0 KEYWORD="vectorize" SINCE=5m
```

**检查点**：
- 看到 runtime startup 日志：`Runtime(app_name='vectorize-worker') running X nodes`
- 看到 `emit(Message)` → `vectorize` node 触发
- 看到 `save_fragment` 写入 qdrant 成功

- [ ] **Step 4: 验证 qdrant 新向量已写入**

```bash
/ops-db @chiwei "SELECT message_id, vector_status FROM conversation_messages WHERE message_id = '<new_message_id>'"
```

通过 qdrant API（/ops 或 /api-test）验证 `messages_recall` collection 里有这条 fragment_id。

- [ ] **Step 5: 无回归后执行回扫**

```bash
kubectl -n prod cp /tmp/backfill_vectorize.py vectorize-worker-df-v0-xxx:/tmp/
kubectl -n prod exec -it vectorize-worker-df-v0-xxx -- python /tmp/backfill_vectorize.py
```

**注意**：按 safety-rules，`kubectl exec` 仅限 read-only 排查——这里是一次性回扫脚本运行在**泳道 Pod** 里（非生产 writes 到生产数据库，但数据库本身是共享的）。先和用户确认是否可以在泳道 exec 跑这个脚本；如果不行，走 PaaS 侧独立一次性 job。

- [ ] **Step 6: 清理泳道、解绑 bot**

```bash
/ops unbind bot dev
make undeploy APP=vectorize-worker LANE=df-v0
```

**不提交 Task 1.9 的任何产物到 repo。**

---

### Task 1.10: 删除旧 vectorize 代码 + `vector_status` 字段退役

> **范围明确（2026-04-24）**：本 task 删的是**消息向量化**老代码(`vectorize_message`、`process_message`、`handle_vectorize`、`cron_scan_pending_messages`、`start_vectorize_consumer` 及相关 semaphore/redis lock)，**以及 `conversation_messages.vector_status` 字段**。`memory_vectorize` 队列 + `handle_memory_vectorize` 是另一条链(fragment/abstract 向量化)，**不在本 PR 范围**，保留原样。

**Files:**
- Modify: `apps/agent-service/app/workers/vectorize.py` — **只删**消息向量化相关(`vectorize_message / process_message / handle_vectorize / start_vectorize_consumer / cron_scan_pending_messages / _semaphore / _get_semaphore`)；保留 `handle_memory_vectorize` 及其启动逻辑(直到下个 PR 迁 memory_vectorize)。文件可能要重命名为 `memory_vectorize_worker.py` 或保持 `vectorize.py` 但内容大幅缩小 — 以 grep 结果为准
- Modify: `apps/agent-service/app/workers/arq_settings.py` — 删 `cron_scan_pending_messages` 条目
- Modify: `apps/agent-service/app/infra/rabbitmq.py` — 删 `VECTORIZE` Route 常量;**保留** `MEMORY_VECTORIZE`(仍被 `handle_memory_vectorize` 使用)
- Modify: `apps/agent-service/app/domain/message.py` — 删 `vector_status` 字段(字段退役)
- Modify: `apps/agent-service/app/data/models.py::ConversationMessage` — 删 `vector_status` 列
- Modify: 所有读 `vector_status` 的地方(grep 找) — 清掉
- DB 改动: `/ops-db submit @chiwei "ALTER TABLE conversation_messages DROP COLUMN vector_status;"` — 走审计通道,**ship 后执行,不在 PR 本身**

**vector_status 退役说明**：
- 老架构里 `vector_status` 是"处理状态字段"(pending/completed/skipped/failed),老 worker 读它 + 写它
- 新架构里没人读写它,没人依赖它做业务决策
- 保留会误导(看数据库以为 pending 的都没处理,实际可能已经完成)
- 直接 DROP COLUMN 最干净。migrator 是 additive-only,不会做 DROP,此改动走 DDL submit 通道

- [ ] **Step 1: 把 helpers 迁到 `app/nodes/_helpers.py`**

```python
# app/nodes/_helpers.py
"""Internal helpers used by Node implementations. Not part of public API."""
from app.workers.vectorize import parse_content, download_image, build_embedding_instruction  # type: ignore
# (temporarily re-export; next step deletes the original module)
```

- [ ] **Step 2: 改 `app/nodes/vectorize.py` import**

```python
from app.nodes._helpers import parse_content, download_image, build_embedding_instruction
```

- [ ] **Step 3: 把函数本体真正搬到 `_helpers.py`（从 workers/vectorize.py 剪切过来）**

剪切：`parse_content`（行号见 grep）、`download_image`、`build_embedding_instruction`。

- [ ] **Step 4: 清空 workers/vectorize.py**

```python
# app/workers/vectorize.py
"""DEPRECATED: vectorize now runs in app.workers.runtime_entry via runtime/wire.

This file is kept only to avoid import-time breakage during the cutover.
Remove entirely after one deploy cycle."""
```

- [ ] **Step 5: 删除 arq cron 条目**

```bash
grep -n "cron_scan_pending_messages" apps/agent-service/app/workers/arq_settings.py
```

删该行。

- [ ] **Step 6: 删除 mq routes**

```bash
grep -n "VECTORIZE\|MEMORY_VECTORIZE" apps/agent-service/app/infra/rabbitmq.py
```

删 `VECTORIZE = Route(...)` 和 `MEMORY_VECTORIZE = Route(...)`。

- [ ] **Step 7: grep 验证零残留**

```bash
grep -rnE "VECTORIZE|MEMORY_VECTORIZE|cron_scan_pending_messages|handle_vectorize|vectorize_worker\.py" \
  apps/agent-service/ --include="*.py"
```

预期只剩（允许）：
- `app/workers/vectorize.py` 的 stub 文件本身
- 测试里的 mock 引用（合法）

业务代码里不应再有这些名字。

- [ ] **Step 8: 跑全量测试**

```bash
cd apps/agent-service && uv run pytest
```

- [ ] **Step 9: Commit**

```bash
git commit -m "refactor(vectorize): delete legacy consumer + cron scan + mq routes"
```

---

### Task 1.11: 静态验收标准 check

对照 spec §验收标准：

- [ ] **Step 1: grep §1 — 基础设施名字零残留**

```bash
grep -rnE "(rabbitmq|arq\.|redis\.Redis|qdrant_client|AsyncSessionLocal|scan_pending_|asyncio\.create_task\(.*_pipeline|find_latest_life_state)" \
  apps/agent-service/app/ --include="*.py" \
  | grep -vE "app/runtime/|app/capabilities/|app/wiring/|app/deployment\.py|app/infra/" | grep -vE "^\s*#"
```

预期：只有 `app/nodes/vectorize.py` 等 Phase 1 迁移的文件中**没有**这些名字；Phase 1 之后旧 workers 已删，应该零输出。

- [ ] **Step 2: §6 — append-only 审计约束**

```bash
grep -rnE "ON CONFLICT .* DO UPDATE|UPDATE data_[a-z_]+ SET" apps/agent-service/app/ --include="*.py"
```

预期：零输出。

- [ ] **Step 3: §4 — 每个 Data 至少有 Key**

由 `Data.__init_subclass__` 运行时保证；本步骤跑：

```bash
cd apps/agent-service && uv run python -c "import app.wiring; print(f'{len(__import__(\"app.runtime.data\").runtime.data.DATA_REGISTRY)} Data classes registered OK')"
```

预期：无 exception。

- [ ] **Step 4: §7 — AdminOnly 不出现在 Node 返回**

由 `@node` 装饰器运行时保证；import `app.wiring` 能成功即通过。

- [ ] **Step 5: §8 — mermaid dump（smoke test）**

```bash
cd apps/agent-service && uv run python -c "from app.runtime.graph import compile_graph; import app.wiring; g = compile_graph(); print(g.wires)"
```

预期：打印出 Message/Fragment wires。

- [ ] **Step 6: Commit 验收报告（可选，作为 PR 描述的一部分）**

无代码改动；把上述命令输出粘到 PR description。

---

## 验收与 Ship

- [ ] Phase 0 所有 task 通过 + 单元测试绿
- [ ] Phase 1 泳道部署验证：发新消息 → vectorize-worker 日志显示 runtime 跑通 → qdrant 新向量写入
- [ ] 存量回扫脚本跑完，`SELECT COUNT(*) FROM conversation_messages WHERE vector_status='pending'` 归零（或显著下降到合理值）
- [ ] grep 验收 §1 / §6 零残留（Phase 1 范围内的文件）
- [ ] **下泳道 + 清理 bot binding**
- [ ] **ship 到 prod 前必须等用户明确批准**（遵守项目 merge-and-ship.md 铁律）

---

## 备忘：后续 Phase 预览（不在本 plan 范围）

- Phase 2: Safety 管线抽 Node（wire(Message).to(safety_pre_check)）
- Phase 3: Drift / Afterthought 消灭内存 debouncer（依赖 `.debounce()` runtime 实现；本 plan 只定义 DSL，未实现 redis 计时器——Phase 3 开始落地）
- Phase 4: Life Engine / Schedule / Glimpse
- Phase 5: Chat 主 pipeline；`app/bridges/` 整包删除（Message Bridge 及同期新增的 bridge 全部清理）；Stream[T] 运行时行为（现在只有 type marker）
- Phase 6: 清扫
