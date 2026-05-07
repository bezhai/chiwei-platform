# Dataflow Phase 6 v4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 dataflow Phase 6 拉回原 design line 543 的"终结清扫" scope —— 闭合 6 个 framework capability gap (HTTP source / 跨进程 emit / tool 副作用 / 事件驱动 worker / 散落 fire-and-forget / 单 worker entry) + 衍生业务清扫 + 业务实现层冗余整理。

**Architecture:** 按 `docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md` (v4) 的 12 commit 切法实施：framework 扩展先做（runtime/）、业务收敛立即跟上（验证 framework 正确性）、业务实现层冗余作为独立维度收尾。每 task 自包含 + 测试 green + ruff 通过 + 独立可 revert。

**Tech Stack:** Python 3.12 / pytest-asyncio / SQLAlchemy AsyncSession / FastAPI / 项目自有 dataflow runtime（`app.runtime.{emit,wire,Source,Data,placement,...}`）/ aio_pika。

**前置 spec：** `docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md` (v4)
**Baseline：** PR #210 已落 v3 4 commits（`d312a48` glimpse 监控 / `aece91d` placement 测试 / `53961db` proactive emit / `b3920c3` queries 拆 9 domain）

---

## 0. Task 顺序总览

| # | Phase | Gap | 主题 | 依赖 |
|---|---|---|---|---|
| 1 | A. framework | 1 | http_source 扩 method / path_params / query / RPC | — |
| 2 | B. business | 1 | routes.py 13 endpoint 收敛 | T1 |
| 3 | A. framework | 2 | emit 跨进程 dispatch via wire source.mq | — |
| 4 | B. business | 2 | vectorize_memory enqueue helpers 删，调用方改 emit | T3 |
| 5 | A. framework | 3 | 新增 AbstractMemoryCommitted / ScheduleRevisionCreated / NoteCreated Data + wire | — |
| 6 | B. business | 3 | agent tools 写 DB 后 emit Data | T5 |
| 7 | A. framework | 4 | sync_life_state_node + wire(ScheduleRevisionCreated) | T5, T6 |
| 8 | B. business | 4 + 6 | 删 arq_settings + state_sync_worker，update_schedule 改 emit，arq-worker 退场 | T7 |
| 9 | B. business | 5 | chat post_actions / context 删 asyncio.create_task | — |
| 10 | C. cleanup | — | chat/context.py 445 行拆分 | — |
| 11 | C. cleanup | — | chat/agent_stream + stream 合并 audit、router audit | — |
| 12 | C. cleanup | — | life/sister_theater + wild_agents + state_sync 收敛 | T7 |
| 13 | C. cleanup | — | memory/cross_chat + memory/context 评估 | — |
| 14 | D. ship | — | dev 泳道部署 + e2e 验证 | T1-T13 |

**所有 task 接进 PR #210**（不新开 PR）。每 task 1 commit。

---

## Task 1: Gap 1 framework — http_source 扩 method / path_params / query / RPC

**Files:**
- Modify: `apps/agent-service/app/runtime/source.py`
- Modify: `apps/agent-service/app/runtime/http_source.py`
- Test: `apps/agent-service/tests/runtime/test_http_source.py` (新建)

### 子步

- [ ] **Step 1.1: 设计 `Source.http` 扩展接口**

`runtime/source.py` 中 `Source.http(...)` 扩成：

```python
@staticmethod
def http(
    path: str,
    *,
    method: str = "POST",
    response: bool = False,
) -> SourceSpec:
    """HTTP source.

    method: "GET" | "POST" | "PUT" | "DELETE"。path 中 ``{name}`` 占位的
    部分自动绑定为 path param，按字段名注入到 Data 实例。
    GET / DELETE 把 query string 反序列化进 Data。
    POST / PUT 默认 body JSON 反序列化进 Data。

    response=True 表示节点返回值会作为 HTTP response body 同步返回；
    runtime 会在 emit 后等节点完成（in-process consumer 必须在本进程，
    跨进程的 RPC 模式 v4 不支持，会在编译期 raise）。
    """
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "DELETE"}:
        raise ValueError(f"unsupported HTTP method {method!r}")
    return SourceSpec("http", {"path": path, "method": method, "response": response})
```

- [ ] **Step 1.2: 写 framework 单测（TDD red）**

`tests/runtime/test_http_source.py`：

