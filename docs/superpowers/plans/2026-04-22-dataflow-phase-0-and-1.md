# Agent Service Dataflow Phase 0 + Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 agent-service dataflow 抽象的 runtime 骨架（Phase 0）并把 vectorize 管线迁到新框架（Phase 1）。旧 chat pipeline 保持原状，只在写 `conversation_messages` 之后加一行 `await emit(Message(...))` 作为 Legacy Bridge 入口；Phase 5 chat 迁完后这一行和 Bridge 一起删除。

**Architecture:**
- `app/runtime/`：框架代码（Data base、Marker、@node、wire DSL、graph、migrator、query、engine、placement、legacy_bridge）
- `app/capabilities/`：六个 capability 薄 adapter（LLMClient / AgentRunner / EmbedderClient / VectorStore / HTTPClient / query）
- `app/domain/`：业务 Data 类（`Message`、`Fragment` 等，逐步接管旧 `app/data/models.py` 的表）
- `app/nodes/`：`@node async def ...` 业务函数
- `app/wiring/`：按域拆分的 `wire(...)` 声明文件
- `app/deployment.py`：`bind(Node).to_app("...")` 归属声明
- `apps/paas-engine/internal/adapter/kubernetes/deployer.go`：deployer 改动，注入 `APP_NAME` 环境变量

Phase 0 交付：runtime 能跑一个 toy Node；所有 capability adapter 通过；Schema Migrator 能接管旧表；Legacy Bridge 的 `emit()` 可用。Phase 1 交付：vectorize 跑在新框架的 `vectorize-worker` App 里，旧 cron/queue/publish 全部删除，一次性回扫脚本处理存量。

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
    emit.py             # runtime.emit(data) — Legacy Bridge 和原生 producer 共用入口
    legacy_bridge.py    # Phase 0 临时 adapter；Phase 5 后删除

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
- `apps/agent-service/app/life/proactive.py:142` — session.add 之后加一行 `await emit(Message(...))`（Phase 1 最后一步）
- `apps/agent-service/app/main.py` — 启动时调 `runtime.migrate_schema()` 和（HTTP Pod）`runtime.serve(app_name)`

**删除（Phase 1 结束时）：**
- `apps/agent-service/app/workers/vectorize.py` 全部（旧 consumer + cron）
- `apps/agent-service/app/workers/arq_settings.py` 中 `cron_scan_pending_messages` 对应的 cron 条目
- `app/infra/rabbitmq.py` 中 `VECTORIZE` / `MEMORY_VECTORIZE` route 常量

**一次性脚本（Phase 1 最后执行，不入 repo 常驻）：**
- `/tmp/backfill_vectorize.py` — 存量 `vector_status='pending'` 行的回扫

---

## Phase 0 — Runtime 骨架

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
    if ret is not None:
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

### Task 0.17: Legacy Bridge（emit wrapper）

**Files:**
- Create: `apps/agent-service/app/runtime/legacy_bridge.py`
- Test: `apps/agent-service/tests/runtime/test_legacy_bridge.py`

Legacy Bridge 只是一个极薄的函数：老代码在写完 `conversation_messages` 后调用它，从行对象构造 `Message(Data)` 并 `await emit(msg)`。Phase 5 整体删除。

- [ ] **Step 1: 写失败测试**

```python
# tests/runtime/test_legacy_bridge.py
import pytest
from unittest.mock import AsyncMock, patch
from app.data.models import ConversationMessage
from app.runtime.legacy_bridge import emit_legacy_message

@pytest.mark.asyncio
async def test_emit_legacy_message_lifts_and_emits():
    cm = ConversationMessage(
        message_id="m1", chat_id="c1", content="hi", role="user",
        create_time=1234567890, vector_status="pending",
    )
    with patch("app.runtime.legacy_bridge.emit", new_callable=AsyncMock) as m:
        await emit_legacy_message(cm)
    m.assert_awaited_once()
    msg = m.call_args.args[0]
    from app.domain.message import Message
    assert isinstance(msg, Message)
    assert msg.message_id == "m1"
    assert msg.chat_id == "c1"
```

- [ ] **Step 2: 实现 legacy_bridge.py（Message 类在 Task 1.1 建；先用 forward ref）**

