# Dataflow Phase 7d — DB / Redis / HTTP capability 收敛

**状态**: Draft v1（2026-05-09）
**前置**: PR #214（Phase 7c, 1.0.1.15）已 ship 到 prod，Gap 7+8+9+11+12+15+18 已闭合
**承接**: 本文件覆盖 Gap 13 (DB session) / Gap 14 (Redis lock+set) / Gap 16 (HTTP client) 三项
**后续**: 7e 收 Gap 10 / 17 / 19（streaming / health / async join）

---

## 0. 与原 Phase 7 gap analysis 的偏离说明

`docs/superpowers/specs/2026-05-08-dataflow-phase-7-gap-analysis.md` §7d 原版给出 Gap 13 的两条建议：

1. **扩 `runtime/query.py` 加 `mutate(sql, params)`** ← **本 spec 否决**
2. 按业务域拆 repository capability，业务节点只调 repo API ← 本 spec **半采纳，留出业务事务边界**

否决「扩 query.py mutate」的原因：`runtime/query.py:24-27` 文件级注释明确写「Intentionally not supported: JOINs, aggregations, OR/IN filters, raw SQL. Callers who need more reach should drop to `get_session()` + `text()` directly rather than grow this builder into a half-baked ORM.」—— query.py 自己拒绝长成 ORM。再往里塞 mutate 违反它已确立的设计立场。

「按业务域拆 repository capability」原版要求每个业务用例对应一个 repo method（`memory_repo.commit_abstract(...)` / `life_repo.upsert_glimpse_state(...)` 等）。**本 spec 拒绝**冻结业务用例为 repo API，原因：

- 赤尾业务仍在快速迭代，新用例每周出现（如「沉淀抽象 + reviewer 队列推一条」是现有 commit_abstract 的临时扩展）。预先固定 50 个 repo method = 给框架反向加锁。
- 现有 `app/data/queries/*.py` 已经是分业务域的领域级函数（`insert_abstract_memory(s, ...)` 不是裸 SQL）—— 真正泄漏的不是「业务在写 SQL」，是 **session 这个对象暴露在签名上 → 业务必须懂事务 / commit / contextvar**。

本 spec 的解法：**保留 `data/queries/*.py` 9 个领域文件不动其切分**，做两件事：

1. session 走 contextvar，从 query 函数签名拿掉。业务永远看不到 session 对象。
2. 业务用 `async with tx():` 表达「这几行原子」，单条 query / 单条写入不需要包；emit 走 `await emit_tx(data)` 强制在 tx 内（保证 outbox 与业务写同事务）。

这样业务只剩「这几行属不属于一个原子动作」一个业务知识层面的概念，工程层（session / commit / SAVEPOINT / contextvar）全藏。

---

## 1. 终态判定（按业务作者视角）

### 写赤尾新业务时，作者只回答这些问题

| 场景 | 作者写什么 | 不需要写什么 |
|---|---|---|
| 单条查询 | `await find_persona(persona_id)` | session、tx |
| 单条写入 | `await insert_fragment(id=..., ...)` | session、tx |
| 多条原子写入 | `async with tx(): await insert_a(...); await insert_b(...); await emit_tx(Event(...))` | session、commit、rollback、SAVEPOINT |
| 防止同 key 并发执行 | `async with single_flight(f"drift:{chat}:{persona}", ttl=600): ...` | redis、SETNX、Lua、token |
| 检查违禁词 | `if await banned_words.contains(text): ...` | redis、smembers、key naming |
| 调外部搜索 | `results = await web_search(query, count=10)` | httpx、API key、retry、timeout |
| 调内部 sandbox | `result = await sandbox.run(skill, payload)` | httpx、lane header、trace header、auth |

### CI 守住（业务区 = `apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/`）

```bash
# Gap 13 — 关闭所有 session 来源 + 任何对 session 对象的直接操作
grep -rn "from app\.data\.session import\|from app\.data import session" 业务区 == 0
grep -rn "get_session(\|async_session(\|AsyncSessionLocal" 业务区 == 0
grep -rn "AsyncSession" 业务区 == 0
grep -rn "session\.execute\|session\.commit\|session\.add\|session\.merge\|session\.flush" 业务区 == 0
grep -rn "transactional_emit" 业务区 == 0   # 被 emit_tx 取代

# Gap 14 — 关闭所有 redis 来源在业务区
grep -rn "from app\.infra\.redis import\|from app\.infra import redis" 业务区 == 0
grep -rn "redis\.set(.*nx=True\|redis\.eval(\|redis\.smembers(\|redis\.sadd(" 业务区 == 0

# Gap 16 — 关闭所有 httpx import 业务区
grep -rn "import httpx\|from httpx" 业务区 == 0
```

允许命中区：`runtime/` / `capabilities/` / `infra/` / `data/`（framework 实现层 + query 层 + redis-backed capability 实现层 `infra/image.py`）。

---

## 2. Gap 13 — DB / session capability 设计

### 2.1 模块清单与职责

| 模块 | 职责 | 现状 → 目标 |
|---|---|---|
| `runtime/db.py`（**新建**） | 提供 `tx()` context manager + `current_session()` 读 contextvar + `emit_tx(data)` outbox API + auto-tx 兜底装饰器 | 不存在 → 60-80 行新代码 |
| `data/session.py` | DB engine + sessionmaker，**保留**；`get_session()` **保留但仅供 framework 内部用** | 不动 |
| `data/queries/*.py` | 9 个领域 query 模块，**保留切分**；函数签名从 `(session, ...)` 改成 `(...)`，内部用 `current_session()` | 70+ 个函数全部签名简化（mechanical） |
| `runtime/outbox.py` | OutboxEmitter / `transactional_emit`，**保留** framework 内部使用，但**不再向业务暴露** | `transactional_emit` 改 private（`_transactional_emit_internal`），新 `emit_tx(data)` 包装暴露 |