```python
"""Phase 6 v4 Gap 1: http_source 扩 method / path_params / query / RPC."""
from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.runtime import Data, Key, Source, wire
from app.runtime.http_source import register_http_sources
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring


@pytest.fixture(autouse=True)
def _isolate():
    clear_wiring()
    clear_bindings()
    yield
    clear_wiring()
    clear_bindings()


class _Ping(Data):
    name: Annotated[str, Key]
    class Meta:
        transient = True


class _Pong(Data):
    name: Annotated[str, Key]
    class Meta:
        transient = True


def test_http_source_get_with_query_param():
    """Source.http(method=GET) 把 query string 注入 Data。"""
    captured: list = []

    async def handler(p: _Ping) -> None:
        captured.append(p)

    wire(_Ping).from_(Source.http("/ping", method="GET")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.get("/ping?name=zoe")
    assert r.status_code == 202
    assert len(captured) == 1
    assert captured[0].name == "zoe"


def test_http_source_delete_with_path_param():
    """path 中 {name} 占位绑定 path param。"""
    captured: list = []

    async def handler(p: _Ping) -> None:
        captured.append(p)

    wire(_Ping).from_(Source.http("/items/{name}", method="DELETE")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.delete("/items/x")
    assert r.status_code == 202
    assert captured[0].name == "x"


def test_http_source_rpc_response_body():
    """response=True 时 node 返回值作为 HTTP response body 同步返回。"""
    async def handler(p: _Ping) -> _Pong:
        return _Pong(name=p.name + "_pong")

    wire(_Ping).from_(Source.http("/rpc", method="POST", response=True)).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.post("/rpc", json={"name": "ping"})
    assert r.status_code == 200
    assert r.json() == {"name": "ping_pong"}


def test_http_source_post_default_unchanged():
    """method 默认 POST + JSON body 行为跟原 36 行 http_source 等价。"""
    captured: list = []

    async def handler(p: _Ping) -> None:
        captured.append(p)

    wire(_Ping).from_(Source.http("/legacy")).to(handler)

    app = FastAPI()
    register_http_sources(app)
    client = TestClient(app)

    r = client.post("/legacy", json={"name": "old"})
    assert r.status_code == 202
    assert captured[0].name == "old"
```

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_http_source.py -v`
预期：全 FAIL（http_source.py 还没实现 method/path/query/response）。

- [ ] **Step 1.3: 改 `runtime/http_source.py`**

完整重写：

```python
"""Register HTTP-kind sources as FastAPI endpoints.

For each wire declaring ``.from_(Source.http(path, method=..., response=...))``,
we bind a route at ``path`` of the given HTTP method. Body / query / path params
are deserialized into the wire's ``Data`` type and emitted.

- method=POST/PUT: JSON body -> Data fields
- method=GET/DELETE: query string -> Data fields
- path "/x/{name}": path param ``name`` -> Data field ``name``
- response=True: node return value (a Data) is JSON-serialized as response body,
  status 200; emit awaits the consumer. Only valid when consumer is in-process.
- response=False (default): emit fire-and-forget, status 202.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from app.runtime.emit import emit
from app.runtime.wire import WIRING_REGISTRY

_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")


def _path_params(path: str) -> list[str]:
    return _PATH_PARAM_RE.findall(path)


def register_http_sources(app: FastAPI) -> None:
    """Attach a route per ``Source.http(...)`` source in WIRING_REGISTRY."""
    for w in WIRING_REGISTRY:
        for src in w.sources:
            if src.kind != "http":
                continue
            _bind_one(app, w, src)


def _bind_one(app: FastAPI, w, src) -> None:
    path = src.params["path"]
    method = src.params.get("method", "POST").upper()
    sync_response = src.params.get("response", False)
    data_cls = w.data_type
    path_params = _path_params(path)

    async def endpoint(req: Request, **path_kwargs: Any) -> Any:
        kwargs: dict[str, Any] = dict(path_kwargs)
        if method in {"POST", "PUT"}:
            try:
                body = await req.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                kwargs.update(body)
        else:  # GET / DELETE
            kwargs.update(dict(req.query_params))

        try:
            data_obj = data_cls(**kwargs)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if not sync_response:
            await emit(data_obj)
            return {"accepted": True}

        # RPC mode: emit to in-process consumer + return its result.
        result = await _emit_rpc(w, data_obj)
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return result

    # Build a function with explicit path-param signature so FastAPI binds them.
    if path_params:
        # FastAPI requires the path-param names to appear as positional kwargs.
        from inspect import Parameter, Signature

        params = [
            Parameter("req", Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        ] + [
            Parameter(name, Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
            for name in path_params
        ]
        endpoint.__signature__ = Signature(params)  # type: ignore[attr-defined]

    status_code = 200 if sync_response else 202
    if method == "GET":
        app.get(path, status_code=status_code)(endpoint)
    elif method == "POST":
        app.post(path, status_code=status_code)(endpoint)
    elif method == "PUT":
        app.put(path, status_code=status_code)(endpoint)
    elif method == "DELETE":
        app.delete(path, status_code=status_code)(endpoint)
    else:
        raise ValueError(f"unsupported HTTP method {method!r}")


async def _emit_rpc(w, data_obj):
    """RPC: only in-process consumer is supported. Return consumer's return value.

    For a single in-process consumer, run it directly so we can capture the
    return value (regular emit() drops returns). If the wire has multiple
    consumers, raise — RPC needs a single result.
    """
    if w.durable:
        raise RuntimeError(
            f"Source.http(response=True) cannot be combined with .durable() — "
            f"need single in-process consumer to capture return value"
        )
    consumers = list(w.consumers)
    if len(consumers) != 1:
        raise RuntimeError(
            f"Source.http(response=True) requires exactly 1 consumer, "
            f"got {len(consumers)} on {data_obj.__class__.__name__}"
        )
    return await consumers[0](data_obj)
```

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_http_source.py -v`
预期：全 PASS。

- [ ] **Step 1.4: 跑全量 runtime 测试**

Run: `cd apps/agent-service && uv run pytest tests/runtime/ -v 2>&1 | tail -10`
预期：全 PASS。

- [ ] **Step 1.5: ruff 检查**

Run: `cd apps/agent-service && uv run ruff check app/runtime/source.py app/runtime/http_source.py tests/runtime/test_http_source.py`
预期：0 报错。

- [ ] **Step 1.6: Commit**

```bash
git add apps/agent-service/app/runtime/source.py apps/agent-service/app/runtime/http_source.py apps/agent-service/tests/runtime/test_http_source.py
git commit -m "feat(runtime): http_source supports GET/DELETE/PUT/path_params/RPC

Source.http(path, method=, response=) — full HTTP capability surface:
- method GET/DELETE: deserialize query string into Data fields
- path '/x/{name}': bind path param name to Data field
- response=True: emit + await single in-process consumer + return its value
- response=False (default): fire-and-forget 202

Closes Gap 1 framework half. Business half (routes.py collapse) in Task 2.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §1 Gap 1"
```

---

## Task 2: Gap 1 business — routes.py 13 endpoint 收敛

**Files:**
- Modify: `apps/agent-service/app/api/routes.py`
- Modify: `apps/agent-service/app/main.py`（如 `register_http_sources` 调用位置需调整）
- Modify: `apps/agent-service/app/wiring/{life_dataflow,memory,memory_triggers,...}.py`（新增 `.from_(Source.http(...))` 边）
- Create: `apps/agent-service/app/wiring/admin.py`（专管 admin trigger 的 wire）
- Create: `apps/agent-service/app/nodes/admin.py`（专管 admin endpoint 的 RPC 节点：debug-glimpse / search / schedule CRUD）
- Modify: `apps/agent-service/app/domain/admin.py`（新建：AdminSearchRequest / DebugGlimpseRequest / ScheduleListRequest / ScheduleCreateRequest 等 Data）
- Test: `apps/agent-service/tests/wiring/test_admin_wiring.py`（新建）

### 子步

- [ ] **Step 2.1: 列出 13 个 endpoint 收敛后的目标 wire**

| 旧 endpoint | 新 wire | 模式 |
|---|---|---|
| `GET /` | 删（死代码）| — |
| `GET /health` | 保留手写（健康检查不走 framework）or `Source.http_health` builtin（v4 暂不引入 builtin） | 例外 |
| `POST /admin/trigger-life-engine-tick` | `wire(LifeTickRequest).from_(Source.http("/admin/trigger-life-engine-tick"))` | fire-and-forget |
| `POST /admin/trigger-glimpse` | 复杂（需要 fan-out 多 chat），改 `Source.http` 入口 + 节点 emit GlimpseRequest 多次 | fire-and-forget |
| `POST /admin/debug-glimpse` | `wire(DebugGlimpseRequest).from_(Source.http("/admin/debug-glimpse", method="GET", response=True))` | RPC |
| `POST /admin/trigger-voice` | `wire(VoiceRequest).from_(Source.http("/admin/trigger-voice"))` | fire-and-forget |
| `POST /admin/trigger-schedule` | `wire(DailyPlanRequest).from_(Source.http("/admin/trigger-schedule"))` | fire-and-forget |
| `POST /admin/search` | `wire(AdminSearchRequest).from_(Source.http("/admin/search", response=True))` | RPC |
| `GET /api/schedule` | `wire(ScheduleListRequest).from_(Source.http("/api/schedule", method="GET", response=True))` | RPC |
| `GET /api/schedule/current` | `wire(ScheduleCurrentRequest).from_(Source.http("/api/schedule/current", method="GET", response=True))` | RPC |
| `GET /api/schedule/daily/{target_date}` | `wire(ScheduleDailyRequest).from_(Source.http("/api/schedule/daily/{target_date}", method="GET", response=True))` | RPC + path param |
| `POST /api/schedule` | `wire(ScheduleCreateRequest).from_(Source.http("/api/schedule", response=True))` | RPC |
| `DELETE /api/schedule/{schedule_id}` | `wire(ScheduleDeleteRequest).from_(Source.http("/api/schedule/{schedule_id}", method="DELETE", response=True))` | RPC + path param |

健康检查 (`/health`) 是约定俗成例外，spec §1 Gap 1 允许保留（infrastructure 用，跟 dataflow 抽象无关）。

- [ ] **Step 2.2: 新建 `app/domain/admin.py` Data classes**

```python
"""Admin / public API request Data — for HTTP source RPC endpoints.

These Data classes represent admin / API requests; they're transient
(no DB row), wire to nodes/admin.py handlers via Source.http(response=True).
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class DebugGlimpseRequest(Data):
    persona_id: Annotated[str, Key]
    class Meta:
        transient = True


class AdminSearchRequest(Data):
    queries: list[str]
    num: int = 5
    class Meta:
        transient = True


class ScheduleListRequest(Data):
    plan_type: str | None = None
    persona_id: Annotated[str | None, Key] = None
    active_only: bool = True
    limit: int = 50
    class Meta:
        transient = True


class ScheduleCurrentRequest(Data):
    persona_id: Annotated[str, Key]
    class Meta:
        transient = True


class ScheduleDailyRequest(Data):
    target_date: Annotated[str, Key]
    persona_id: str = ""
    class Meta:
        transient = True


class ScheduleCreateRequest(Data):
    persona_id: Annotated[str, Key]
    plan_type: str
    period_start: str
    period_end: str
    time_start: str | None = None
    time_end: str | None = None
    content: str = ""
    mood: str | None = None
    energy_level: int | None = None
    response_style_hint: str | None = None
    proactive_action: dict | None = None
    target_chats: list | None = None
    model: str | None = None
    is_active: bool = True
    class Meta:
        transient = True


class ScheduleDeleteRequest(Data):
    schedule_id: Annotated[int, Key]
    class Meta:
        transient = True


class TriggerGlimpseRequest(Data):
    persona_id: Annotated[str, Key]
    class Meta:
        transient = True
```

- [ ] **Step 2.3: 新建 `app/nodes/admin.py` 节点**

每个节点对应一个 admin endpoint，负责：
- RPC: 查 / 写 DB + 返回 dict 或 ResponseData
- fire-and-forget: emit 下游 Data（如 trigger-glimpse 节点 emit 多个 GlimpseRequest）

```python
"""Admin / public API @nodes — handle HTTP request, query/write, emit downstream.

Per Phase 6 v4 Gap 1 closure: every endpoint goes through Source.http wiring;
nodes/admin.py owns the business of converting request Data to response or
to fan-out emit.
"""
from __future__ import annotations

from typing import Any

from app.data.queries import (
    delete_schedule,
    list_schedules,
    upsert_schedule,
    find_daily_entries,
)
from app.data.session import get_session
from app.data.models import AkaoSchedule
from app.domain.admin import (
    AdminSearchRequest,
    DebugGlimpseRequest,
    ScheduleCreateRequest,
    ScheduleCurrentRequest,
    ScheduleDailyRequest,
    ScheduleDeleteRequest,
    ScheduleListRequest,
    TriggerGlimpseRequest,
)
from app.domain.life_dataflow import GlimpseRequest
from app.runtime import emit, node


@node
async def admin_debug_glimpse(req: DebugGlimpseRequest) -> dict:
    """Read-only debug — return pipeline state per chat without LLM call."""
    from app.data import queries as Q
    from app.life.glimpse import _now_cst, list_target_groups
    from app.life.proactive import get_unseen_messages

    now = _now_cst()
    groups_info = []
    for chat_id in list_target_groups():
        async with get_session() as s:
            state = await Q.find_latest_glimpse_state(s, req.persona_id, chat_id)
        last_seen = state.last_seen_msg_time if state else 0
        last_obs = (state.observation if state else "")[:100]

        async with get_session() as s:
            bot_reply_time = await Q.find_last_bot_reply_time(s, chat_id)
        effective_after = max(last_seen, bot_reply_time)
        messages = await get_unseen_messages(chat_id, after=effective_after)

        groups_info.append({
            "chat_id": chat_id,
            "last_seen_msg_time": last_seen,
            "last_observation": last_obs,
            "bot_reply_time": bot_reply_time,
            "effective_after": effective_after,
            "unseen_message_count": len(messages),
            "first_msg_time": messages[0].create_time if messages else None,
            "last_msg_time": messages[-1].create_time if messages else None,
        })
    return {"now_cst": now.isoformat(), "groups": groups_info}