```python
# app/runtime/legacy_bridge.py
"""Legacy Bridge: lift legacy ConversationMessage rows into new Message Data.

This module exists ONLY during Phases 0-4. After Phase 5 (chat pipeline
migration), the call sites are deleted and so is this file.
"""
from app.runtime.emit import emit

async def emit_legacy_message(cm) -> None:  # cm: ConversationMessage
    from app.domain.message import Message  # forward import to avoid cycles
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

- [ ] **Step 3: 测试通过（需要 Task 1.1 的 Message 先就位；两个任务顺序连在一起或用 xfail 临时跳过）**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(runtime): legacy_bridge — emit_legacy_message(ConversationMessage) -> Message(Data)"
```

---

## Phase 1 — Vectorize 管线迁移

### Task 1.1: Message Data（接管 conversation_messages 表）

**Files:**
- Create: `apps/agent-service/app/domain/__init__.py`
- Create: `apps/agent-service/app/domain/message.py`
- Test: `apps/agent-service/tests/domain/test_message.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/domain/test_message.py
from typing import Annotated
from app.domain.message import Message
from app.runtime.data import key_fields, dedup_fields

def test_message_key_is_message_id():
    assert key_fields(Message) == ("message_id",)

def test_message_dedup_includes_generation():
    assert dedup_fields(Message) == ("message_id", "generation")

def test_message_existing_table():
    assert Message.Meta.existing_table == "conversation_messages"

def test_message_instance():
    m = Message(message_id="m1", generation=0, chat_id="c1", persona_id="p1",
                role="user", text="hi", images=[], create_time=123)
    assert m.message_id == "m1"
```

- [ ] **Step 2: 实现 message.py**

```python
# app/domain/message.py
from typing import Annotated
from app.runtime.data import Data, Key, DedupKey

class Message(Data):
    message_id: Annotated[str, Key, DedupKey]
    generation: Annotated[int, DedupKey] = 0
    chat_id: str
    persona_id: str = ""
    role: str
    text: str
    images: list[str] = []
    create_time: int

    class Meta:
        existing_table = "conversation_messages"
```

- [ ] **Step 3: 更新 `app/domain/__init__.py`**

```python
from app.domain.message import Message
__all__ = ["Message"]
```

- [ ] **Step 4: 跑 runtime migrator，确认不碰 conversation_messages 的现有列（新增列允许）**

```bash
uv run python -c "from app.runtime.migrator import plan_migration; from app.runtime.data import DATA_REGISTRY; import app.domain; \
existing = {'conversation_messages': {'message_id':'text','chat_id':'text','content':'text','role':'text','create_time':'bigint','vector_status':'text'}}; \
plan = plan_migration(list(DATA_REGISTRY), existing); \
[print(s.sql) for s in plan.stmts]"
```

预期：输出只包含 `ALTER TABLE conversation_messages ADD COLUMN ...` 对 persona_id / images / generation / dedup_hash 的新增；不包含 CREATE TABLE。

**重要：新增 `dedup_hash` 列需要 backfill 和 UNIQUE index**。此处由于旧表不是 append-only 且 `message_id` 已经 PK，单独处理：

```python
# app/domain/message.py
class Message(Data):
    ...
    class Meta:
        existing_table = "conversation_messages"
        # override: use message_id as implicit dedup (no separate hash column needed)
        dedup_column = "message_id"
```

在 migrator 里扩展：若 `Meta.dedup_column` 指定，则跳过 `dedup_hash` 列的自动生成，改用指定列。改 `_table_name` 附近逻辑。

- [ ] **Step 5: 更新 migrator.py 支持 `Meta.dedup_column`**

```python
# in migrator.py, where dedup_hash is added:
meta = getattr(cls, "Meta", None)
dedup_col = getattr(meta, "dedup_column", None) if meta else None
if not dedup_col:
    desired_cols["dedup_hash"] = "TEXT"
```

相应 `insert_idempotent` / `_dedup_hash` 也要支持这个模式：

```python
# persist.py insert_idempotent: if Meta.dedup_column specified, ON CONFLICT (<that column>)
conflict_col = getattr(getattr(cls, "Meta", None), "dedup_column", None) or "dedup_hash"
```

相应调整测试。

- [ ] **Step 6: 测试通过**

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(domain): Message Data (maps to conversation_messages, dedup by message_id)"
```

---

### Task 1.2: Fragment Data

**Files:**
- Create: `apps/agent-service/app/domain/fragment.py`
- Test: `apps/agent-service/tests/domain/test_fragment.py`

Fragment 是 vectorize 的产出——一个已算好向量的段落。**不需要 pg 表**（业务代码从不查），但需要是 Data 类以便流经 wire。Fragment 走 `.broadcast()` 发给 VectorStore Sink；不落 pg。

- [ ] **Step 1: 写失败测试**

```python
# tests/domain/test_fragment.py
from typing import Annotated
from app.domain.fragment import Fragment
from app.runtime.data import key_fields