### 2.2 接口定义

#### 2.2.1 `runtime/db.py` — 新建

```python
"""DB session capability — Phase 7d Gap 13.

业务永远不直接拿 session。三个对外 API：

  - ``async with tx():``  — 表达「这几行原子」
  - ``await emit_tx(data)``  — 在 tx 内追加 outbox row（强制：tx 外调用 raise）
  - ``current_session()``  — query 函数内部用；业务区禁止 import

session 走 contextvar。**AsyncSession 单 session 单 connection 不支持并发使用，
所以同一 tx 内 DB 操作只能顺序 await — 在 tx 内塞 ``asyncio.gather`` 跑多条
query 没有并发收益（最终被 SQLAlchemy 锁串成顺序），并且会触发
``InvalidRequestError``。** 想并发查就让每个分支自己进独立 tx，见下方业务示例。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.data.session import get_session as _get_session_internal
from app.runtime.data import Data
from app.runtime.outbox import OutboxEmitter

_session_var: ContextVar[AsyncSession | None] = ContextVar("_session", default=None)


def current_session() -> AsyncSession:
    """Return the AsyncSession bound to the current tx context.

    Caller MUST be inside ``async with tx():`` (or auto-tx fallback).
    Raises ``RuntimeError`` otherwise.
    """
    s = _session_var.get()
    if s is None:
        raise RuntimeError(
            "current_session() called outside tx() — wrap your DB calls in "
            "`async with tx():` or rely on a query function's auto-tx fallback"
        )
    return s


@asynccontextmanager
async def tx() -> AsyncIterator[None]:
    """Open / reuse a DB transaction. Nestable (SAVEPOINT)."""
    existing = _session_var.get()
    if existing is not None:
        # nested — use SAVEPOINT
        async with existing.begin_nested():
            yield
        return

    async with _get_session_internal() as s:
        token = _session_var.set(s)
        try:
            yield
        finally:
            _session_var.reset(token)


async def emit_tx(data: Data) -> None:
    """Append outbox row in the current tx. Raises if not in a tx."""
    s = current_session()
    emitter = OutboxEmitter(s)
    await emitter.append(data)


@asynccontextmanager
async def auto_tx() -> AsyncIterator[None]:
    """Internal helper used by query functions: if not in tx, open one for this single call."""
    if _session_var.get() is not None:
        yield
        return
    async with tx():
        yield
```

#### 2.2.2 query 函数签名改造（机械）

```python
# Before
async def insert_fragment(session: AsyncSession, *, id: str, ...) -> None:
    await session.execute(text("INSERT INTO ..."), {...})

# After
from app.runtime.db import auto_tx, current_session

async def insert_fragment(*, id: str, ...) -> None:
    async with auto_tx():
        await current_session().execute(text("INSERT INTO ..."), {...})
```

`auto_tx()` 兜底：
- 业务调时已在 `async with tx():` 内 → 复用现有 session
- 业务直接调 `await insert_fragment(...)` 没包 tx → query 自己开一个一次性事务，commit 完关掉

→ 业务作者写「单条写入」不用任何 with；写「多条原子」才包 tx。

#### 2.2.3 业务侧形态对照

```python
# 单条查询（赤尾里最多的形态）
persona = await find_persona(persona_id)     # 不用 with

# 单条写入
await insert_fragment(id=fid, persona_id=..., ...)  # 不用 with

# 多条原子（commit_abstract 形态）
async with tx():
    await insert_abstract_memory(id=aid, persona_id=..., ...)
    for fid in supported_by_fact_ids:
        await insert_memory_edge(id=new_id("e"), from_id=fid, to_id=aid, ...)
    await emit_tx(AbstractMemoryCommitted(abstract_id=aid, persona_id=..., chat_id=...))
# tx 退出时 commit；中途 raise 自动 rollback（包括 outbox row）

# 节点内并发查 DB — 每分支独立事务（独立 session、独立 connection）
# !! 前提：调用方自身必须不在外层 tx 里 —— ContextVar 会继承，
# !! 外层有 tx 时分支里的 tx() 会复用外层 session（走 SAVEPOINT 嵌套），
# !! 多个分支仍然共享同一个 connection，gather 仍会撞坑。
async def gather_branches():
    async def branch_a():
        async with tx():               # 仅当外层无 tx 时，这里才是独立 session
            return await find_xxx(...)
    async def branch_b():
        async with tx():
            return await find_yyy(...)
    return await asyncio.gather(branch_a(), branch_b())
```

### 2.3 不变量（写到 db.py docstring + spec 这里）