@node
async def admin_search(req: AdminSearchRequest) -> dict:
    from app.agent.tools.search import _you_search
    from app.infra.config import settings
    from fastapi import HTTPException
    if not settings.you_search_host:
        raise HTTPException(503, "You Search API not configured")
    results: dict[str, Any] = {}
    for query in req.queries:
        try:
            hits = await _you_search(query, req.num, "CN", "ZH-HANS")
            results[query] = hits
        except Exception as e:
            results[query] = {"error": str(e)}
    return results


@node
async def admin_trigger_glimpse(req: TriggerGlimpseRequest) -> None:
    from app.life.glimpse import list_target_groups
    for chat_id in list_target_groups():
        await emit(GlimpseRequest(persona_id=req.persona_id, chat_id=chat_id, request_id=...))
    # request_id 生成参考 nodes/life_dataflow.py:_new_glimpse_request


@node
async def admin_list_schedules(req: ScheduleListRequest) -> list[dict]:
    async with get_session() as s:
        entries = await list_schedules(
            s,
            plan_type=req.plan_type,
            persona_id=req.persona_id,
            active_only=req.active_only,
            limit=req.limit,
        )
    return [_to_out(e) for e in entries]


@node
async def admin_current_schedule(req: ScheduleCurrentRequest) -> dict:
    from app.life.schedule import build_schedule_context
    return {"context": await build_schedule_context(req.persona_id)}


@node
async def admin_daily_entries(req: ScheduleDailyRequest) -> list[dict]:
    async with get_session() as s:
        entries = await find_daily_entries(s, req.target_date, req.persona_id)
    return [_to_out(e) for e in entries]


@node
async def admin_create_schedule(req: ScheduleCreateRequest) -> dict:
    from fastapi import HTTPException
    if req.plan_type not in ("monthly", "weekly", "daily"):
        raise HTTPException(400, "plan_type must be monthly, weekly, or daily")
    if req.plan_type == "daily" and (not req.time_start or not req.time_end):
        raise HTTPException(400, "daily entries require time_start and time_end")
    entry = AkaoSchedule(
        persona_id=req.persona_id,
        plan_type=req.plan_type,
        period_start=req.period_start,
        period_end=req.period_end,
        time_start=req.time_start,
        time_end=req.time_end,
        content=req.content,
        mood=req.mood,
        energy_level=req.energy_level,
        response_style_hint=req.response_style_hint,
        proactive_action=req.proactive_action,
        target_chats=req.target_chats,
        model=req.model,
        is_active=req.is_active,
    )
    async with get_session() as s:
        saved = await upsert_schedule(s, entry)
    return _to_out(saved)


@node
async def admin_delete_schedule(req: ScheduleDeleteRequest) -> dict:
    from fastapi import HTTPException
    async with get_session() as s:
        ok = await delete_schedule(s, req.schedule_id)
    if not ok:
        raise HTTPException(404, "Schedule entry not found")
    return {"ok": True}


def _to_out(entry: AkaoSchedule) -> dict:
    """ScheduleOut 序列化（沿用 routes.py 原 ScheduleOut 模型）。"""
    return {
        "id": entry.id,
        "persona_id": entry.persona_id,
        "plan_type": entry.plan_type,
        "period_start": entry.period_start,
        "period_end": entry.period_end,
        "time_start": entry.time_start,
        "time_end": entry.time_end,
        "content": entry.content,
        "mood": entry.mood,
        "energy_level": entry.energy_level,
        "response_style_hint": entry.response_style_hint,
        "proactive_action": entry.proactive_action,
        "target_chats": entry.target_chats,
        "model": entry.model,
        "is_active": entry.is_active,
    }
```

- [ ] **Step 2.4: 新建 `app/wiring/admin.py`**

```python
"""Admin / public API HTTP wiring — Source.http for every endpoint.

Phase 6 v4 Gap 1 closure: collapses api/routes.py 13 endpoints into wire
declarations; runtime/http_source.py auto-registers FastAPI routes.
"""
from app.domain.admin import (
    AdminSearchRequest,
    DebugGlimpseRequest,
    ScheduleCreateRequest,
    ScheduleCurrentRequest,
    ScheduleDailyRequest,
    ScheduleDeleteRequest,
    ScheduleListRequest,
    TriggerGlimpseRequest,
)
from app.domain.life_dataflow import DailyPlanRequest, LifeTickRequest, VoiceRequest
from app.nodes.admin import (
    admin_create_schedule,
    admin_current_schedule,
    admin_daily_entries,
    admin_debug_glimpse,
    admin_delete_schedule,
    admin_list_schedules,
    admin_search,
    admin_trigger_glimpse,
)
from app.runtime import Source, wire

# Fire-and-forget admin triggers — emit existing life_dataflow Data classes.
wire(LifeTickRequest).from_(Source.http("/admin/trigger-life-engine-tick"))
wire(VoiceRequest).from_(Source.http("/admin/trigger-voice"))
wire(DailyPlanRequest).from_(Source.http("/admin/trigger-schedule"))
wire(TriggerGlimpseRequest).from_(Source.http("/admin/trigger-glimpse")).to(admin_trigger_glimpse)

# RPC endpoints — node returns response body directly.
wire(DebugGlimpseRequest).from_(Source.http("/admin/debug-glimpse", method="GET", response=True)).to(admin_debug_glimpse)
wire(AdminSearchRequest).from_(Source.http("/admin/search", response=True)).to(admin_search)
wire(ScheduleListRequest).from_(Source.http("/api/schedule", method="GET", response=True)).to(admin_list_schedules)
wire(ScheduleCurrentRequest).from_(Source.http("/api/schedule/current", method="GET", response=True)).to(admin_current_schedule)
wire(ScheduleDailyRequest).from_(Source.http("/api/schedule/daily/{target_date}", method="GET", response=True)).to(admin_daily_entries)
wire(ScheduleCreateRequest).from_(Source.http("/api/schedule", response=True)).to(admin_create_schedule)
wire(ScheduleDeleteRequest).from_(Source.http("/api/schedule/{schedule_id}", method="DELETE", response=True)).to(admin_delete_schedule)
```

- [ ] **Step 2.5: 改 `main.py` import 新 wiring**

`app/main.py`：

```python
# 新增 import：
import app.wiring.admin  # noqa: F401  -- registers Source.http wires
```

确保 `register_http_sources(app)` 在 main.py 已经调用（v4 Task 1 的 framework 改动应已包含；查 main.py 现状）。

- [ ] **Step 2.6: 收缩 `routes.py`**

完整重写为：

```python
"""API routes — health only.

All admin / API endpoints are now declared via Source.http in
app/wiring/admin.py and registered automatically by
register_http_sources(app) called from main.py.
"""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", tags=["Health"])
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "service": "agent-service",
        "version": os.environ.get("GIT_SHA", "unknown"),
    }
```

行数 ~20。

- [ ] **Step 2.7: 写 wiring 测试**

`tests/wiring/test_admin_wiring.py`：

```python
"""Phase 6 v4 Gap 1 acceptance: admin wiring 注册 13 个 HTTP source。"""
from __future__ import annotations

import importlib

from fastapi import FastAPI


def _reload_admin_wiring():
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring
    clear_wiring()
    clear_bindings()
    import app.wiring.admin
    importlib.reload(app.wiring.admin)


def test_admin_wiring_registers_all_paths():
    _reload_admin_wiring()
    from app.runtime.http_source import register_http_sources

    app = FastAPI()
    register_http_sources(app)

    paths = {(r.path, list(r.methods - {"HEAD"})[0]) for r in app.routes}
    expected = {
        ("/admin/trigger-life-engine-tick", "POST"),
        ("/admin/trigger-voice", "POST"),
        ("/admin/trigger-schedule", "POST"),
        ("/admin/trigger-glimpse", "POST"),
        ("/admin/debug-glimpse", "GET"),
        ("/admin/search", "POST"),
        ("/api/schedule", "GET"),
        ("/api/schedule/current", "GET"),
        ("/api/schedule/daily/{target_date}", "GET"),
        ("/api/schedule", "POST"),
        ("/api/schedule/{schedule_id}", "DELETE"),
    }
    for p in expected:
        assert p in paths, f"missing wire: {p}"


def test_routes_py_only_health():
    """routes.py 不能再有 admin/api endpoint。"""
    import app.api.routes as r
    paths = {route.path for route in r.router.routes}
    assert "/health" in paths
    forbidden = {p for p in paths if p.startswith(("/admin/", "/api/"))}
    assert not forbidden, f"routes.py still has framework-eligible paths: {forbidden}"