def test_fragment_key():
    assert key_fields(Fragment) == ("fragment_id",)

def test_fragment_transient():
    assert getattr(Fragment.Meta, "transient", False) is True

def test_fragment_instance():
    f = Fragment(
        fragment_id="m1:0", message_id="m1", chat_id="c1",
        dense=[0.0]*1024, sparse={"indices": [1], "values": [0.5]},
        dense_cluster=[0.0]*1024,
        payload={"text": "hi"},
    )
    assert f.fragment_id == "m1:0"
```

- [ ] **Step 2: 实现 fragment.py**

```python
# app/domain/fragment.py
from typing import Annotated
from app.runtime.data import Data, Key

class Fragment(Data):
    fragment_id: Annotated[str, Key]
    message_id: str
    chat_id: str
    dense: list[float]
    sparse: dict  # {"indices": [...], "values": [...]}
    dense_cluster: list[float]
    payload: dict

    class Meta:
        transient = True  # not persisted to pg; goes straight to Sink
```

- [ ] **Step 3: migrator.py 跳过 `Meta.transient=True` 的类**

```python
# migrator.py, plan_migration:
if getattr(getattr(cls, "Meta", None), "transient", False):
    continue
```

- [ ] **Step 4: 测试通过**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(domain): Fragment (transient, not persisted)"
```

---

### Task 1.3: vectorize Node

**Files:**
- Create: `apps/agent-service/app/nodes/__init__.py`
- Create: `apps/agent-service/app/nodes/vectorize.py`
- Test: `apps/agent-service/tests/nodes/test_vectorize.py`

- [ ] **Step 1: 读旧 vectorize 逻辑**

```bash
sed -n '57,167p' apps/agent-service/app/workers/vectorize.py
```

理解：parse content → 下载图片 → 构造 instruction → 并行 hybrid+dense → 返回

- [ ] **Step 2: 写失败测试（mock EmbedderClient）**

```python
# tests/nodes/test_vectorize.py
import pytest
from unittest.mock import AsyncMock, patch
from app.domain.message import Message
from app.nodes.vectorize import vectorize

@pytest.mark.asyncio
async def test_vectorize_produces_fragment():
    m = Message(message_id="m1", generation=0, chat_id="c1", persona_id="p1",
                role="user", text="hello world", images=[], create_time=1)
    with patch("app.nodes.vectorize.embedder.encode", new_callable=AsyncMock) as enc:
        enc.side_effect = [
            {"dense": [0.1]*1024, "sparse": {"indices": [1], "values": [0.5]}},
            {"dense": [0.2]*1024},
        ]
        frag = await vectorize(m)
    assert frag.fragment_id.startswith("m1")
    assert len(frag.dense) == 1024
    assert frag.payload["text"] == "hello world"

@pytest.mark.asyncio
async def test_vectorize_with_image():
    m = Message(message_id="m2", generation=0, chat_id="c1", persona_id="p1",
                role="user", text="see this", images=["https://x/a.jpg"], create_time=1)
    with patch("app.nodes.vectorize.embedder.encode", new_callable=AsyncMock) as enc, \
         patch("app.nodes.vectorize.download_image", new_callable=AsyncMock) as dl:
        dl.return_value = b"imagebytes"
        enc.side_effect = [{"dense":[0.1]*1024,"sparse":{"indices":[],"values":[]}},{"dense":[0.2]*1024}]
        frag = await vectorize(m)
    dl.assert_awaited_once()
```

- [ ] **Step 3: 实现 vectorize.py（保留旧核心逻辑，只把依赖换成 capability）**