1. **业务区 0 处 `from app.data.session import ...`**（不允许任何来源拿 session）—— grep gate 守。
2. **业务区 0 处 `AsyncSession` 类型出现** —— 业务不见 session 对象类型。
3. **业务区 0 处 `current_session` import / 调用** —— framework primitive 但只供 `data/queries/*.py` 内部用；业务区禁止 import `current_session` 或调用它，否则就拿到了裸 session。
4. **业务区 0 处 `session.execute / .commit / .add / .merge / .flush / .rollback`**（含 `s.` 变量名别名）—— 兜底 grep；正常情况下不变量 1-3 已堵住 session 来源，本条不会命中。
5. **`emit_tx` 必须在 tx 内** —— 否则 raise；保证 outbox 永远和业务写同事务。
6. **tx 内 DB 操作只能顺序 await。并发查 DB 必须在 tx 外层调用 gather，每分支自己进 tx。** AsyncSession 单 connection 不支持并发使用；ContextVar 继承机制让嵌套 `tx()` 走 SAVEPOINT 复用外层 session，**所以"每分支独立 tx"只在外层无 tx 时成立**。本规则靠 docstring + code review 约束（不上集成测试 — 实测「错误写法必然 raise」是 race-dependent，可能串行通过、可能 raise InvalidRequestError、可能 raise PendingRollbackError，不稳）。
7. **tx 内禁止外部 IO（LLM / HTTP）长调用** —— 文档约定，不强制；`tx()` 内置 elapsed 计时，超过 5 秒 log warning。

### 2.4 现状差距（按文件统计）

#### `get_session(` / `AsyncSessionLocal`：90 处业务区命中