```

- [ ] **Step 2.8: 跑测试 + ruff**

```bash
cd apps/agent-service && uv run pytest tests/wiring/test_admin_wiring.py tests/wiring/ -v 2>&1 | tail -10
uv run ruff check app/api/routes.py app/wiring/admin.py app/nodes/admin.py app/domain/admin.py
```

- [ ] **Step 2.9: 跑全量 agent-service 测试**

Run: `cd apps/agent-service && uv run pytest 2>&1 | tail -8`
预期：全 PASS。如有 fail，多半是某个 e2e / integration 测试在调老 routes.py 的具体 endpoint，用 Source.http 替代后行为应等价（path / method 不变）。

- [ ] **Step 2.10: grep 验收**

```bash
grep -rn "@router\.\|@app\." apps/agent-service/app/ | grep -v "@app.post\|@app.get\|@app.put\|@app.delete" | grep -v http_source
# 预期：仅 @router.get "/health" 在 routes.py 命中
```

- [ ] **Step 2.11: Commit**

```bash
git add apps/agent-service/app/api/routes.py apps/agent-service/app/wiring/admin.py apps/agent-service/app/nodes/admin.py apps/agent-service/app/domain/admin.py apps/agent-service/app/main.py apps/agent-service/tests/wiring/test_admin_wiring.py
git commit -m "refactor(api): collapse routes.py 13 endpoints into Source.http wiring

12 admin / API endpoints declared in app/wiring/admin.py via Source.http;
runtime auto-registers FastAPI routes through register_http_sources(app).
routes.py shrinks 292 -> ~20 lines (only /health remains as infra exception).

Adds:
- app/domain/admin.py — request Data classes for admin endpoints
- app/nodes/admin.py — RPC nodes that handle DB queries / writes / fan-outs
- app/wiring/admin.py — wire(...).from_(Source.http(...)) declarations

Closes Gap 1 business half. Together with Task 1 (framework half), the
Gap 1 acceptance gate now holds: zero @router.* in business code.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §2.1"
```

---

## Task 3: Gap 2 framework — emit 跨进程 dispatch via wire source.mq

**Files:**
- Modify: `apps/agent-service/app/runtime/emit.py`
- Modify: `apps/agent-service/app/runtime/wire.py`（如需在 wire spec 内记录 source.mq 信息以便 emit 反查；现状 sources 已在 wire 上）
- Test: `apps/agent-service/tests/runtime/test_emit_cross_process.py`（新建）

### 子步

- [ ] **Step 3.1: 写 framework 单测（TDD red）**

`tests/runtime/test_emit_cross_process.py`：

```python
"""Phase 6 v4 Gap 2: emit 跨进程通过 wire source.mq 自动 publish。"""
from __future__ import annotations

import os
from typing import Annotated
from unittest.mock import AsyncMock

import pytest

from app.runtime import Data, Key, Source, bind, emit, wire
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    clear_wiring()
    clear_bindings()
    yield
    clear_wiring()
    clear_bindings()


class _XReq(Data):
    x_id: Annotated[str, Key]
    class Meta:
        transient = True


@pytest.mark.asyncio
async def test_emit_publishes_to_mq_when_consumer_in_other_app(monkeypatch):
    """consumer bind 到非本进程 app + wire source 是 mq → emit 自动 mq.publish。"""

    async def x_handler(r: _XReq) -> None:
        pass

    wire(_XReq).to(x_handler).from_(Source.mq("x_queue"))
    bind(x_handler).to_app("vectorize-worker")

    # Pretend we're running as agent-service.
    monkeypatch.setenv("APP_NAME", "agent-service")

    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.emit._mq_publish_for_source", fake_publish)

    from app.runtime.emit import reset_emit_runtime
    reset_emit_runtime()

    await emit(_XReq(x_id="x1"))

    fake_publish.assert_awaited_once()
    args = fake_publish.await_args.args
    # Expected signature: _mq_publish_for_source(src, data)
    assert args[0].kind == "mq"
    assert args[0].params["queue"] == "x_queue"
    assert args[1].x_id == "x1"


@pytest.mark.asyncio
async def test_emit_inprocess_when_consumer_in_same_app(monkeypatch):
    """consumer 在本 app（fall through default）→ in-process 调，不 publish。"""
    captured: list = []

    async def x_handler(r: _XReq) -> None:
        captured.append(r)

    wire(_XReq).to(x_handler).from_(Source.mq("x_queue"))
    # NOT binding x_handler -> falls through to default agent-service.

    monkeypatch.setenv("APP_NAME", "agent-service")
    fake_publish = AsyncMock()
    monkeypatch.setattr("app.runtime.emit._mq_publish_for_source", fake_publish)

    from app.runtime.emit import reset_emit_runtime
    reset_emit_runtime()

    await emit(_XReq(x_id="x2"))

    fake_publish.assert_not_called()
    assert len(captured) == 1
    assert captured[0].x_id == "x2"
```

Run: `cd apps/agent-service && uv run pytest tests/runtime/test_emit_cross_process.py -v`
预期：全 FAIL（emit 还没跨进程逻辑）。

- [ ] **Step 3.2: 改 `runtime/emit.py`**

在 `emit()` 的 wire loop 内加跨进程分支：

```python
# 在 emit.py:84 附近（in-process consumer dispatch 之前）
for c in w.consumers:
    if w.durable:
        from app.runtime.durable import publish_durable
        await publish_durable(w, c, data)
        continue

    if c not in own_nodes:
        # Cross-process: if wire has a Source.mq, publish to that queue.
        mq_src = next((s for s in w.sources if s.kind == "mq"), None)
        if mq_src is not None:
            await _mq_publish_for_source(mq_src, data)
            continue
        # Else: silently skip (preserves prior behavior)
        continue

    kwargs = await _resolve_inputs(c, data, w)
    await c(**kwargs)
```

新增 helper：

```python
async def _mq_publish_for_source(src, data: Data) -> None:
    """Publish data body to the mq queue declared by src.

    Mirrors source.mq consumer-side decoding: body is data.model_dump().
    Lane-aware queue resolution via current_lane().
    """
    from app.infra.rabbitmq import lane_queue, mq, current_lane

    queue = lane_queue(src.params["queue"], current_lane())
    body = data.model_dump()
    # Use a default exchange / routing key matching engine.py source.mq loop.
    await mq.publish_raw(queue=queue, body=body)
```

注意：需要看 `app/infra/rabbitmq.py` 的现有 publish API 是 `mq.publish(Route, body)` 还是 `mq.publish_raw(queue=, body=)`；可能需要新增 `publish_raw` API。如果 `mq` 的现有 API 只有 `publish(Route, ...)`，就用一个临时构造 Route：

```python
from app.infra.rabbitmq import Route, mq, current_lane
queue = src.params["queue"]
route = Route(name=queue, ...)  # 看 Route 的字段；可能需要 exchange + routing_key
await mq.publish(route, body)
```

实际实现以 `infra/rabbitmq.py` 现状为准，落地时 grep 决定。

- [ ] **Step 3.3: 跑测试**

```bash
cd apps/agent-service && uv run pytest tests/runtime/test_emit_cross_process.py tests/runtime/test_emit_inprocess.py -v
```

预期：跨进程 + in-process 都 PASS。

- [ ] **Step 3.4: 跑全量 runtime 测试**

```bash
uv run pytest tests/runtime/ -v 2>&1 | tail -10
```

- [ ] **Step 3.5: Commit**

```bash
git add apps/agent-service/app/runtime/emit.py apps/agent-service/tests/runtime/test_emit_cross_process.py
git commit -m "feat(runtime): emit cross-process dispatch via wire source.mq

When a consumer is bound to a different app (out-of-process), emit() now
checks if the wire has Source.mq(...) — if so, publishes the data body
to that mq queue (lane-aware). This makes emit() a uniform publisher
API regardless of in-process vs cross-process consumer placement.

Closes Gap 2 framework half. Business half (delete enqueue_* helpers,
callers switch to emit) in Task 4.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §1 Gap 2"
```

---

## Task 4: Gap 2 business — vectorize_memory enqueue helpers 删，调用方改 emit

**Files:**
- Modify: `apps/agent-service/app/memory/vectorize_memory.py`
- Modify: `apps/agent-service/app/agent/tools/commit_abstract.py`
- Modify: `apps/agent-service/app/life/glimpse.py`
- Modify: `apps/agent-service/app/nodes/memory_pipelines.py`
- Test: 现有 `tests/unit/agent/tools/test_commit_abstract.py`、`tests/unit/life/test_glimpse.py`、`tests/nodes/test_memory_pipelines_helpers.py` 改 patch 路径

### 子步

- [ ] **Step 4.1: 删 `enqueue_fragment_vectorize` / `enqueue_abstract_vectorize`**

`memory/vectorize_memory.py` 删除 `enqueue_fragment_vectorize` 和 `enqueue_abstract_vectorize` 两个函数（line 104-123）。

- [ ] **Step 4.2: 改调用方**

每个调用方从 `await enqueue_fragment_vectorize(fid)` 改为 `await emit(MemoryFragmentRequest(fragment_id=fid))`。

- `agent/tools/commit_abstract.py:62`：
  ```python
  # 旧：await enqueue_abstract_vectorize(aid)
  from app.domain.memory_request import MemoryAbstractRequest
  from app.runtime import emit
  await emit(MemoryAbstractRequest(abstract_id=aid))
  ```
- `life/glimpse.py:241`：
  ```python
  # 旧：await enqueue_fragment_vectorize(fid)
  from app.domain.memory_request import MemoryFragmentRequest
  from app.runtime import emit
  await emit(MemoryFragmentRequest(fragment_id=fid))
  ```
- `nodes/memory_pipelines.py:201`：同 glimpse 做法。

import 顶部清理：删 `from app.memory.vectorize_memory import enqueue_*`。

- [ ] **Step 4.3: 改测试 patch 路径**

现有测试 patch `app.agent.tools.commit_abstract.enqueue_abstract_vectorize` / `app.nodes.memory_pipelines.enqueue_fragment_vectorize` 等，改成 patch `app.runtime.emit`：

```python
# 旧：patch("app.agent.tools.commit_abstract.enqueue_abstract_vectorize", new=AsyncMock())
# 新：patch("app.runtime.emit", new=AsyncMock()) as mock_emit
# 然后断言 mock_emit.await_args_list 含 MemoryAbstractRequest(abstract_id=...)
```

- [ ] **Step 4.4: 跑测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/agent/tools/test_commit_abstract.py tests/unit/life/test_glimpse.py tests/nodes/test_memory_pipelines_helpers.py -v 2>&1 | tail -15
```