```python
# app/nodes/vectorize.py
import asyncio
from app.runtime.node import node
from app.capabilities.embed import EmbedderClient
from app.domain.message import Message
from app.domain.fragment import Fragment

embedder = EmbedderClient()

# TODO: import real download_image / parse_content from existing modules
from app.workers.vectorize import parse_content, download_image, build_embedding_instruction  # type: ignore

@node
async def vectorize(msg: Message) -> Fragment:
    parsed = parse_content(msg.text)
    images_bytes: list[bytes] = []
    for url in msg.images:
        try:
            data = await download_image(url)
            images_bytes.append(data)
        except PermissionError:
            return None  # gracefully skip; runtime drops None
    instruction = build_embedding_instruction(msg.role, parsed)
    hybrid_task = embedder.encode(parsed, images_bytes, mode="hybrid", instruction=instruction)
    dense_task = embedder.encode(parsed, images_bytes, mode="dense", instruction=instruction)
    hybrid, dense_cluster = await asyncio.gather(hybrid_task, dense_task)
    return Fragment(
        fragment_id=f"{msg.message_id}:0",
        message_id=msg.message_id,
        chat_id=msg.chat_id,
        dense=hybrid["dense"],
        sparse=hybrid["sparse"],
        dense_cluster=dense_cluster["dense"],
        payload={
            "text": parsed, "role": msg.role, "persona_id": msg.persona_id,
            "create_time": msg.create_time,
        },
    )
```

**注意**：`parse_content` / `download_image` / `build_embedding_instruction` 暂时从旧 `workers/vectorize.py` 直接 import（Phase 1 尾部把这些函数挪到 `app/nodes/_vectorize_helpers.py`，把旧文件清空）。

- [ ] **Step 4: 测试通过**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(nodes): vectorize Node (Message -> Fragment via EmbedderClient)"
```

---

### Task 1.4: save_fragment Node（写 qdrant）

**Files:**
- Create: `apps/agent-service/app/nodes/save_fragment.py`
- Test: `apps/agent-service/tests/nodes/test_save_fragment.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/nodes/test_save_fragment.py
import pytest
from unittest.mock import AsyncMock, patch
from app.domain.fragment import Fragment
from app.nodes.save_fragment import save_fragment

@pytest.mark.asyncio
async def test_save_fragment_upserts_to_both_collections():
    f = Fragment(fragment_id="m1:0", message_id="m1", chat_id="c1",
                 dense=[0.1]*1024, sparse={"indices": [1], "values": [0.5]},
                 dense_cluster=[0.2]*1024, payload={"text": "hi"})
    with patch("app.nodes.save_fragment.recall_store.upsert", new_callable=AsyncMock) as r, \
         patch("app.nodes.save_fragment.cluster_store.upsert", new_callable=AsyncMock) as c:
        await save_fragment(f)
    r.assert_awaited_once()
    c.assert_awaited_once()
```

- [ ] **Step 2: 实现 save_fragment.py**

```python
# app/nodes/save_fragment.py
from app.runtime.node import node
from app.capabilities.vector_store import VectorStore
from app.domain.fragment import Fragment

recall_store = VectorStore("messages_recall")
cluster_store = VectorStore("messages_cluster")

@node
async def save_fragment(frag: Fragment) -> None:
    await recall_store.upsert(
        frag.fragment_id,
        {"dense": frag.dense, "sparse": frag.sparse},
        frag.payload,
    )
    await cluster_store.upsert(
        frag.fragment_id,
        {"dense": frag.dense_cluster},
        frag.payload,
    )
```

- [ ] **Step 3: 测试通过**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(nodes): save_fragment Node (Fragment -> qdrant recall + cluster)"
```

---

### Task 1.5: wiring/memory.py + deployment.py

**Files:**
- Create: `apps/agent-service/app/wiring/__init__.py`
- Create: `apps/agent-service/app/wiring/memory.py`
- Create: `apps/agent-service/app/deployment.py`
- Test: `apps/agent-service/tests/wiring/test_memory.py`

- [ ] **Step 1: 写失败测试（验证 wiring 注册 + 图可 compile）**

```python
# tests/wiring/test_memory.py
import importlib
from app.runtime.wire import WIRING_REGISTRY, clear_wiring
from app.runtime.placement import _BINDINGS, clear_bindings
from app.runtime.graph import compile_graph

def test_wiring_memory_registers_expected_edges():
    clear_wiring(); clear_bindings()
    import app.wiring.memory  # noqa
    import app.deployment  # noqa
    from app.domain.message import Message
    from app.domain.fragment import Fragment
    msg_wires = [w for w in WIRING_REGISTRY if w.data_type is Message]
    frag_wires = [w for w in WIRING_REGISTRY if w.data_type is Fragment]
    assert any(w.durable and any(c.__name__ == "vectorize" for c in w.consumers) for w in msg_wires)
    assert any(any(c.__name__ == "save_fragment" for c in w.consumers) for w in frag_wires)

def test_bindings_set():
    from app.nodes.vectorize import vectorize
    from app.nodes.save_fragment import save_fragment
    assert _BINDINGS[vectorize] == "vectorize-worker"
    assert _BINDINGS[save_fragment] == "vectorize-worker"

def test_compile_succeeds():
    compile_graph()
```