| 业务域 | 文件 | 处数 |
|---|---|---|
| life | glimpse.py / proactive.py / schedule.py / engine.py / state_sync.py / tool.py | 22 |
| nodes | memory_pipelines.py / admin.py / chat_node.py / safety.py / hydrate_message.py / life_dataflow.py / sync_life_state.py / vectorize.py | 28 |
| memory | reviewer/tools.py / voice.py / cross_chat.py / vectorize_memory.py / recall_engine.py / context.py / conflict.py / _persona.py / _timeline.py / sections/* | 22 |
| agent | tools/history.py / notes.py / commit_abstract.py / update_schedule.py / models.py | ~10 |
| chat | persona_filter.py / agent_stream.py / _context_images.py | 6 |
| **合计** | **~38 files** | **90** |

#### `async_session(` 业务区命中：2 处（之前漏抓）

- `chat/quick_search.py:65` —— `async with async_session() as session: ...`
- `nodes/persist_tos_files.py:49` —— 同上

#### 直接 `session.execute / .commit / .add` 业务区命中：6 处

- `chat/quick_search.py:89, 125`
- `nodes/persist_tos_files.py:60`
- `life/proactive.py:51, 145, 197`

`life/proactive.py` 同时有 `session.add(msg)` 这种 ORM 操作，改造时全部改成 `await current_session().execute(text(...))` 或抽到 `data/queries/messages.py` 加新 query 函数（推荐后者，保持业务侧只看到领域 API）。

#### `transactional_emit` 业务区命中：7 文件（按 grep）

`agent/tools/commit_abstract.py / notes.py / update_schedule.py`、`life/glimpse.py / proactive.py / tool.py`、`nodes/memory_pipelines.py` —— 全部改 `emit_tx`。

#### query 函数签名改造

9 个 `data/queries/*.py` 模块，~70 个函数全部去掉 `session: AsyncSession` 参数，内部从 `session.execute(...)` 改成 `current_session().execute(...)` → mechanical。

---

## 3. Gap 14 — Redis lock / banned set / image registry capability 设计

**完整闭合 Gap 14**：lock + banned set + Redis-backed registry 三类。原 spec §Gap 14 包含「typed Registry 收敛」，本 PR 把唯一一处 `ImageRegistry`（`chat/context.py:128` 业务侧构造时把 redis 传进去）也一并收掉。

### 3.1 模块清单

| 模块 | 职责 | 现状 → 目标 |
|---|---|---|
| `runtime/single_flight.py`（**新建**） | `single_flight(key, ttl)` async context manager；进入抢 SETNX + uuid token；离开 Lua 比较 token + 删；冲突 raise `SingleFlightConflict` | 不存在 → ~50 行新代码 |
| `capabilities/banned_words.py`（**新建**） | `banned_words.contains(text) -> str | None`，下面读 `_BANNED_WORDS_KEY` redis set | 不存在 → ~30 行 |
| `infra/image.py` | `ImageRegistry` 构造改为只接收 `message_id`；redis 由内部自取（不再让业务把 redis client 传进来） | 改 ~10 行 |
| `chat/context.py` | 删 `from app.infra.redis import get_redis` + `await get_redis()`；构造 `ImageRegistry(message_id)`（不传 redis） | -3 行 / +0 行 |
| `nodes/memory_pipelines.py` | 删 SETNX + Lua 调用，改用 `single_flight` | -40 行 / +8 行 |
| `nodes/safety.py` | 删 `redis.smembers` 调用，改用 `banned_words.contains` | -8 行 / +3 行 |

### 3.2 接口定义

#### 3.2.1 `runtime/single_flight.py`

```python
"""Single-flight lock — Phase 7d Gap 14.

Idiom:
    async with single_flight(f"drift:{chat}:{persona}", ttl=600):
        await _do_work()

**语义**：ttl 时间窗内 single-flight，**不是任务存活期严格互斥**。
- 进入：SETNX + uuid token，已被持有 → raise SingleFlightConflict
- 离开：Lua 比较 token 后 DEL（防误删别人持有的锁）
- TTL 到期前：保护 single-flight
- TTL 到期后：哪怕原 holder 还在跑，新 holder 可以进入；原 holder finally 时
  Lua 比较 token 失败、不会误删新 holder 的锁

调用方负责选择「比业务最坏耗时更大的 ttl」。如果业务可能跑超过 ttl，就要么
扩 ttl，要么接受 ttl 后并发的可能（例如 drift 用 600s + DebounceReschedule 兜底
即可）。
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.infra.redis import get_redis

_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class SingleFlightConflict(Exception):
    """Raised when another holder already owns the key."""

    def __init__(self, key: str) -> None:
        super().__init__(f"single-flight conflict on key={key!r}")
        self.key = key


@asynccontextmanager
async def single_flight(key: str, *, ttl: int) -> AsyncIterator[None]:
    """Acquire single-flight lock; raise SingleFlightConflict if held."""
    redis = await get_redis()
    token = uuid.uuid4().hex
    if not await redis.set(key, token, nx=True, ex=ttl):
        raise SingleFlightConflict(key)
    try:
        yield
    finally:
        await redis.eval(_RELEASE_LUA, 1, key, token)
```

#### 3.2.2 `infra/image.py` ImageRegistry 改造

```python
# Before
class ImageRegistry:
    def __init__(self, message_id: str, redis: Redis) -> None:
        self._message_id = message_id
        self._redis = redis
    ...

# After — redis 内部自取
class ImageRegistry:
    def __init__(self, message_id: str) -> None:
        self._message_id = message_id

    async def _get_redis(self) -> Redis:
        from app.infra.redis import get_redis
        return await get_redis()

    async def register(self, ...) -> ...:
        redis = await self._get_redis()
        ...
```

**业务侧**：

```python
# chat/context.py
# Before
from app.infra.redis import get_redis
redis = await get_redis()
registry = ImageRegistry(message_id, redis)

# After
registry = ImageRegistry(message_id)
```

#### 3.2.3 `capabilities/banned_words.py`

```python
"""Banned-words set — Phase 7d Gap 14."""
from __future__ import annotations

from app.infra.redis import get_redis

_KEY = "banned_words"


async def contains(text: str) -> str | None:
    """Return the matched banned word, or None if clean."""
    redis = await get_redis()
    words = await redis.smembers(_KEY)
    if not words:
        return None
    normalized = text.replace(" ", "").lower()
    for w in words:
        if w in normalized:
            return w
    return None
```

#### 3.2.4 业务侧形态对照

```python
# nodes/memory_pipelines.py — drift_check / afterthought_check
@node
async def drift_check(trigger: DriftTrigger) -> None:
    try:
        async with single_flight(
            f"phase2:drift:{trigger.chat_id}:{trigger.persona_id}", ttl=600
        ):
            await _run_drift(trigger.chat_id, trigger.persona_id)
    except SingleFlightConflict:
        raise DebounceReschedule(DriftTrigger(
            chat_id=trigger.chat_id, persona_id=trigger.persona_id,
        ))

# nodes/safety.py — _check_banned_word
async def _check_banned_word(text: str) -> str | None:
    return await banned_words.contains(text)
```

### 3.3 现状差距

5 处 redis 调用 + 1 处 ImageRegistry 业务侧 redis 依赖：
- `nodes/memory_pipelines.py:254 / 265 / 274 / 285` ← drift + afterthought 各一对 SETNX/Lua
- `nodes/safety.py:129` ← smembers
- `chat/context.py:127-128` ← `await get_redis() + ImageRegistry(message_id, redis)`

`nodes/memory_pipelines.py` 顶部 `_LOCK_RELEASE_LUA` 常量删除（移到 `runtime/single_flight.py`）。

`infra/image.py` 仍 import redis（capability 实现层，允许）；业务区 `chat/context.py` 不再 `from app.infra.redis import get_redis`。

---

## 4. Gap 16 — HTTP capability 设计

### 4.1 模块清单

| 模块 | 职责 | 现状 → 目标 |
|---|---|---|
| `capabilities/http.py` | **现存**，Lane-aware HTTPClient；扩 retry / timeout 默认 | 现 47 行 → ~100 行（加 retry / timeout） |
| `capabilities/web_search.py`（**新建**） | `web_search(query, count) -> SearchResults` + `read_webpage(url) -> str` + `rerank(query, docs) -> list[Hit]`；下面用 HTTPClient（base_url=settings.you_search_host）+ X-API-Key | 不存在 → ~120 行 |
| `capabilities/image_search.py`（**新建**） | `image_search(query, count) -> list[ImageHit]`；下面同上 | 不存在 → ~50 行 |
| `capabilities/sandbox.py`（**新建**） | `sandbox.run(skill_name, payload) -> SandboxResult`；下面用 HTTPClient（service="sandbox"）走 LaneRouter | 不存在 → ~50 行 |
| `agent/tools/search.py` | 删 httpx import + retry + 手塞 header；改调 `web_search` / `read_webpage` / `rerank` / `google_search` / `brave_search` capability | -120 行 / +40 行 |
| `agent/tools/image_search.py` | 删 httpx；改调 `image_search` capability | -60 行 / +20 行 |
| `agent/image_gen.py` | 删 inline `import httpx`；改用 HTTPClient | -3 行 / +1 行 |
| `skills/sandbox_client.py` | **整文件删除**（73 行） | -73 / +0 |
| `agent/tools/sandbox.py` | 删 `SandboxClient` 引用（`_get_sandbox_client / _sandbox_client` 全删）；`sandbox_bash` 直接 `from app.capabilities.sandbox import run`；保留 module-level reference 以让 test patch（patch 目标改成 `app.agent.tools.sandbox.run` 或 `app.capabilities.sandbox.run`） | -25 行 / +5 行 |
| `skills/renderer.py` | 同上，删 `_get_sandbox_client` / `sandbox_client` global；`run_directive` 直接调 `sandbox.run(command=..., skill_name=...)` | -15 行 / +5 行 |
| `tests/unit/test_skill_renderer.py` | 三处 `@patch("app.skills.renderer.sandbox_client")` 改成 `@patch("app.capabilities.sandbox.run")`，mock 返回 `SandboxResult(exit_code=0, stdout="...", stderr="")` | -? / +? 行 |
| `tests/unit/test_sandbox_tool.py`（如存在 — 改造时确认） | 同上更新 patch 目标和 mock 返回 | 同上 |

### 4.2 接口定义

#### 4.2.1 `capabilities/http.py` 升级（按错误粒度 + 方法分级的 retry 策略）

**核心原则**：retry 决策必须区分「服务端肯定没执行」/「服务端可能已执行」。POST 默认零 retry；GET/HEAD 默认 retry。POST 想要 retry，必须传 `idempotency_key`，并且**即便如此也只对 connect 阶段失败 + 429 重试**——`ReadTimeout` / `WriteTimeout` / `5xx` 仍不重试（这些状态下服务端可能已执行，重试 = 重复副作用 / 重复消耗外部 API 配额）。

##### Retry 决策矩阵

| 错误类型 | GET / HEAD | POST 默认 | POST + idempotency_key |
|---|---|---|---|
| `ConnectError` / `ConnectTimeout` / DNS / TLS 握手失败（请求未到达服务端） | retry | **retry** | retry |
| 429 Too Many Requests（合规重试） | retry | **retry** | retry |
| `ReadTimeout`（请求已发，等响应超时；服务端可能已执行） | retry | 不 retry | **不 retry** |
| `WriteTimeout` / `RemoteProtocolError`（请求半途断开） | retry | 不 retry | **不 retry** |
| 5xx（500/502/503/504 — 服务端响应了但失败；可能已执行业务） | retry | 不 retry | **不 retry** |
| 4xx（除 429） | 不 retry | 不 retry | 不 retry |

> 即使有 `idempotency_key`，本框架仍然**不**给 POST 开 ReadTimeout / 5xx 重试。服务端真正幂等需要服务端自己用 idempotency key 做去重；客户端无法保证。让 capability 显式选择是更稳的设计：业务知道 sandbox `/exec` 命令必须只跑一次（不传 key），知道 web 查询多消费一次配额无所谓（也可以选不传 key）。

##### 接口

```python
class HTTPClient:
    def __init__(
        self,
        service: str | None = None,
        *,
        timeout: float = 30.0,
        retries: int = 3,                     # GET/HEAD retry 次数
        retry_post: int = 0,                  # POST retry 次数（仅当 idempotency_key 传入时生效）
        retry_backoff: float = 0.5,           # exponential base
        retry_on_status_get: frozenset[int] = frozenset({429, 500, 502, 503, 504}),
        retry_on_status_post: frozenset[int] = frozenset({429}),  # POST 即使带 key 也只对 429 重试
    ) -> None:
        ...

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self._request("GET", path, **kw)

    async def post(
        self,
        path: str,
        *,
        idempotency_key: str | None = None,
        **kw: Any,
    ) -> httpx.Response:
        # 没传 idempotency_key:           retries 强制为 0
        # 传了 idempotency_key:            按 retry_post 次数 retry，但仅 connect 阶段 + 429
        # 传了 idempotency_key 时同时注入  Idempotency-Key header，留给服务端去重
        ...

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        # connect 阶段错误 (ConnectError / ConnectTimeout / DNS / TLS) → 所有方法都 retry
        # ReadTimeout / WriteTimeout / RemoteProtocolError → GET retry, POST 不 retry
        # status code → 按 retry_on_status_{get,post} 决策
        ...
```

##### 各 capability 实际 method + retry 决策（基于真实 endpoint verify）

| capability | 函数 | 实际 endpoint method | retry 决策 | 理由 |
|---|---|---|---|---|
| `web_search` | `web_search(query)` 路由到 You/Google/Brave | **GET**（You `/v1/search`、Google CSE、Brave 都是 GET with params） | 默认 GET retry（connect / read / 5xx / 429） | 幂等查询 |
| `read_webpage` | `read_webpage(url)` | **POST** `/v1/contents`（payload `{urls, formats}`） | **不传 idempotency_key、retries=0**；仅 connect 阶段错误依靠 httpx 默认行为 | webpage 查询消耗外部 API 配额，重复读不应自动 retry；语义幂等所以业务可手动重试 |
| `rerank` | `rerank(query, docs)` | **POST** `/v1/rerank`（SiliconFlow） | 同上，**不传 idempotency_key、retries=0** | 同上理由 |
| `image_search` | `image_search(query)` | **GET**（按 image_search.py:72 现状） | 默认 GET retry | 幂等查询 |
| `sandbox.run` | `sandbox.run(command=...)` | **POST** `/exec` | **不传 idempotency_key、retries=0** | 非幂等：执行 bash 命令有副作用 |
| `image_gen` 内 TOS upload | TOS PUT/POST | **POST** | retries=0 | 上传非幂等 |

> 没有任何 capability 当前传 `idempotency_key`。该参数留给将来：业务上下文有稳定 hash 且服务端配合做去重时再启用。本 PR 范围内**所有 POST 都是 retries=0**。

#### 4.2.2 `capabilities/web_search.py`

```python
"""Web search capability — Phase 7d Gap 16."""
from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass
class SearchResults:
    hits: list[SearchHit]
    elapsed_ms: int


_client = HTTPClient(timeout=10.0)
# absolute base URL embedded — search 是外部供应商


async def web_search(query: str, *, count: int = 10) -> SearchResults:
    """Search via You Search API."""
    if not settings.you_search_host or not settings.you_search_api_key:
        return SearchResults(hits=[], elapsed_ms=0)
    resp = await _client.post(
        f"{settings.you_search_host}/v1/search",
        json={"query": query, "count": count},
        headers={"X-API-Key": settings.you_search_api_key},
    )
    ...


async def read_webpage(url: str) -> str:
    """Fetch + html→text via You Search Contents API."""
    ...


async def rerank(query: str, docs: list[str], *, top_k: int = 5) -> list[int]:
    """Rerank docs against query, return top-k indices."""
    ...
```

#### 4.2.3 `capabilities/sandbox.py`

**接口契约必须对齐现有 sandbox-worker**（按 `skills/sandbox_client.py` 现状）：
- service: `sandbox-worker`（不是 `sandbox`）
- endpoint: `POST /exec`
- request body: `{command, skill_name, envs, timeout_sec}`
- auth header: `Authorization: Bearer {settings.inner_http_secret}`（如配置）
- response: `{exit_code, stdout, stderr}`；exit_code != 0 时业务侧自决格式化
- **non-idempotent**：`HTTPClient` 不传 idempotency_key，超时/失败一律不重试

```python
"""Sandbox skill execution — Phase 7d Gap 16."""
from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings

# service="sandbox-worker" → LaneRouter 解 lane；trace + lane header 自动注入
# retries=0（POST 默认不重试）、retry_post=0：sandbox /exec 非幂等
_client = HTTPClient(service="sandbox-worker", timeout=45.0)


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str


async def run(
    *,
    command: str,
    skill_name: str = "",
    envs: dict[str, str] | None = None,
    timeout: int = 30,
) -> SandboxResult:
    """Execute command in sandbox; non-idempotent, no retry on failure."""
    headers: dict[str, str] = {}
    if settings.inner_http_secret:
        headers["Authorization"] = f"Bearer {settings.inner_http_secret}"
    resp = await _client.post(
        "/exec",
        json={
            "command": command,
            "skill_name": skill_name,
            "envs": envs or {},
            "timeout_sec": timeout,
        },
        headers=headers,
        # 不传 idempotency_key — POST 不重试
    )
    resp.raise_for_status()
    data = resp.json()
    return SandboxResult(
        exit_code=data["exit_code"],
        stdout=data["stdout"],
        stderr=data["stderr"],
    )
```

业务侧（agent tool 包装层）保留「exit_code != 0 时拼成报错字符串」逻辑：

```python
# agent/tools/sandbox.py（或 skill 调用方）
result = await sandbox.run(command=cmd, skill_name=skill, envs=env, timeout=t)
if result.exit_code != 0:
    return (
        f"命令退出码 {result.exit_code}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
return result.stdout
```

### 4.3 业务侧形态对照

```python
# agent/tools/search.py
@tool
@tool_error("搜索失败")
async def web_search_tool(query: str) -> list[dict]:
    results = await web_search(query, count=10)
    return [hit.__dict__ for hit in results.hits]


# agent/tools/image_search.py
@tool
@tool_error("图片搜索失败")
async def search_images(query: str) -> list[dict]:
    hits = await image_search(query, count=IMAGE_MAX_RESULTS)
    ...
```

### 4.4 现状差距

3-4 处 import httpx 业务侧（按 grep）：
- `agent/tools/search.py`
- `agent/tools/image_search.py`
- `skills/sandbox_client.py`
- `agent/image_gen.py`（inline `import httpx`）

---

## 5. PR 切分（一次 ship）

按 spec §3 PR 切分原则单 PR diff +1500/-500 阈值，本 PR 估算 **+2200/-1700**，**超出阈值上沿**但用户确认一刀切。Diff 主要构成：
- Gap 13 改造（70+ query 函数签名 + 38 文件 + 90 处业务切换）：~+1500/-1300
- Gap 14（runtime/single_flight + capabilities/banned_words + ImageRegistry + 3 处业务切换）：~+200/-90
- Gap 16（HTTPClient method-aware retry + 4 个新 capability 模块 + 4 处业务切换 + 删 sandbox_client）：~+500/-310

**确保 PR diff 可审**：commit 切分粒度小（13 个 commit，每个独立可 review），且每 commit 内变化是同质的（同质 = 同一类型的机械改造，例如「全部 query 删 session 参数」是一类，「全部业务切 tx/emit_tx」是另一类）。reviewer 可分 commit 看，不必看 4000 行 squashed diff。

### 5.1 commit 切分（13 个 commit）

1. `docs(spec): Phase 7d gap 13/14/16 design`（本文件 + plan 落盘）
2. `feat(runtime): db capability — tx() / current_session() / emit_tx()`（Gap 13.1）
3. `refactor(data/queries): drop session arg from all 9 modules`（Gap 13.2 mechanical — 70+ 函数签名）
4. `refactor(business): switch get_session/transactional_emit to tx/emit_tx`（Gap 13.3 — 38 文件 / 90 处机械切换）
5. `refactor(business): replace async_session() / direct session.* in 3 files`（Gap 13.4 — `chat/quick_search.py` / `nodes/persist_tos_files.py` / `life/proactive.py`：抽到 `data/queries/messages.py` 加新 query 函数后业务侧调用）
6. `chore(runtime): swap public surface — drop transactional_emit, export tx + emit_tx`（Gap 13 收尾，`runtime/__init__.py` 删 `transactional_emit` 导出 + 添加 `tx` / `emit_tx` 导出；`OutboxEmitter` / `transactional_emit` 仍存在但仅作为 framework 内部 API，业务区禁 import）
7. `feat(runtime): single_flight capability`（Gap 14.1）
8. `feat(capabilities): banned_words`（Gap 14.2）
9. `refactor(infra/image): ImageRegistry self-fetches redis`（Gap 14.3，构造签名 `(message_id, redis)` → `(message_id)`）
10. `refactor(business): switch SETNX/Lua/SMEMBERS/ImageRegistry to capabilities`（Gap 14.4 — `nodes/memory_pipelines.py` / `nodes/safety.py` / `chat/context.py`）
11. `feat(capabilities): http method-aware retry + web_search / image_search / sandbox`（Gap 16.1，HTTPClient 升级 + 4 个新 capability 模块）
12. `refactor(agent/tools+skills): use web_search / image_search / sandbox capability`（Gap 16.2）—— 涉及 `agent/tools/search.py` / `agent/tools/image_search.py` / `agent/tools/sandbox.py` / `agent/image_gen.py` / `skills/renderer.py`；删 `skills/sandbox_client.py`；同步更新 `tests/unit/test_skill_renderer.py`（3 处 `@patch` 目标改成 `app.capabilities.sandbox.run`，mock 返回 `SandboxResult(...)`）
13. `chore(ci): grep gate for Gap 13+14+16 closed`（baseline 三项归零，提到 closed-gap-zero job；新增 §6.1 的 5 类 grep 全部 == 0）

### 5.2 每 commit 验收

- 单元 / contract test 全 green
- ruff 通过
- 本 commit 关闭的 grep gate 加进 CI（commit 11 兜底统一加）

---

## 6. CI grep gate

### 6.1 closed-gap-zero（本 PR ship 后立即转）

业务区 = `apps/agent-service/app/{nodes,agent,chat,life,memory,skills}/`

```bash
# === Gap 13 — DB session 完全闭合 ===
# 任何 session 来源 import（包括 framework 的 current_session — 业务不能直接拿 session）
grep -rn "from app\.data\.session import\|from app\.data import session" 业务区 | wc -l == 0
grep -rn "from app\.runtime\.db import.*current_session\|from app\.runtime import.*current_session" 业务区 | wc -l == 0
# outbox 内部 API 也禁业务直接 import（防止业务自己 new OutboxEmitter 绕过 emit_tx 的 in-tx 强制）
grep -rn "from app\.runtime\.outbox import\|from app\.runtime import.*transactional_emit" 业务区 | wc -l == 0
# 任何 session 工厂 / 拿 session 调用
grep -rn "get_session(\|async_session(\|AsyncSessionLocal\|current_session(" 业务区 | wc -l == 0
# AsyncSession 类型暴露
grep -rn "AsyncSession" 业务区 | wc -l == 0
# 兜底：业务区禁止任何 SQLAlchemy session-style 操作（抓常见变量名 session/s/db 别名）
# 这条是 belt-and-suspenders；正常情况下前两条已堵住 session 来源，第 3 条不该有命中
grep -rnE "\bsession\.(execute|commit|add|merge|flush|rollback)\(" 业务区 | wc -l == 0
grep -rnE "\bs\.(execute|commit|add|merge|flush|rollback)\(" 业务区 | wc -l == 0
# transactional_emit 已被 emit_tx 取代
grep -rn "transactional_emit" 业务区 | wc -l == 0

# === Gap 14 — Redis 业务区零 import ===
grep -rn "from app\.infra\.redis import\|from app\.infra import redis" 业务区 | wc -l == 0
grep -rn "redis\.set(.*nx=True\|redis\.eval(\|redis\.smembers(\|redis\.sadd(" 业务区 | wc -l == 0

# === Gap 16 — httpx 业务区零 import ===
grep -rn "import httpx\|from httpx" 业务区 | wc -l == 0
# 业务区禁直接构造 HTTPClient（防止业务自己 new 一个 HTTPClient 直接调 sandbox /exec
# 绕过 sandbox.run 的零 retry / 各域 capability 的 method 决策）
grep -rn "from app\.capabilities\.http import\|from app\.capabilities import http" 业务区 | wc -l == 0
```

**允许命中区**：`runtime/`（db / outbox / single_flight）+ `capabilities/`（http / banned_words / web_search / image_search / sandbox）+ `infra/`（image / redis / config / lane）+ `data/`（session / queries）。

**关于 HTTPClient 的特殊约束**：HTTPClient 是 framework primitive（lane / trace 注入 + retry 策略），业务区**禁止直接构造** —— 必须通过域 capability（`web_search` / `image_search` / `sandbox` / `read_webpage` / `rerank` / `image_gen` 内部上传）调用。这条不是单纯避免 imports 蔓延，是堵掉「业务自己 new HTTPClient + post 到 sandbox `/exec`」这类绕过零 retry 设计的对手路径。

### 6.2 baseline 收尾

`.github/grep-baselines.json` 删除 `gap_13_get_session` / `gap_14_redis_setnx_business` / `gap_16_httpx_business` 三行；剩 `gap_19_create_task_business: 5`（留给 7e）。

---

## 7. 验证（dev 泳道 drill）

### 7.1 强制 drill 项（每项必须 dev 泳道真实跑过）

| 业务路径 | 触发方式 | 验证点 |
|---|---|---|
| commit_abstract（多条原子写 + emit_tx） | 让赤尾沉淀一条抽象记忆 | DB 三表（abstract / edge / outbox）原子可见；vectorize-worker 收到事件 |
| commit_abstract 中途 raise | 手动改代码注入 raise（drill 完 revert） | DB 完全无残留；outbox 无脏 row |
| drift_check single_flight | 短时间内并发触发同 (chat, persona) drift | 1 个跑、1 个 rescheduled |
| afterthought_check single_flight | 同上 | 同上 |
| banned_words 检查 | 飞书发命中违禁词的消息 | pre-safety 拦截 |
| web_search | 让赤尾搜一个网页 | tool 调用成功，HTTPClient retry / lane / trace 注入正常 |
| image_search | 让赤尾搜图 | 同上 |
| sandbox skill 调用 | 让赤尾跑一个 skill | sandbox HTTPClient + LaneRouter 路由通 |
| 节点内并发查 DB | 构造 unit test：**调用方不在外层 tx**，`asyncio.gather(branch_a(), branch_b())`，每分支自带 `async with tx():` | 两分支独立 session 顺序拿连接；返回各自结果 |
| nested tx 子事务 | 构造 unit test：外层 `async with tx():` 起后写一行 A；嵌一层 `async with tx():` 写 B 然后 raise；外层 `try/except` 捕获后再写 C | A 可见 / B 因 SAVEPOINT 回滚 / C 可见；外层最终 commit |

> **不上集成测试的反例**：「外层 tx 内直接 gather」期望 raise，但实际 race-dependent（可能串行通过、可能多种 SQLAlchemy 错误），测试 flaky。该规则靠 docstring + code review 约束。

### 7.2 Langfuse trace 检查

`memory_repo` 风格用例（commit_abstract）trace 仍包含完整链路（pre-safety → chat → emit AbstractMemoryCommitted → vectorize），不能因 contextvar session 改造丢 trace。

---

## 8. 已知风险与不留隐患

### 8.1 contextvar session 在 asyncio.gather 下的并发坑

SQLAlchemy AsyncSession 不支持并发使用。如果业务在同一 tx 内 `asyncio.gather` 多 query → InvalidRequestError。

**应对**：
- db.py docstring 明示规则
- query 函数 docstring 同样标注
- code review 重点扫描业务侧 `asyncio.gather` 与 query 调用的搭配
- 不强制 lint（false positive 太多：gather 里调 LLM / HTTP capability 不需要警告）

**不留隐患**：drill §7.1 第 9 项专门验证 safety 4 路并发分支各自进 tx 不撞坑。

### 8.2 长事务包外部 IO

业务作者可能把 LLM 调用 / HTTPClient 调用塞进 `async with tx():` 块。

**应对**：
- spec / db.py docstring 明示「tx 块内禁止外部 IO」
- 不强制 lint（误报太多）
- 给 `tx()` 加 elapsed warning：超过 N 秒 log warning（N = 5）

### 8.3 `auto_tx()` 兜底导致业务忘 tx

业务想要原子但忘写 `async with tx():`，多条 query 各自独立事务，部分失败留脏数据。

**应对**：
- `emit_tx` 必须在 tx 内（已强制）—— 任何写 emit 的业务都必须显式 tx
- 纯写多条无 emit 的场景：靠 review + drill
- 文档明示「multi-write 场景必须显式 tx，否则脏数据风险」

### 8.4 emit_tx 与 transactional_emit 共存窗口

commit 4-5 期间业务侧逐步切；commit 5 收 transactional_emit 改 internal。**不允许业务侧两种 API 共存**：commit 4 收尾必须把 7 个业务文件全切完；commit 5 是简单的 rename + visibility change。

### 8.5 lifespan / Runtime.run() 双入口（feedback_main_vs_runtime_run_dual_entry）

新加的 `single_flight` / `banned_words` / capability HTTP 都不依赖 lifecycle hook，无双入口风险。`runtime/db.py` 的 contextvar 是 module-level，不需要 bootstrap hook。**确认无新 lifespan 改动**。

### 8.6 数据库连接池影响

`auto_tx()` 让单条 query 自己开事务 → 短事务，连接立刻归还。pool_size=10 理论够用。

但业务侧 90 处改造中如果某些原本是 1 个 with 块包 5 条 query 的，改成 `auto_tx` 会变成 5 个独立连接获取 → 短期连接池压力上升。**drill 期间监控 pool 使用率**，必要时调 pool_size。

---

## 9. 不在本 spec 范围

- Gap 10 (streaming segment) → 7e
- Gap 17 (/health builtin) → 7e
- Gap 19 (asyncio.create_task / Future race) → 7e
- 跨边界挂账：lark-server publish trace_id / RabbitMQ delayed-message plugin runbook / agent-service /admin/* 开发机不可达 → 仍 pending（见 `project_dataflow_phase7.md`）

---

## 10. 完成后 memory 更新

- `project_dataflow_phase7.md` 的 7d 行从 pending → shipped，记 PR 编号 + prod 版本号
- 删除该文件 §「7d 范围」整段（迁到 done 摘要）
- 7e 上手前另开 spec