- [ ] **Step 4.5: 跑全量**

```bash
uv run pytest 2>&1 | tail -8
```

- [ ] **Step 4.6: 验收 grep**

```bash
grep -n "enqueue_fragment_vectorize\|enqueue_abstract_vectorize" apps/agent-service/app/
# 预期：0 命中（除 vectorize_memory.py 已删的）

grep -n "mq.publish" apps/agent-service/app/memory/
# 预期：0 命中（vectorize_memory.py 删 enqueue 后无残留）
```

- [ ] **Step 4.7: Commit**

```bash
git add apps/agent-service/app/memory/vectorize_memory.py apps/agent-service/app/agent/tools/commit_abstract.py apps/agent-service/app/life/glimpse.py apps/agent-service/app/nodes/memory_pipelines.py apps/agent-service/tests/
git commit -m "refactor(memory): delete enqueue_* helpers, callers use emit()

Phase 6 v3 §0 deferred this knife because emit() didn't support
cross-process dispatch; v4 Task 3 added that capability, so callers
(commit_abstract / glimpse / memory_pipelines) now uniformly use
emit(MemoryFragmentRequest(...)) / emit(MemoryAbstractRequest(...)),
and the enqueue_* wrappers in app/memory/vectorize_memory.py are removed.

Closes Gap 2 business half.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §2.2"
```

---

## Task 5: Gap 3 framework — 新增 AbstractMemoryCommitted / ScheduleRevisionCreated / NoteCreated Data + wires

**Files:**
- Create: `apps/agent-service/app/domain/agent_tool_events.py`
- Modify: `apps/agent-service/app/wiring/memory.py`（add wire for AbstractMemoryCommitted）
- Modify: `apps/agent-service/app/wiring/life_dataflow.py`（add wire for ScheduleRevisionCreated → sync_life_state_node — Task 7 实施）
- Test: `apps/agent-service/tests/domain/test_agent_tool_events.py`

### 子步

- [ ] **Step 5.1: 创建 `app/domain/agent_tool_events.py`**

```python
"""Agent tool side-effect events.

Each mutation tool (commit_abstract / commit_life_state / update_schedule /
notes) writes DB then emits one of these Data classes; downstream nodes
react via wire (vectorize / state-sync / reviewer / etc).

All transient — these are events, not durable rows; the underlying DB row
(AbstractMemory / ScheduleRevision / Note) is the source of truth.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class AbstractMemoryCommitted(Data):
    """commit_abstract tool wrote an abstract memory + edges."""
    abstract_id: Annotated[str, Key]
    persona_id: str
    chat_id: str | None = None
    class Meta:
        transient = True


class ScheduleRevisionCreated(Data):
    """update_schedule tool wrote a schedule_revision row."""
    revision_id: Annotated[str, Key]
    persona_id: str
    class Meta:
        transient = True


class NoteCreated(Data):
    """notes tool created a new note."""
    note_id: Annotated[str, Key]
    persona_id: str
    class Meta:
        transient = True
```

- [ ] **Step 5.2: 写 domain test**

`tests/domain/test_agent_tool_events.py`：

```python
"""Phase 6 v4 Gap 3: agent tool event Data classes."""
def test_data_classes_register():
    from app.domain.agent_tool_events import (
        AbstractMemoryCommitted,
        NoteCreated,
        ScheduleRevisionCreated,
    )
    from app.runtime.data import DATA_REGISTRY
    assert AbstractMemoryCommitted in DATA_REGISTRY
    assert ScheduleRevisionCreated in DATA_REGISTRY
    assert NoteCreated in DATA_REGISTRY


def test_abstract_committed_required_fields():
    from app.domain.agent_tool_events import AbstractMemoryCommitted
    e = AbstractMemoryCommitted(abstract_id="a_1", persona_id="akao-001")
    assert e.abstract_id == "a_1"
    assert e.chat_id is None
```

Run: `uv run pytest tests/domain/test_agent_tool_events.py -v`
预期：PASS。

- [ ] **Step 5.3: wire AbstractMemoryCommitted → vectorize**

`app/wiring/memory.py` 加：

```python
from app.domain.agent_tool_events import AbstractMemoryCommitted
from app.domain.memory_request import MemoryAbstractRequest
from app.nodes.memory_pipelines import on_abstract_committed  # Task 6 实施
# wire: tool 写 abstract 后 emit AbstractMemoryCommitted -> 同步 in-process
#       node on_abstract_committed 再 emit MemoryAbstractRequest 触发 vectorize
wire(AbstractMemoryCommitted).to(on_abstract_committed)
```

`nodes/memory_pipelines.py` 加：

```python
from app.domain.agent_tool_events import AbstractMemoryCommitted
from app.domain.memory_request import MemoryAbstractRequest
from app.runtime import emit, node


@node
async def on_abstract_committed(e: AbstractMemoryCommitted) -> None:
    """commit_abstract tool 写 abstract 后 emit AbstractMemoryCommitted；
    本 node 转 emit MemoryAbstractRequest 触发 vectorize-worker。
    后续可加 reviewer 通知 / dirty cache invalidation 等下游。"""
    await emit(MemoryAbstractRequest(abstract_id=e.abstract_id))
```

- [ ] **Step 5.4: 跑 wiring test**

`tests/wiring/test_memory.py` 加：

```python
def test_abstract_committed_wired():
    from app.domain.agent_tool_events import AbstractMemoryCommitted
    from app.runtime.wire import WIRING_REGISTRY
    wires = [w for w in WIRING_REGISTRY if w.data_type is AbstractMemoryCommitted]
    assert len(wires) == 1
```

- [ ] **Step 5.5: ScheduleRevisionCreated wire 留在 Task 7 一起做（依赖 sync_life_state_node）**

NoteCreated wire：本 task 创建 Data class，但 wire（如 reviewer 订阅 NoteCreated）按 spec §3.4 评估再决定，暂不强加 wire。

- [ ] **Step 5.6: Commit**

```bash
git add apps/agent-service/app/domain/agent_tool_events.py apps/agent-service/app/wiring/memory.py apps/agent-service/app/nodes/memory_pipelines.py apps/agent-service/tests/domain/test_agent_tool_events.py apps/agent-service/tests/wiring/test_memory.py
git commit -m "feat(domain): add agent tool side-effect Data classes

Three tool-effect events:
- AbstractMemoryCommitted: commit_abstract tool
- ScheduleRevisionCreated: update_schedule tool (wired in Task 7)
- NoteCreated: notes tool

AbstractMemoryCommitted wires to on_abstract_committed node which then
emits MemoryAbstractRequest. Adds an in-graph layer between tool and
vectorize so future downstream subscribers (reviewer, dirty cache)
plug in via wire instead of patching the tool.

Closes Gap 3 framework half. Tool body changes in Task 6.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §1 Gap 3"
```

---

## Task 6: Gap 3 business — agent tools 写 DB 后 emit Data

**Files:**
- Modify: `apps/agent-service/app/agent/tools/commit_abstract.py`
- Modify: `apps/agent-service/app/agent/tools/notes.py`
- (`update_schedule.py` 在 Task 8 做，跟 arq 退场一起)
- Test: 现有 tool 测试加 emit 顺序断言

### 子步

- [ ] **Step 6.1: `commit_abstract.py` 改 emit AbstractMemoryCommitted**

替换原 `await enqueue_abstract_vectorize(aid)`（Task 4 已改成 emit MemoryAbstractRequest）为：

```python
from app.domain.agent_tool_events import AbstractMemoryCommitted
# ... DB write ...
await emit(AbstractMemoryCommitted(
    abstract_id=aid,
    persona_id=persona_id,
    chat_id=chat_id,
))
```

注意：emit 必须在 `get_session()` block 外（事务 commit 后）。

- [ ] **Step 6.2: `notes.py` 改 emit NoteCreated**

写 note 后：