- [ ] **Step 2: 实现 wiring/memory.py**

```python
# app/wiring/memory.py
from app.runtime.wire import wire
from app.domain.message import Message
from app.domain.fragment import Fragment
from app.nodes.vectorize import vectorize
from app.nodes.save_fragment import save_fragment

# Message durable -> vectorize (Phase 1 cutover target)
wire(Message).to(vectorize).durable()

# Fragment -> save_fragment (in-process within vectorize-worker)
wire(Fragment).to(save_fragment)
```

- [ ] **Step 3: 实现 wiring/__init__.py**

```python
# app/wiring/__init__.py
"""Import all wiring submodules so their wire() calls run on package import."""
from app.wiring import memory  # noqa: F401
```

- [ ] **Step 4: 实现 deployment.py**

```python
# app/deployment.py
"""Node -> PaaS App bindings.

Every Node not bound here defaults to the "agent-service" (main HTTP) app.
App names must already exist in PaaS (create via /api/paas/apps/ before binding).
"""
from app.runtime.placement import bind
from app.nodes.vectorize import vectorize
from app.nodes.save_fragment import save_fragment

bind(vectorize).to_app("vectorize-worker")
bind(save_fragment).to_app("vectorize-worker")
```

- [ ] **Step 5: 测试通过**

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(wiring): memory.py + deployment.py for vectorize pipeline"
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

- [ ] **Step 4: 不部署。先让下一个 Task 把 Legacy Bridge 接完、存量脚本写完，再一次性部署泳道验证。**

---

### Task 1.7: 接入 LegacyMessageBridge（老代码写入点加 emit）

**Files:**
- Modify: `apps/agent-service/app/life/proactive.py:142`
- （如有其他写入点也一并改——grep 验证）
- Test: integration test in 泳道（无单元测试，老代码链太重）

- [ ] **Step 1: grep 所有写 `conversation_messages` 的点**

```bash
grep -rn "session.add.*ConversationMessage\|ConversationMessage(.*)" apps/agent-service/app/ --include="*.py" | grep -v "_test\|tests/"
```

把每个 hit 列进一张临时清单。预期包括：
- `app/life/proactive.py:142`
- chat pipeline 主写入点（读 `app/chat/` 下代码确认具体行号）
- 其他 producer（read/afterthought 等如有）

- [ ] **Step 2: 在每个写入点下面加一行 emit_legacy_message**

每处改动的 pattern：

```python
# Before
session.add(conv_msg)
await session.commit()

# After
session.add(conv_msg)
await session.commit()
from app.runtime.legacy_bridge import emit_legacy_message  # local import to avoid boot-time cycles
await emit_legacy_message(conv_msg)
```

- [ ] **Step 3: 跑 unit tests 确保没回归**

`cd apps/agent-service && uv run pytest`

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(bridge): emit_legacy_message() at every ConversationMessage write site"
```

---

### Task 1.8: 存量回扫脚本

**Files:**
- Create (tmp, do NOT commit to repo): `/tmp/backfill_vectorize.py`

注：CLAUDE.md 禁止 `scripts/` 下放一次性脚本。放 `/tmp/` 运行一次即弃。

- [ ] **Step 1: 写脚本**

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
from app.runtime.legacy_bridge import emit_legacy_message
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

### Task 1.10: 删除旧 vectorize 代码

**Files:**
- Modify: `apps/agent-service/app/workers/vectorize.py` — 清空业务逻辑，只留 `parse_content / download_image / build_embedding_instruction` helpers（或迁到 `app/nodes/_helpers.py`）
- Modify: `apps/agent-service/app/workers/arq_settings.py` — 删除 `cron_scan_pending_messages` cron 条目
- Modify: `apps/agent-service/app/infra/rabbitmq.py` — 删除 `VECTORIZE` / `MEMORY_VECTORIZE` Route 常量

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
- Phase 5: Chat 主 pipeline；Legacy Bridge 整体删除；Stream[T] 运行时行为（现在只有 type marker）
- Phase 6: 清扫