```python
from app.domain.agent_tool_events import NoteCreated
# ... insert_note ...
await emit(NoteCreated(note_id=note_id, persona_id=persona_id))
```

- [ ] **Step 6.3: 改测试 — 断言 emit 顺序**

`tests/unit/agent/tools/test_commit_abstract.py`：删 patch enqueue_abstract_vectorize，加 patch emit + 断言两次 emit（先 AbstractMemoryCommitted 再 MemoryAbstractRequest 由 on_abstract_committed node 转发；具体看 wire 在 in-process 还是单独跑）。

实际上 v4 设计是 commit_abstract 只 emit AbstractMemoryCommitted；on_abstract_committed node（in-process）再 emit MemoryAbstractRequest。所以测试 patch emit 时会捕到两次（因为 wire 是同一进程 in-process dispatch，emit 链路是同步的）。

- [ ] **Step 6.4: 跑测试 + ruff**

```bash
cd apps/agent-service && uv run pytest tests/unit/agent/tools/ tests/wiring/test_memory.py -v 2>&1 | tail -10
uv run ruff check app/agent/tools/
```

- [ ] **Step 6.5: 验收 grep**

```bash
grep -rn "mq.publish\|enqueue_job\|create_pool" apps/agent-service/app/agent/
# 预期：仅 update_schedule.py 命中（Task 8 收尾）
```

- [ ] **Step 6.6: Commit**

```bash
git add apps/agent-service/app/agent/tools/commit_abstract.py apps/agent-service/app/agent/tools/notes.py apps/agent-service/tests/
git commit -m "refactor(agent): tool writes emit Data after DB commit

commit_abstract emits AbstractMemoryCommitted; notes emits NoteCreated.
Tools no longer call enqueue_* helpers or arq enqueue_job — side effects
declared via emit + wire. update_schedule deferred to Task 8 (paired
with arq retirement).

Closes Gap 3 business half (except update_schedule).

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §2.3"
```

---

## Task 7: Gap 4 framework — sync_life_state_node + wire(ScheduleRevisionCreated)

**Files:**
- Create: `apps/agent-service/app/nodes/sync_life_state.py`
- Modify: `apps/agent-service/app/wiring/life_dataflow.py`
- Test: `apps/agent-service/tests/nodes/test_sync_life_state.py`

### 子步

- [ ] **Step 7.1: 创建 `app/nodes/sync_life_state.py`**

把 `app/workers/state_sync_worker.py` 的 `sync_life_state_after_schedule` 函数搬过来改 dataflow node：

```python
"""sync_life_state_node — react to ScheduleRevisionCreated by re-evaluating
life-engine state.

Replaces app/workers/state_sync_worker.py (arq worker). Wired in
app/wiring/life_dataflow.py via wire(ScheduleRevisionCreated).durable().
Bind to event-worker app (Task 8) so it runs out-of-process.
"""
from __future__ import annotations

import logging

from app.domain.agent_tool_events import ScheduleRevisionCreated
from app.runtime import node

logger = logging.getLogger(__name__)


@node
async def sync_life_state_node(e: ScheduleRevisionCreated) -> None:
    from app.life.state_sync import refresh_life_state_after_schedule
    await refresh_life_state_after_schedule(revision_id=e.revision_id)
```

把 `state_sync_worker.py` 里实际处理逻辑（如果是直接调 life.state_sync 函数）保留在 `life/state_sync.py` 不动；node 是 thin shim。

- [ ] **Step 7.2: wire — durable 还是 in-process？**

`wiring/life_dataflow.py` 加：

```python
from app.domain.agent_tool_events import ScheduleRevisionCreated
from app.nodes.sync_life_state import sync_life_state_node

wire(ScheduleRevisionCreated).to(sync_life_state_node).durable()
```

`.durable()` 让 emit 在 update_schedule tool 的 agent-service 进程 publish 到 mq；sync_life_state_node 在 event-worker 进程消费。

- [ ] **Step 7.3: bind sync_life_state_node 到 event-worker**

`app/deployment.py` 加：

```python
from app.nodes.sync_life_state import sync_life_state_node
bind(sync_life_state_node).to_app("event-worker")
```

注意：v4 之前没有 event-worker 这个 app。Task 8 会引入；本 task 暂时 bind agent-service（fall through）让测试通过，Task 8 再改。

实际上 ScheduleRevisionCreated 的 wire 是 durable 的，consumer 必须 bind 到一个明确的 app；如果 fall-through 到 agent-service，agent-service 跑 durable wire 也合法（PaaS App 同时是 HTTP 服务和 durable consumer）。简化：bind sync_life_state_node 到 agent-service，跑 durable consumer 在 agent-service 进程。Task 8 再决定是不是开 event-worker。

`deployment.py` 简化：

```python
from app.nodes.sync_life_state import sync_life_state_node
bind(sync_life_state_node).to_app("agent-service")  # 显式落在主进程
```

- [ ] **Step 7.4: 写测试**

`tests/nodes/test_sync_life_state.py`：

```python
"""Phase 6 v4 Gap 4: sync_life_state_node 替代 arq state_sync_worker。"""
import pytest
from unittest.mock import AsyncMock, patch

from app.domain.agent_tool_events import ScheduleRevisionCreated
from app.nodes.sync_life_state import sync_life_state_node


@pytest.mark.asyncio
async def test_sync_life_state_calls_refresh():
    e = ScheduleRevisionCreated(revision_id="sr_1", persona_id="akao-001")
    fake_refresh = AsyncMock()
    with patch("app.life.state_sync.refresh_life_state_after_schedule", fake_refresh):
        await sync_life_state_node(e)
    fake_refresh.assert_awaited_once_with(revision_id="sr_1")
```

注意：state_sync.py 现有函数名可能叫别的（看 grep 结果），实施时 verify 实际函数名替换。

- [ ] **Step 7.5: 跑测试 + ruff**

```bash
cd apps/agent-service && uv run pytest tests/nodes/test_sync_life_state.py tests/wiring/ -v 2>&1 | tail -10
uv run ruff check app/nodes/sync_life_state.py app/wiring/life_dataflow.py app/deployment.py
```

- [ ] **Step 7.6: Commit**

```bash
git add apps/agent-service/app/nodes/sync_life_state.py apps/agent-service/app/wiring/life_dataflow.py apps/agent-service/app/deployment.py apps/agent-service/tests/nodes/test_sync_life_state.py
git commit -m "feat(nodes): sync_life_state_node replaces arq state_sync_worker

Replaces the arq event-driven worker with a dataflow node:
  wire(ScheduleRevisionCreated).to(sync_life_state_node).durable()

Bound to agent-service app for now (durable consumer runs in main
process). Task 8 retires arq runtime entirely + decides if event-worker
deployment is needed.

Closes Gap 4 framework half.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §1 Gap 4"
```

---

## Task 8: Gap 4 + 6 business — 删 arq，update_schedule 改 emit

**Files:**
- Modify: `apps/agent-service/app/agent/tools/update_schedule.py`
- Delete: `apps/agent-service/app/workers/arq_settings.py`
- Delete: `apps/agent-service/app/workers/state_sync_worker.py`
- Modify: `apps/agent-service/app/workers/__init__.py`（如有 import 引用 arq_settings）
- Modify: `apps/agent-service/app/workers/README.md`（更新或删除 arq 文档）
- Modify: `apps/agent-service/pyproject.toml`（如 arq 仅 worker 用，移除依赖）
- Delete: `apps/agent-service/tests/unit/workers/test_state_sync_worker.py`
- Modify: `apps/agent-service/tests/unit/agent/tools/test_update_schedule.py`

### 子步

- [ ] **Step 8.1: `update_schedule.py` 改 emit**

替换 `enqueue_state_sync` + `arq pool` 整段（line 24-44）：

```python
# 旧 enqueue_state_sync 函数整段删除

async def _update_schedule_impl(
    *, persona_id: str, content: str, reason: str, created_by: str,
) -> dict:
    from app.domain.agent_tool_events import ScheduleRevisionCreated
    from app.runtime import emit

    content = (content or "").strip()
    reason = (reason or "").strip()
    if not content or not reason:
        return {"error": "content 和 reason 都不能为空"}

    rid = new_id("sr")
    async with get_session() as s:
        await insert_schedule_revision(
            s, id=rid, persona_id=persona_id,
            content=content, reason=reason, created_by=created_by,
        )

    # emit AFTER commit (Gap 8 spec convention) — sync_life_state_node consumes.
    await emit(ScheduleRevisionCreated(revision_id=rid, persona_id=persona_id))
    return {"revision_id": rid, "schedule": content}
```

- [ ] **Step 8.2: 删 worker 文件**

```bash
git rm apps/agent-service/app/workers/arq_settings.py apps/agent-service/app/workers/state_sync_worker.py apps/agent-service/tests/unit/workers/test_state_sync_worker.py
```

`app/workers/__init__.py` 检查是否 import 了被删的模块：

```bash
cat apps/agent-service/app/workers/__init__.py
```

如果有 `from app.workers.arq_settings import ...` 之类的，删除该行。

`app/workers/README.md` 更新：删 arq 启动命令章节，保留 runtime_entry 说明。

- [ ] **Step 8.3: 移除 arq 依赖**

```bash
grep "^arq" apps/agent-service/pyproject.toml
```

如果 arq 在 dependencies 里且不被其它代码引用，移除：

```bash
cd apps/agent-service && uv remove arq
```

- [ ] **Step 8.4: 改 update_schedule 测试**

`tests/unit/agent/tools/test_update_schedule.py` 删除原 patch arq pool 逻辑：

```python
# 删：with patch("app.workers.arq_settings.WorkerSettings", fake_settings):
# 加：patch app.runtime.emit + 断言 emit ScheduleRevisionCreated

@pytest.mark.asyncio
async def test_update_schedule_emits_revision_created():
    from app.domain.agent_tool_events import ScheduleRevisionCreated
    from app.agent.tools.update_schedule import _update_schedule_impl

    with patch("app.runtime.emit", new=AsyncMock()) as mock_emit, \
         patch("app.data.queries.insert_schedule_revision", new=AsyncMock()), \
         patch("app.data.session.get_session", new=_mock_session_cm(MagicMock())):
        result = await _update_schedule_impl(
            persona_id="akao-001",
            content="x",
            reason="y",
            created_by="chiwei",
        )
    emitted = mock_emit.await_args.args[0]
    assert isinstance(emitted, ScheduleRevisionCreated)
    assert emitted.persona_id == "akao-001"
    assert result["revision_id"] == emitted.revision_id
```

- [ ] **Step 8.5: 跑测试 + ruff**

```bash
cd apps/agent-service && uv run pytest -v 2>&1 | tail -10
uv run ruff check app/agent/tools/update_schedule.py app/workers/
```

- [ ] **Step 8.6: K8s arq-worker Deployment 评估**

**Step 8.6.1**：grep `pyproject.toml` / Dockerfile / k8s 配置看 arq-worker 启动命令现状：

```bash
grep -rn "arq.workers\|app.workers.arq_settings" apps/agent-service/Dockerfile* /paas-engine 2>/dev/null
# 实施时再决定 PaaS undeploy arq-worker 还是改成 event-worker（跑 runtime_entry + APP_NAME=event-worker）
```

**Step 8.6.2**：决定方案。两个选择：
- A. **arq-worker Deployment 退场**：因为 sync_life_state_node bind agent-service（Task 7），durable consumer 跑在主进程。简单、少一个 deployment。
- B. **改名 event-worker**：跑 dataflow runtime_entry + APP_NAME=event-worker，专跑 durable wire。隔离主 HTTP 服务。

v4 推荐 A（简单，agent-service 进程能 handle durable consumer load）。如有性能压力 v5 再开 event-worker。

Step 8.6.3：如选 A，本 task 不动 K8s（PaaS undeploy 在 Task 14 部署阶段做）；如选 B，需要在 deployment.py 加 event-worker bind + PaaS create app。

实施 Step 8 时跟用户确认 A 还是 B。

- [ ] **Step 8.7: 验收 grep**

```bash
grep -rn "arq\|enqueue_job\|create_pool" apps/agent-service/app/ | grep -v "README\|long_tasks"
# 预期：0 命中（long_tasks 子系统的 arq 用法不在 dataflow scope）

ls apps/agent-service/app/workers/
# 预期：仅 __init__.py + runtime_entry.py + common.py（如保留）
```

- [ ] **Step 8.8: Commit**

```bash
git add apps/agent-service/app/agent/tools/update_schedule.py apps/agent-service/app/workers/ apps/agent-service/pyproject.toml apps/agent-service/tests/
git rm 已经做过 Step 8.2
git commit -m "refactor(workers): retire arq runtime, update_schedule emits Data

update_schedule tool emits ScheduleRevisionCreated after DB commit;
sync_life_state_node (Task 7) consumes via durable wire in agent-service
process. arq queue / pool / WorkerSettings deleted.

- delete app/workers/arq_settings.py
- delete app/workers/state_sync_worker.py
- delete tests/unit/workers/test_state_sync_worker.py
- remove arq from pyproject.toml dependencies (was only for these workers)

K8s arq-worker Deployment retirement is a runtime ops step (PaaS
undeploy after this code lands; durable wire's lane queues on RabbitMQ
already drain before retire).

Closes Gap 4 + 6 business halves.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §2.4"
```

---

## Task 9: Gap 5 — chat post_actions / context 删 asyncio.create_task

**Files:**
- Modify: `apps/agent-service/app/chat/post_actions.py`
- Modify: `apps/agent-service/app/chat/context.py`
- Possibly Create: `apps/agent-service/app/domain/chat_events.py`（如需 ConversationMessageContentSynced Data）
- Possibly Modify: `apps/agent-service/app/wiring/chat.py`

### 子步

- [ ] **Step 9.1: post_actions.py 三处 _emit_memory_trigger → 直接 emit**

`chat/post_actions.py:89/94/99` 现状是：

```python
asyncio.create_task(_emit_memory_trigger(...))
```

`_emit_memory_trigger` 是 wrapper 调 `emit(MemoryDriftTrigger(...))` / `emit(MemoryAfterthoughtTrigger(...))`。直接 `await emit(...)`：

```python
# 删 _emit_memory_trigger wrapper
# 三处 asyncio.create_task(_emit_memory_trigger(drift_xxx)) 改为：
await emit(MemoryDriftTrigger(...))
# 或 afterthought trigger
await emit(MemoryAfterthoughtTrigger(...))
```

emit 内部如果 wire 是 durable 会异步 publish；async semantics 跟原 `asyncio.create_task` 比稍重（emit await 等 mq publish 完成），但 fire-and-forget 语义保留。

- [ ] **Step 9.2: chat/context.py:119 `_persist_tos_files`**

现状：

```python
asyncio.create_task(_persist_tos_files(l1_results, image_key_to_file))
```

选项：
- A. emit `ConversationMessageContentSynced` Data（新建），wire 到 durable node `_persist_tos_files`
- B. 接受现状（fire-and-forget），spec §2.5 允许这种例外（"chat_node 内部 single-task 句柄"模式）—— 但 context.py 不是 chat_node，所以不算例外

按 spec 不留隐患原则：选 A。

新建 `app/domain/chat_events.py`：

```python
"""Chat-side fire-and-forget events emitted from chat nodes."""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class ConversationMessageContentSynced(Data):
    """Trigger background sync of message.content TOS files."""
    message_id: Annotated[str, Key]
    l1_results: list = ...  # struct from chat/context.py 看实际类型
    image_key_to_file: dict = ...
    class Meta:
        transient = True
```

注意：`l1_results` / `image_key_to_file` 类型实际看 `_persist_tos_files` 签名；落地时 grep 验证。

`wiring/chat.py` 加：

```python
from app.domain.chat_events import ConversationMessageContentSynced
from app.nodes.chat_node import persist_tos_files_node  # 新建

wire(ConversationMessageContentSynced).to(persist_tos_files_node).durable()
```

`nodes/chat_node.py` 加 `persist_tos_files_node` thin shim 调 `chat/context.py:_persist_tos_files`。

context.py:119 改：

```python
await emit(ConversationMessageContentSynced(...))
```

- [ ] **Step 9.3: routes.py:139（已经在 Task 2 收敛，无需在本 Task 改）**

- [ ] **Step 9.4: 跑测试 + ruff**

```bash
cd apps/agent-service && uv run pytest tests/ -v 2>&1 | tail -10
uv run ruff check app/chat/ app/domain/chat_events.py app/wiring/chat.py
```

- [ ] **Step 9.5: 验收 grep**

```bash
grep -rn "asyncio.create_task\|asyncio.ensure_future" apps/agent-service/app/{chat,life,memory,api}/
# 预期：仅 chat/pre_safety_gate.py + chat_node.py 内部允许 + 必须有 docstring
```

- [ ] **Step 9.6: Commit**

```bash
git add apps/agent-service/app/chat/post_actions.py apps/agent-service/app/chat/context.py apps/agent-service/app/domain/chat_events.py apps/agent-service/app/wiring/chat.py apps/agent-service/app/nodes/chat_node.py
git commit -m "refactor(chat): replace asyncio.create_task with emit Data

Chat post_actions stops wrapping emit in asyncio.create_task — calls
emit(MemoryDriftTrigger / MemoryAfterthoughtTrigger) directly. The
fire-and-forget semantics are preserved because the wires are durable
(async mq publish).

context.py:_persist_tos_files moved into a node + wire(
ConversationMessageContentSynced).durable() — TOS file sync runs in
background via the same durable mechanism as other off-graph effects.

Only chat_node + pre_safety_gate may use asyncio.create_task (single-task
handle pattern documented in their docstrings).

Closes Gap 5.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §2.5"
```

---

## Task 10: chat/context.py 445 行拆分

**Files:**
- Modify: `apps/agent-service/app/chat/context.py`
- Possibly Create: `apps/agent-service/app/chat/context/{__init__.py, l1.py, persist.py, builder.py, ...}`

### 子步

- [ ] **Step 10.1: 量职责**

`wc -l apps/agent-service/app/chat/context.py` → 445 行（Phase 6 v3 之后）

读 context.py，按职责切：
- L1 query / cross-chat results
- TOS file persist
- LLM message builder
- gray config / persona resolve

按 spec §3.1，目标"按职责拆分（context / l1_results / persist 等）"，每个文件 < 300 行。

实施：grep `def ` 列出全部 30+ 函数，按职责归类，每组搬到独立文件，原 `context.py` 改为 package 重导出（类似 Task 4 的 queries.py 拆分模式）。

详细切分清单实施时确定（依赖读完 context.py 全文）。

- [ ] **Step 10.2: 写拆分**

仿 v3 queries.py 拆分模式（spec §3.5 重导出原则）：每文件 module docstring + 自包含 import + `__all__`。

- [ ] **Step 10.3: 跑全量测试**

```bash
uv run pytest 2>&1 | tail -8
```

- [ ] **Step 10.4: 量行**

```bash
wc -l apps/agent-service/app/chat/context*.py apps/agent-service/app/chat/context/*.py
# 预期：每文件 < 300 行
```

- [ ] **Step 10.5: Commit**

```bash
git commit -m "refactor(chat): split context.py 445 lines into focused modules

Per CLAUDE.md (single file <300 lines + single responsibility), context.py
split into multiple modules by responsibility (l1 query / TOS persist /
LLM message builder / gray config). Original context.py shrinks to
re-export package init.

Spec: docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md §3.1"
```

---

## Task 11: chat/agent_stream + stream 合并 audit、router audit

**Files:** 探索性，落地后定

### 子步

- [ ] **Step 11.1: audit chat/agent_stream + chat/stream 是否能合并**

```bash
# 看 caller
grep -rn "from app.chat.agent_stream\|from app.chat.stream" apps/agent-service --include="*.py"
# 看两文件的实际职责（docstring + 对外 API）
head -20 apps/agent-service/app/chat/agent_stream.py apps/agent-service/app/chat/stream.py
```

如果发现两文件职责重叠 → 合并到一个，删另一个。
如果职责实质不同（agent_stream 处理 langgraph + agent，stream 处理 token state） → 保留独立但加 docstring 说明边界。

- [ ] **Step 11.2: audit chat/router.py 是否跟 route_chat_node 重叠**

```bash
head -60 apps/agent-service/app/chat/router.py
# 看实际职责
grep -rn "from app.chat.router" apps/agent-service --include="*.py"
```

如发现 router.py 实际就是 message-level 路由（哪些 persona 应该回复），跟 nodes/chat_node:route_chat_node 的 fan-out 是不同事 → 保留，但重命名 `chat/router.py` → `chat/persona_filter.py` 避免歧义。

如发现重叠 → 合并到 nodes/chat_node。

- [ ] **Step 11.3: 落地决策 + commit**

每个 audit 结果一个 commit：
- 合并 commit message: `refactor(chat): merge agent_stream + stream`
- 重命名 commit message: `refactor(chat): rename router.py to persona_filter.py to avoid name collision with route_chat_node`

如发现都不需要改，本 task 0 commit（产出："已 audit 确认 X / Y 职责不同，保留")，但要在 PR description 写明结论。

- [ ] **Step 11.4: 跑测试**

```bash
uv run pytest 2>&1 | tail -8
```

---

## Task 12: life/sister_theater + wild_agents + state_sync 收敛

**Files:** 探索性

### 子步

- [ ] **Step 12.1: audit sister_theater + wild_agents 跟 schedule 关系**

```bash
head -50 apps/agent-service/app/life/sister_theater.py apps/agent-service/app/life/wild_agents.py
grep -rn "from app.life.sister_theater\|from app.life.wild_agents" apps/agent-service --include="*.py"
```

如果 sister_theater.py（39 行）只是 schedule.py 用的常量 / 小 helper → 合到 schedule.py。
如果 wild_agents.py（60 行）只在 schedule 调 → 同上合并。

按"functions that change together live together"原则。

- [ ] **Step 12.2: state_sync.py 收敛**

Task 7 引入 sync_life_state_node 调 `life.state_sync.refresh_life_state_after_schedule`；state_sync.py 模块继续存在合理（业务实现层）。

但要审：state_sync.py 跟 life/engine.py 是不是有重叠（都在算 life-engine 状态）。如果 state_sync 的 `refresh_*` 函数实际就是 `engine.tick(force=True)` 的子集 → 合并，删 state_sync.py。

- [ ] **Step 12.3: 落地 + commit**

按 audit 结果决定，commit message 类似：
- `refactor(life): merge sister_theater + wild_agents into schedule.py`
- `refactor(life): inline state_sync into engine.py`

或 0 改动 + audit 结论写 PR description。

---

## Task 13: memory/cross_chat + memory/context 评估

**Files:** 探索性

### 子步

- [ ] **Step 13.1: audit cross_chat + context 是否重叠**

```bash
head -30 apps/agent-service/app/memory/cross_chat.py apps/agent-service/app/memory/context.py
diff <(grep "^def \|^async def " apps/agent-service/app/memory/cross_chat.py) <(grep "^def \|^async def " apps/agent-service/app/memory/context.py)
```

如果 cross_chat 是按 chat_id 维度 / context 是按 message 维度 → 实质职责不同，保留。
如果重叠 → 合并。

- [ ] **Step 13.2: 落地 + commit**

按 audit 结果决定。

---

## Task 14: dev 泳道部署 + e2e 验证

**Files:** 0（运维 + 测试）

### 子步

- [ ] **Step 14.1: 推送 + 更新 PR**

```bash
git push origin refactor/flow-parse-6
ghc pr edit 210 --body "[updated to v4 scope, see spec docs/superpowers/specs/2026-05-07-dataflow-phase-6-cleanup-design.md]"
```

- [ ] **Step 14.2: 部署 dev 泳道**

PR #210 已有 dev 泳道 phase6-cleanup（agent-service 1.0.0.329 / arq-worker / vectorize-worker）。本 task 重新构建：

```bash
make deploy APP=agent-service LANE=phase6-cleanup GIT_REF=refactor/flow-parse-6
make release APP=arq-worker LANE=phase6-cleanup VERSION=<new-version>  # 如选 Task 8 方案 A，arq-worker undeploy 而不是 release
make release APP=vectorize-worker LANE=phase6-cleanup VERSION=<new-version>
```

如 Task 8 选方案 A：

```bash
make undeploy APP=arq-worker LANE=phase6-cleanup
```

- [ ] **Step 14.3: 飞书 e2e 验证**

绑定 dev bot：

```
/ops bind TYPE=bot KEY=dev LANE=phase6-cleanup
```

测试矩阵：
1. 群聊 / p2p 普通对话（chat 主链路）
2. glimpse 触发 proactive（Phase 6 v3 验证项保留）
3. update_schedule tool 调用 → 观察 sync_life_state_node 跑（Gap 4）
4. commit_abstract tool 调用 → 观察 vectorize 跑（Gap 3 + 2）
5. 13 个 admin/api endpoint 全部访问验证（Gap 1）
6. drift / afterthought 触发（Gap 5 chat post_actions）

- [ ] **Step 14.4: grep 总验收（按 spec §4 全验）**

```bash
cd apps/agent-service
echo "=== zero @router\\. except /health ==="
grep -rn "@router\\." app/ | grep -v "/health"

echo "=== zero mq.publish in business code ==="
grep -rn "mq.publish" app/ | grep -v "runtime/\|infra/rabbitmq"

echo "=== zero arq enqueue ==="
grep -rn "enqueue_job\|create_pool\|from arq" app/ | grep -v README

echo "=== asyncio.create_task only in chat_node + pre_safety_gate ==="
grep -rn "asyncio.create_task\|asyncio.ensure_future" app/chat/ app/life/ app/memory/ app/api/

echo "=== workers/ minimal ==="
ls app/workers/

echo "=== routes.py size ==="
wc -l app/api/routes.py
```

每条都按 spec §4 期望。

- [ ] **Step 14.5: 解绑 + 下泳道（验证完成后）**

```
/ops unbind TYPE=bot KEY=dev
make undeploy APP=agent-service LANE=phase6-cleanup
make undeploy APP=vectorize-worker LANE=phase6-cleanup
# arq-worker 看 Task 8 决策
```

- [ ] **Step 14.6: 等用户决定是否 ship**

不自行 `ghc pr merge`。等用户明确说"ship"再走 `/ship` skill。

---

## 总验收（全 task 完成后）

跑 spec §4 全验：

```bash
cd apps/agent-service
uv run pytest 2>&1 | tail -3
uv run ruff check app/data/queries/ app/runtime/http_source.py app/runtime/emit.py
ls app/api/routes.py app/data/queries/  # routes.py 在，queries.py 不在
wc -l app/api/routes.py app/data/queries/*.py  # routes.py < 50, queries.py 不存在
grep -rn "@router\." app/ | grep -v "/health"  # 仅 health
grep -rn "mq.publish" app/ | grep -v "runtime/\|infra/rabbitmq"  # 0
grep -rn "enqueue_job\|create_pool" app/  # 0
grep -rn "asyncio.create_task" app/{chat,life,memory,api}  # 仅 pre_safety_gate + chat_node
ls app/workers/  # __init__ + runtime_entry + common
```
