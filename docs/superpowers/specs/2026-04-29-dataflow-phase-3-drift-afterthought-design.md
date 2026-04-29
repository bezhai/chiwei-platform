# Dataflow Phase 3 — Drift / Afterthought 进 Graph

**状态**: Draft v3 (2026-04-29，吸收 reviewer 第 2 轮 7 条意见)
**前置**: PR #203 (Phase 2 safety) + PR #204 (followup) shipped to prod
**后续**: Phase 4 Life Engine / Schedule / Glimpse

**v3 关键变化（vs v2）**：
- handler 不再 `latest.decode()`：项目 redis client `decode_responses=True`，`get()` 返回 str；改成 `latest != trigger_id` 直接比（修 round-2 H1）
- 业务节点 single-flight 锁改 token 化：`SET lock_key token NX EX` + Lua compare-and-delete 释放，避免 LLM 卡到 TTL 之外时旧 finally 误删新锁（修 round-2 H2）
- spec 明说 `publish_debounce` 是 best-effort：mq publish 失败时这一轮丢，下次新事件自然恢复（修 round-2 H3）
- compile_graph `.debounce()` 合法形态收紧：exactly one @node consumer + 无 sinks / sources / durable / as_latest / with_latest（修 round-2 M4 + M5 + M6）
- `Route` 末尾加 `lane_fallback: bool = True` 默认字段（NamedTuple，向后兼容；不改成 dataclass）（修 round-2 L7）

**v2 关键变化（vs v1）**：
- handler 改成"consumer 完成后 conditional DEL"，DEL 仅作用于自己的 trigger_id（修 round-1 H1 phase2 期间事件丢 + H2 DLQ 不可重放）
- 业务节点锁冲突时 `await emit(SameTrigger)` 重发，让 timer 链恢复（修 round-1 H1）
- max_buffer 阈值触发用 atomic reset count = 0，每轮只一条 immediate fire（修 round-1 H3）
- TTL 改 `max(seconds * 2, 86400)` 覆盖 24h 停机窗口（修 round-1 H4）
- debounce route 跳过 lane TTL fallback，infra/rabbitmq.py 加 `lane_fallback=False` 选项（修 round-1 M5）
- post_actions 封装 `_emit_memory_trigger` helper 包 try/except，避免 fire-and-forget 异常黑洞（修 round-1 M6）
- wiring 删错误的 `bind(...)` 调用（drift_check / afterthought_check 走 DEFAULT_APP，不需要 bind）（修 round-1 L7）
- 部署验证步骤改用 `/ops` skill，去掉 `kubectl rollout restart` / `kubectl apply`（修 round-1 L8）

## 1. 背景

Phase 0+1 落地了 dataflow runtime 框架（`app/runtime/*`）+ vectorize 管线；Phase 2 把 safety 链路改成节点 + `.durable()` wire。Phase 3 把 drift / afterthought 这两条"in-memory 两阶段 debouncer 管线"改造成 graph 节点，**首次落地 `.debounce()` runtime**。

`graph.py:198-209` 当前在 `compile_graph` 阶段拒绝任何带 `.debounce()` 的 wire（"unimplemented wire features"），Phase 3 要把这段拒绝拆掉并实装。

**验收点**（roadmap）：
- 进程重启不丢待触发事件（旧 in-memory `_buffers` / `_timers` 重启即灰飞烟灭）
- `app/memory/debounce.py` 整文件删除（`DebouncedPipeline` 基类消失）
- drift / afterthought 在 `chat/post_actions.py` 的入口换成 `emit(DriftTrigger)` / `emit(AfterthoughtTrigger)`

## 2. 现状

### 2.1 In-memory debouncer 基类（`app/memory/debounce.py`）

`DebouncedPipeline` ABC，state 全在进程内 dict：
- `_buffers: dict[str, int]` — per-key 计数
- `_timers: dict[str, asyncio.Task]` — per-key 计时器
- `_phase2_running: set[str]` — per-key 处理锁
- key 格式：`f"{chat_id}:{persona_id}"`

两阶段语义：
- Phase 1（可中断）：收事件 → 累加 buffer → 起 N 秒 timer；下次事件来重置 timer，或 buffer 达到 max_buffer 立即进 phase2
- Phase 2（不可中断）：调子类的 `process(chat_id, persona_id, event_count)`；处理期间新事件只 buffer，处理完后若 buffer > 0 自动起下一轮（`_enter_phase2:104-112`）

### 2.2 Drift（`app/memory/drift.py`）

`_Drift(DebouncedPipeline)` 单例 `drift = _Drift()`：
- `debounce_seconds = settings.identity_drift_debounce_seconds`
- `max_buffer = settings.identity_drift_max_buffer`
- `process()` 调 `_run_drift(chat_id, persona_id)`：读最近 1 小时群消息 + 最近 2 小时本 persona 回复 → 调 `app.memory.voice.generate_voice(persona_id, recent_context, source="drift")`

### 2.3 Afterthought（`app/memory/afterthought.py`）

`_Afterthought(DebouncedPipeline)` 单例 `afterthought = _Afterthought()`：
- `DEBOUNCE_SECONDS = 300` / `MAX_BUFFER = 15` / `LOOKBACK_HOURS = 2`
- `process()` 调 `_generate_fragment(chat_id, persona_id)`：读最近 2 小时群消息 → 调 LLM 生成 conversation-grain fragment → `insert_fragment` + `enqueue_fragment_vectorize`

### 2.4 调用入口（`app/chat/post_actions.py:80,88`）

```python
asyncio.create_task(drift.on_event(chat_id, persona_id))
asyncio.create_task(afterthought.on_event(chat_id, persona_id))
```

## 3. 目标架构

```
DriftTrigger        --debounce(N, M, key)--> drift_check
AfterthoughtTrigger --debounce(300, 15, key)--> afterthought_check
```

`.debounce()` runtime 语义：上游 emit → mq 延迟消息 + redis "latest trigger id" 标记 → 消费时比对 latest 决定 fire / drop。重启不丢（mq broker 持久化 + redis 持久化）。

**Fire 信号语义**：`.debounce()` 触发时下游 `@node` 收到的是"最后一条没被作废的 publish 携带的 Data"（实现上：每次 publish 都把 data 编进 mq body，handler 比对 redis latest 后只让最新那条幸存，其他的 ack drop）。下游不携带积累期内全部 payload。drift / afterthought 都是"幂等检查信号"业务模型 —— 节点拿到 (chat_id, persona_id) 就够，时间窗口内的具体内容自己去 db 拉。

| | Drift | Afterthought |
|---|---|---|
| 触发方 | post_actions `emit(DriftTrigger)` | post_actions `emit(AfterthoughtTrigger)` |
| Wire 修饰符 | `.debounce(...)` | `.debounce(...)` |
| Debounce 参数 | `seconds=settings.identity_drift_debounce_seconds`, `max_buffer=settings.identity_drift_max_buffer` | `seconds=300`, `max_buffer=15` |
| Key | `f"drift:{chat_id}:{persona_id}"` | `f"afterthought:{chat_id}:{persona_id}"` |
| 处理逻辑 | `_run_drift` 搬迁 | `_generate_fragment` 搬迁 |
| Single-flight | redis SETNX `phase2:drift:*` ex=600 | redis SETNX `phase2:afterthought:*` ex=900 |
| 跨进程 | mq delay 消息 | mq delay 消息 |
| 数据类型 | `DriftTrigger` (transient) | `AfterthoughtTrigger` (transient) |

### 3.1 Data 类（`apps/agent-service/app/domain/memory_triggers.py`）

```python
from typing import Annotated
from app.runtime.data import Data, Key

class DriftTrigger(Data):
    chat_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True

class AfterthoughtTrigger(Data):
    chat_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True
```

`Meta.transient = True` 表示不落 pg 表 —— Fire 信号是 transient 语义，状态全在 mq 上的 delay 消息 + redis latest 标记上；进 pg 表既冗余也不符合 "trigger 信号" 的本质。

`Annotated[..., Key]` 仅满足 Data 基类的 schema 要求（dedup_hash 计算需要 key 字段）；`.debounce()` 实际作用的 key 在 wire 上的 `key_by` lambda 里再算一次（包含 persona_id），不复用 `Key` 字段。

### 3.2 节点（`apps/agent-service/app/nodes/memory_pipelines.py`）

整合 drift / afterthought 处理逻辑到此文件。结构：

- module-level 私有 helper：`_run_drift` / `_generate_fragment` / `_recent_timeline` / `_recent_persona_replies` / `_build_scene` —— 从 `app/memory/drift.py` + `afterthought.py` 整体搬迁
- module-level 私有常量：`_AFTERTHOUGHT_CFG` / `_LOOKBACK_HOURS` / `_CST` —— 从 afterthought.py 搬迁
- 节点 `drift_check` / `afterthought_check`

```python
# app/nodes/memory_pipelines.py 内 module-level 共享 Lua（compare-and-delete lock）
_LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
end
return 0
"""

@node
async def drift_check(trigger: DriftTrigger) -> None:
    """Single-flight per (chat, persona). 锁冲突 → self-emit 让 timer 链恢复。"""
    lock_key = f"phase2:drift:{trigger.chat_id}:{trigger.persona_id}"
    token = uuid.uuid4().hex
    redis = get_redis()
    if not await redis.set(lock_key, token, nx=True, ex=600):
        # phase2 在跑 → self-emit 一个 trigger 让 timer 链自己恢复，
        # 避免 phase2 期间被 fire 的事件因为锁冲突而真丢
        logger.info(
            "drift_check: phase2 in flight for chat_id=%s persona=%s, requeue",
            trigger.chat_id, trigger.persona_id,
        )
        from app.runtime.emit import emit
        await emit(DriftTrigger(
            chat_id=trigger.chat_id, persona_id=trigger.persona_id,
        ))
        return
    try:
        await _run_drift(trigger.chat_id, trigger.persona_id)
    finally:
        # compare-and-delete: 仅删自己持有的 token 对应的锁，避免 LLM
        # 卡到 TTL 之外时旧 finally 误删 *新* fire 拿到的同 key 锁
        await redis.eval(_LOCK_RELEASE_LUA, 1, lock_key, token)


@node
async def afterthought_check(trigger: AfterthoughtTrigger) -> None:
    lock_key = f"phase2:afterthought:{trigger.chat_id}:{trigger.persona_id}"
    token = uuid.uuid4().hex
    redis = get_redis()
    if not await redis.set(lock_key, token, nx=True, ex=900):
        logger.info(
            "afterthought_check: phase2 in flight for chat_id=%s persona=%s, requeue",
            trigger.chat_id, trigger.persona_id,
        )
        from app.runtime.emit import emit
        await emit(AfterthoughtTrigger(
            chat_id=trigger.chat_id, persona_id=trigger.persona_id,
        ))
        return
    try:
        await _generate_fragment(trigger.chat_id, trigger.persona_id)
    finally:
        await redis.eval(_LOCK_RELEASE_LUA, 1, lock_key, token)
```

**业务幂等机制（single-flight + 锁冲突 self-emit + token 化释放）**：

- redis `SET lock_key token NX EX <ttl>`，TTL = 600s (drift) / 900s (afterthought)，兜底防泄漏（异常 finally 释放 + TTL 自动过期）
- **token 化释放**（reviewer round-2 H2 修法）：每次拿锁生成 uuid token；finally 调 Lua compare-and-delete，仅当 redis 上仍是自己 token 时才 DEL。LLM 卡超过 TTL 时锁已失效 + 新 fire 已 SETNX 拿到新 token，旧 finally 不会误删新锁
- **锁冲突 → self-emit 同类型 trigger**：让 publish 路径写新 latest + 重置 count + 起新一轮 timer。等 phase2 跑完 + 新一轮 timer 到期 → handler 拿到，比对 latest match，跑 consumer，拿到锁正常处理。这是修 round-1 H1 (phase2 期间事件丢) 的关键：不让 fire 信号"消化掉就消失"
- self-emit 不会无限循环：handler 端是 conditional DEL（仅删自己 trigger_id；见 §3.4.3 Lua），phase2 释放锁后下一轮 fire 拿到的 trigger_id 跟 latest 一致，正常处理
- self-emit 链最坏情况：phase2 跑超 N 秒（drift LLM ~10s vs `settings.identity_drift_debounce_seconds` 通常 60s+，afterthought LLM ~30s vs 300s，安全余量充足）。极端 case 下 self-emit 链按 N 秒拍数延迟，最终 phase2 完成时正常处理，无丢失
- TTL 选 600/900 是给 LLM 卡死余量（drift 约 10s LLM、afterthought 约 30s LLM；TTL 是异常 timeout 层级，不是预期路径）

### 3.3 Wiring（`apps/agent-service/app/wiring/memory.py`）

```python
from app.runtime.wire import wire
from app.domain.memory_triggers import DriftTrigger, AfterthoughtTrigger
from app.nodes.memory_pipelines import drift_check, afterthought_check
from app.infra.config import settings

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

`drift_check` / `afterthought_check` 不需要 `bind(...)` —— `placement.DEFAULT_APP = "agent-service"`，未 bind 的 @node 默认归属 DEFAULT_APP（见 `placement.py:21,57`）。`start_debounce_consumers(app_name="agent-service")` 调用时 `nodes_for_app("agent-service")` 自动包含这俩节点。arq-worker / vectorize-worker 启动时传自己的 app_name，过滤掉这俩 wire，不会重复消费。

`.debounce()` 不跟 `.durable()` 组合 —— `.debounce()` 自己实现 mq 跨进程能力（见 §3.4），不需要 `.durable()` 的 `insert_idempotent` dedup（因为 fire 信号是 transient）。

### 3.4 `.debounce()` runtime（`apps/agent-service/app/runtime/debounce.py`，新增）

#### 3.4.1 DSL 扩展（`app/runtime/wire.py`）

```python
@dataclass
class WireSpec:
    ...
    debounce: dict | None = None              # 已有
    debounce_key_by: Callable | None = None   # 新增

class WireBuilder:
    def debounce(
        self, *,
        seconds: int,
        max_buffer: int,
        key_by: Callable[[Data], str],
    ) -> WireBuilder:
        self._spec.debounce = {"seconds": seconds, "max_buffer": max_buffer}
        self._spec.debounce_key_by = key_by
        return self
```

`key_by` 必须传（不带默认值），强制业务在 wire 层显式声明 partition key。

#### 3.4.2 graph.py 校验

**删除** `graph.py:198-209` 的 unimplemented raise。

**新增** 校验段（reviewer round-2 M4 + M5 + M6 一起处理 —— `.debounce()` 必须收紧到一个明确的合法形态：1 consumer + 无 sinks/sources + transient + 互斥所有 wire 修饰符）：

```python
# .debounce() 合法形态：
#   exactly one @node consumer
#   data type Meta.transient = True
#   key_by 必填
#   不跟 .durable() / .as_latest() / .with_latest() / sinks / sources / fan-out 组合
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

#### 3.4.3 Runtime 实现要点

```python
# runtime/debounce.py

from app.infra.redis import get_redis
from app.infra.rabbitmq import Route, mq, current_lane, lane_queue
from app.runtime.naming import to_snake
from app.runtime.node import inputs_of
from app.runtime.wire import WireSpec
from app.api.middleware import lane_var, trace_id_var

_consumer_tags: list[tuple[Any, str]] = []

# Per-wire route: queue=debounce_<snake_data>_<consumer>, rk 同 pattern
def _route_for(w: WireSpec, consumer) -> Route:
    data_snake = to_snake(w.data_type.__name__)
    return Route(
        queue=f"debounce_{data_snake}_{consumer.__name__}",
        rk=f"debounce.{data_snake}.{consumer.__name__}",
    )

_DEFAULT_TTL_SECONDS = 86400  # 24h，覆盖典型停机/恢复窗口（reviewer H4）

# Lua (publish): 原子设置 latest + 增加 count；count 达 max_buffer 时
# 原子重置 count = 0 并返回 fire_now=1，保证每轮只一条 immediate fire 消息
# 携带正确的"触发 trigger_id"，不会被 backlog 中旧 fire_now 消息重复触发。
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

# Lua (handler conditional DEL): 仅当 latest == trigger_id 时 DEL latest+count。
# 关键作用：consumer 完成后 cleanup 不会误删 self-emit 写入的新 latest，
# 也保证 consumer 抛异常时 latest 保留供 DLQ replay (reviewer H1 + H2)
_CONDITIONAL_DEL_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('DEL', KEYS[2])
    return 1
end
return 0
"""


async def publish_debounce(w: WireSpec, consumer, data: Data) -> None:
    """上游 emit 路径。"""
    key = w.debounce_key_by(data)
    seconds = w.debounce["seconds"]
    max_buffer = w.debounce["max_buffer"]
    trigger_id = uuid.uuid4().hex
    redis = get_redis()
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
        # fire_now 仅由 Lua atomic 判定，每轮最多一条 immediate fire 消息携带 True
        "fire_now": bool(fire_now_flag),
    }
    headers = {
        "trace_id": trace_id_var.get() or "",
        "lane": lane_var.get() or "",
        "data_type": type(data).__name__,
    }
    delay_ms = 0 if body["fire_now"] else seconds * 1000
    await mq.publish(_route_for(w, consumer), body, headers=headers, delay_ms=delay_ms)


def _build_handler(w: WireSpec, consumer):
    data_cls = w.data_type
    param_name = next(iter(inputs_of(consumer)))

    async def handler(message):
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
                # fire_now 标志只是 publish 端给 consumer 端"绕开 delay"
                # 的提示；consumer 端**仍然必须做 stale check**，否则
                # backlog 中旧 fire_now 消息（来自上一轮已重置的 count）
                # 会重复触发（reviewer H3 修法）
                # NOTE: fire_now=True 的消息走 delay=0 路径，跟其他 publish
                # 形成的 latest 写入在 redis 上是同一原子序列；只要 latest
                # 还指向当前 trigger_id 就 fire。
                _fire_now = payload.get("fire_now", False)  # 留一个变量供日志
                _ = _fire_now

                redis = get_redis()
                redis_latest = f"debounce:latest:{data_cls.__name__}:{key}"
                redis_count = f"debounce:count:{data_cls.__name__}:{key}"

                # Stale check: latest != trigger_id → 被新事件作废，drop
                # NOTE: 项目 redis client 配 decode_responses=True
                # (infra/redis.py:30)，redis.get() 返回 str (None 表示 key 不存在)，
                # 不需要 .decode()。直接 != trigger_id 比即可（reviewer round-2 H1）。
                latest = await redis.get(redis_latest)
                if latest != trigger_id:
                    logger.debug(
                        "debounce drop stale: %s key=%s trigger_id=%s latest=%s",
                        data_cls.__name__, key, trigger_id, latest,
                    )
                    return

                obj = data_cls(**data_dict)
                logger.info(
                    "debounce fire: %s key=%s trigger_id=%s",
                    data_cls.__name__, key, trigger_id,
                )

                # Consumer 在 try 内调用：成功 / 锁冲突 self-emit 都正常返回；
                # 抛异常 → 跳过 conditional DEL，进 DLQ → DLQ replay 时
                # latest 还在，可以重新跑（reviewer H2 修法）
                await consumer(**{param_name: obj})

                # 完成后 conditional DEL：只删自己 trigger_id 对应的状态。
                # 如果 consumer 自己 self-emit 覆盖了 latest（锁冲突路径），
                # 这里 DEL 不动，新 latest+count 保留供下一轮 fire。
                await redis.eval(
                    _CONDITIONAL_DEL_LUA, 2,
                    redis_latest, redis_count,
                    trigger_id,
                )
            finally:
                trace_id_var.reset(t_tok)
                lane_var.reset(l_tok)
    return handler


async def start_debounce_consumers(app_name: str | None = None) -> None:
    """对所有 .debounce() wire 启动 mq consumer。逻辑参考 durable.start_consumers。"""
    if _consumer_tags:
        raise RuntimeError("consumers already started; call stop first")
    from app.runtime.graph import compile_graph
    graph = compile_graph()
    allowed = nodes_for_app(app_name) if app_name else None

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
    for queue, tag in _consumer_tags:
        try:
            await queue.cancel(tag)
        except Exception as e:
            logger.warning("failed to cancel debounce consumer %s: %s", tag, e)
    _consumer_tags.clear()
    await asyncio.sleep(0)
```

#### 3.4.4 emit.py 集成 + infra/rabbitmq.py 扩展

**emit.py 加 debounce 分支**：

```python
async def emit(data):
    for w in [x for x in WIRING_REGISTRY if x.data_type == type(data)]:
        if w.debounce is not None:
            from app.runtime.debounce import publish_debounce
            for consumer in w.consumers:
                await publish_debounce(w, consumer, data)
            continue   # debounce wire 不走 in-process 派发
        if w.durable:
            ...existing...
        if w.sinks:
            ...existing...
        ...in-process consumer dispatch...
```

**infra/rabbitmq.py 扩展（reviewer M5 修法）**：

`_build_queue_args` 现把所有 lane queue 写成 `x-message-ttl=10000` + dead-letter 回 prod rk（`infra/rabbitmq.py:121`）。debounce 队列里有 ≥ 300s 延迟消息，泳道 consumer 暂停 10s 就被 fallback 截到 prod 队列，跨泳道副作用。

修法：`declare_route` 加 `lane_fallback: bool = True` 参数；False 时调 `_build_queue_args` 的变体，跳过 `x-message-ttl` + `x-dead-letter-*` 两项（lane queue 仍设 `x-expires=24h` 防泳道下了之后队列残留）。`_ensure_lane_queue` 同样需要支持，否则 producer 端 lazy-declare 会还是走 fallback。

```python
def _build_queue_args(prod_rk: str, lane: str | None,
                     lane_fallback: bool = True) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if lane:
        extra["x-expires"] = _NON_PROD_EXPIRES_MS
    if not lane:
        return {"x-dead-letter-exchange": DLX_NAME, **extra}
    if not lane_fallback:
        # debounce 队列: 不要 ttl-back-to-prod，长延迟消息要在自己 lane 上等
        return extra
    return {
        "x-message-ttl": _LANE_FALLBACK_TTL_MS,
        "x-dead-letter-exchange": EXCHANGE_NAME,
        "x-dead-letter-routing-key": prod_rk,
        **extra,
    }

class _RabbitMQ:
    async def declare_route(self, route: Route, *,
                            lane_fallback: bool = True) -> None:
        ...
        arguments=_build_queue_args(route.rk, lane, lane_fallback)
        ...

    async def _ensure_lane_queue(self, route: Route, lane: str, *,
                                 lane_fallback: bool = True) -> None:
        ...同样传透...
```

`runtime/debounce.start_debounce_consumers` 调 `mq.declare_route(route, lane_fallback=False)`；publish 路径需要 producer 端 lazy declare 也走 `lane_fallback=False`，方案要么 publish 时显式调 `_ensure_lane_queue`（破坏现有封装），要么把"非 fallback"信息存在 `Route` 上，由 `mq.publish` 自己感知。

**实现注意**（reviewer round-2 L7）：当前 `Route` 是 `NamedTuple`（`infra/rabbitmq.py:39`），不是 dataclass。NamedTuple 末尾可加带默认值的字段且向后兼容（现有 `Route("queue", "rk")` 调用不变）：

```python
class Route(NamedTuple):
    queue: str
    rk: str
    lane_fallback: bool = True   # 新增；默认 True 不破坏现有 Route 实例
```

`_ensure_lane_queue` 读 `route.lane_fallback` 决定参数。debounce route 创建时显式设 `Route(queue=..., rk=..., lane_fallback=False)`。

#### 3.4.5 关键决策记录

- **不用 sweeper / leader election**：mq x-delayed-message 自己负责"到时投递"，没有后台 task
- **不用 redis ZSET**：仅一对 SET (latest) + INCR (count)，状态紧凑
- **stale check 由 trigger_id 比对决定**：拿到 delay 消息时比对 redis latest，不匹配就 drop（消息正常 ack）—— publish-then-drop 模式，QPS 不高场景可接受（用户已确认）。**fire_now 消息也走 stale check**（避免 backlog 中旧 fire_now 重复触发，reviewer H3）
- **max_buffer atomic 重置 count**：Lua 在 `count >= max_buffer` 时原子 `SET count = 0`，保证每轮只一条 immediate fire 消息携带正确的 trigger_id；下一条 publish 从 1 重新攒
- **Conditional DEL on consumer success**：handler 调 consumer 完成后才 DEL，**且仅 DEL 自己 trigger_id 对应的状态**（Lua compare-and-delete）。这同时解决：
  - reviewer H1：consumer 锁冲突 self-emit 覆盖了 latest → conditional DEL 不动，新 latest+count 保留供下一轮 fire
  - reviewer H2：consumer 抛异常 → 跳过 DEL，DLQ replay 时 latest 还在
- **TTL = max(seconds * 2, 86400s = 24h)**：覆盖典型停机/恢复窗口（reviewer H4）；redis 内存占用低（活跃 chat × persona 量级，每 key 两个标量）
- **不写 `insert_idempotent`**：transient data type，没有 pg 表（runtime/migrator.py 跳过 transient）
- **debounce route 跳过 lane TTL fallback**：见 §3.4.4，调 `mq.declare_route(route, lane_fallback=False)` 让 lane queue 不带 `x-message-ttl=10000`，避免 300s 延迟消息被 lane fallback 截到 prod（reviewer round-1 M5）
- **publish 是 best-effort，不引 outbox**（reviewer round-2 H3）：`publish_debounce` 先 redis SET latest+count，后 mq publish。publish 失败时 redis latest 指向永不到达的 trigger_id：
  - 如果之后还有新事件来 → 新 emit 走 publish 路径覆盖 latest+count + 发新 delay 消息 → 自然恢复
  - 如果之后无新事件 → 这一轮 fire 真丢（drift / afterthought 业务上等价于"chat 完成后系统抖了一下"，最近 1-2 小时窗口内有任何新消息都会触发新一轮恢复）
  - 不引 outbox / Lua 回滚机制：drift / afterthought 是低关键 best-effort 业务，引 outbox 复杂度跟收益不匹配；Lua 回滚需要"上一个 trigger_id"快照，逻辑复杂且无法保证回滚跟 publish 失败原子（mq publish 抛异常时 redis 已经 INCR 了，回滚还得 DECR/restore）
- **lock token 化释放**：业务节点 SETNX 时存 uuid token，finally 用 Lua compare-and-delete（见 §3.2）；标准分布式锁模式，避免 LLM 卡到 TTL 之外时旧 finally 误删新锁（reviewer round-2 H2）

### 3.5 Main lifespan 改造（`app/main.py`）

新增 startup（在 `start_consumers(app_name=...)` 之后）：

```python
from app.runtime.debounce import start_debounce_consumers
await start_debounce_consumers(app_name="agent-service")
```

shutdown（在 `stop_consumers()` 之前）：

```python
from app.runtime.debounce import stop_debounce_consumers
await stop_debounce_consumers()
```

`start_consumers` (durable) 和 `start_debounce_consumers` (debounce) 共存：前者负责 `.durable()` wire 的 consumer，后者负责 `.debounce()` wire 的 consumer。两者独立维护各自的 `_consumer_tags`。

### 3.6 Post actions 接入（`app/chat/post_actions.py:80,88`）

封装 helper 包 emit，**避免 `asyncio.create_task(emit(...))` 失败被丢进 task exception 黑洞**（reviewer M6）：

```python
# app/chat/post_actions.py 新增 module-level helper
async def _emit_memory_trigger(trigger: Data) -> None:
    """fire-and-forget memory trigger emit. Failures are logged, not raised
    (post_actions 调用方语义就是 fire-and-forget)."""
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

# 旧调用点
asyncio.create_task(drift.on_event(chat_id, persona_id))
asyncio.create_task(afterthought.on_event(chat_id, persona_id))

# 新
from app.domain.memory_triggers import DriftTrigger, AfterthoughtTrigger
asyncio.create_task(_emit_memory_trigger(
    DriftTrigger(chat_id=chat_id, persona_id=persona_id)
))
asyncio.create_task(_emit_memory_trigger(
    AfterthoughtTrigger(chat_id=chat_id, persona_id=persona_id)
))
```

`from app.memory.drift import drift` / `from app.memory.afterthought import afterthought` 这两行 import 跟着删除。

`asyncio.create_task` 包 helper 而不是直接包 emit，是因为 helper 内部已 try/except，task 不会再产生未处理 exception（reviewer M6 的真正修法 —— v1 直接 `asyncio.create_task(emit(...))` 会让 redis/mq publish 失败变成事件循环噪声警告）。

### 3.7 旧 `app/memory/` 文件清理

**整文件删除**：
- `apps/agent-service/app/memory/debounce.py` — `DebouncedPipeline` 不再被任何代码引用
- `apps/agent-service/app/memory/drift.py` — `_Drift` 类 + `drift` 单例 + `_run_drift` / `_recent_timeline` / `_recent_persona_replies` 全部迁出（搬到 `nodes/memory_pipelines.py`）
- `apps/agent-service/app/memory/afterthought.py` — `_Afterthought` 类 + `afterthought` 单例 + `_generate_fragment` / `_build_scene` + 常量全部迁出

迁移前 grep 确认：
```bash
grep -rn "from app.memory.debounce\|from app.memory.drift\|from app.memory.afterthought" \
    apps/agent-service/app
```

只有 post_actions.py 的两行 import + 这三个文件自己 + 测试文件。post_actions 改完后旧 import 全部消失。

### 3.8 Settings / 常量

`settings.identity_drift_debounce_seconds` / `settings.identity_drift_max_buffer` 保留 —— wire 层 `.debounce(...)` 直接读 settings。

`afterthought.py` 里的 `DEBOUNCE_SECONDS = 300` / `MAX_BUFFER = 15` 在新 wire 上变成字面量 `seconds=300, max_buffer=15`，不再作为模块常量；如果后续要做 dynamic config，再单独提取。

`LOOKBACK_HOURS = 2` / `_AFTERTHOUGHT_CFG = AgentConfig(...)` 等业务常量随 `_generate_fragment` 一起搬到 `nodes/memory_pipelines.py`。

## 4. 失败模式 / 兼容性 / 迁移

### 4.1 重启不丢

- mq broker 持久化 delay 消息：`x-delayed-message` 插件本身把 delay 消息存在 broker 上，重启 agent-service 不丢
- redis latest / count 标记：用现有 redis 实例（生产已配置持久化），TTL = `max(seconds * 2, 86400)` = 至少 24h，覆盖典型停机/恢复窗口
- 重启过程：未到期的 delay 消息留在 broker → agent-service 重启后 consumer 重新绑定 → 到期投递 → 比对 redis latest → fire 或 drop

**TTL 边界 case**：如果 agent-service + redis + mq 同时挂超过 24h，redis latest 过期但 mq delay 消息还在，handler 拿到时比对 latest 为 None → drop。这种 case 业务上等价于"chat 安静了一整天后才恢复"，丢这一轮无影响（drift / afterthought 都是"持续触发"语义，下次新消息自然恢复）。

**跟现状对比**：旧 in-memory debouncer 重启后 `_buffers` / `_timers` 全丢，待触发的 drift / afterthought 全丢一轮（直到下次新消息再触发）。新架构在 24h 内停机不丢。

### 4.2 迁移期 in-flight 状态

切换瞬间（部署 = 杀 Pod，CLAUDE.md "部署铁律"）：
- 旧进程内 buffer / timer 全丢（重启即灰飞烟灭）
- 新进程从 emit() 开始走新链路

**不需要兜底**：drift / afterthought 是"持续触发"管线，下次新消息来时自然触发新一轮。Migration 期间最多丢一两个未触发的 cycle，业务无感（drift 的偏差是几个分钟一档；afterthought 没生成的 fragment 等下次 chat 又会重启 timer）。

### 4.3 Single-flight 锁泄漏 + self-emit 链行为

redis SETNX 锁 TTL = 600s/900s 是兜底防泄漏。正常路径 `finally` 释放；异常路径靠 TTL 自动过期。

**已知边界 case**：
- 节点 LLM 卡住超过 TTL → 锁自然过期，新一轮 fire 拿到锁开始处理 → **可能重复 LLM 调用**（旧 phase2 跑完 finally 释放，但锁此时已过期失效，DEL 是 no-op，无害）
- 进程崩溃 → 锁靠 TTL 释放
- TTL 选 600/900 是给 LLM "异常 timeout" 余量；正常路径都在 30s 内

**self-emit 链行为**（reviewer H1 修法的 runtime 视角）：

锁冲突时 drift_check / afterthought_check 调 `await emit(SameTrigger)`：
- 走 publish_debounce → 写新 latest（覆盖旧 trigger_id）+ INCR count + publish 新 delay 消息
- handler 当前 trigger_id 完成（`return`），调 conditional DEL → latest != 当前 trigger_id（已被 self-emit 覆盖）→ 不 DEL → 新 latest+count 保留
- 新 delay 消息 N 秒后到期 → handler 拿到 → 比对 latest match → 调 consumer → 拿到锁正常处理

self-emit 链最坏情况（phase2 跑超 N）：每轮 N 秒 publish 一次直到 phase2 完成。LLM 卡到 TTL 极限（600s/900s）也只是 N=60s/300s 的几次循环 publish，开销可忽略。

**self-emit 链不会**：
- 触发新 fire_now（self-emit 时 publish 端 INCR count，但 count 重新从 1 开始，不会立即达 max_buffer）
- 死循环（每次 self-emit 都走 N 秒 delay；phase2 释放后下一轮 fire 必然能拿锁）

### 4.4 LLM 调用 / langfuse trace

业务节点（drift_check / afterthought_check）的 LLM 调用复用现有 trace 机制：
- `_run_drift` 调 `generate_voice` —— langfuse trace 已经接入
- `_generate_fragment` 调 `Agent(_AFTERTHOUGHT_CFG).run(...)` —— langfuse trace 已经接入

dataflow 重构不改 LLM 调用本身，只换调度层。Trace context 通过 mq headers (`trace_id` / `lane`) 跨进程传递（参考 `runtime/durable.py:73-79` 同模式），跟 Phase 2 范式一致。

### 4.5 Lane TTL fallback 跟长延迟消息冲突

`infra/rabbitmq.py:_build_queue_args` 现在给所有 lane queue 写死 `x-message-ttl=10000` + dead-letter 回 prod rk —— 这是泳道下了之后让残留消息回流到 prod 处理的 fallback 机制（10s 后 retry to prod）。

**冲突**：debounce 队列里有 ≥ 300s 延迟消息，泳道下/重启时 consumer 暂停 10s 就被 fallback 截到 prod 队列；prod handler 从 message header 恢复 lane → 跑 prod 的 drift_check / afterthought_check 时还以为自己是泳道 lane → 跨泳道副作用。

**修法**（见 §3.4.4）：debounce route 在 `Route` NamedTuple 末尾的新字段 `lane_fallback=False` 上声明，`_build_queue_args` / `_ensure_lane_queue` / `declare_route` 全链路尊重这个 flag，跳过 `x-message-ttl` + `x-dead-letter-*`。lane queue 仍设 `x-expires=24h` 防泳道残留。

### 4.6 Mq 消息堆积观察

Publish-then-drop 模式下，drift 高频时 mq 上会有"被作废的延迟消息"。指标解读：
- `debounce_drift_trigger_drift_check_<lane>` 队列消费速率 = 上游 emit 速率
- 真正 fire 调下游 = 消费量减去"作废 drop"
- "进 vs fire" 比例 = debounce 压制率（通常应该很高，比如 10:1 或更高）

**告警**：PR #202 的 `RabbitmqConsumerDown` 队列正则当前覆盖 `durable_*` + `memory_fragment_vectorize` + `memory_abstract_vectorize`，**不含 `debounce_*`**。Phase 3 deploy 前需扩展 regex 包含 `debounce_*`，否则 debounce consumer 挂了不告警。这步随 spec 验收 checklist 跟踪。

### 4.7 监控影响

- 旧链路无 mq 流量（drift / afterthought 进程内）
- 新链路：`debounce_drift_trigger_drift_check` + `debounce_afterthought_trigger_afterthought_check` 两个新队列出现
- redis key 数量增长：`debounce:latest:*` + `debounce:count:*` 数量 ≈ 活跃 (chat_id, persona_id) 对数 × 2，TTL 控制总量

### 4.8 灰度

- 泳道部署 `phase3-debounce`，单镜像（agent-service，三个 Deployment 同步发布）
- bind dev bot，跑 §5.5 四类
- 重启验证：触发后通过 `/ops` skill 重启泳道 Pod → 看 mq 上 delay 消息存活 + 重启后 consumer 接管 → drift_check 仍触发（**禁止 `kubectl rollout restart`**，遵守 CLAUDE.md 基础设施约束）

## 5. 测试

### 5.1 Runtime 单元测试（`tests/runtime/test_debounce.py`，新增）

- `publish_debounce`（fake redis + fake mq）：
  - 单事件：写 redis latest + count + mq publish delay 消息
  - max_buffer 触发：count 达 max_buffer 时 Lua atomic 重置 count = 0 + publish 一条 `fire_now=True` + delay=0 消息（reviewer H3）
  - **max_buffer 之后下一条**：count 从 1 重新攒，不再 fire_now=True；只有这第一条 immediate fire 携带"那一轮"trigger_id
  - 多事件覆盖：第二次 publish 后 redis latest 是新 trigger_id，旧 trigger_id 的 delay 消息后续消费时会 drop
- `_build_handler`（fake redis + fake mq + mock consumer）：
  - latest 匹配 → 调下游 → conditional DEL 仅删自己 trigger_id
  - latest 不匹配 → ack drop + 不调下游
  - latest 为 None（TTL 过期）→ ack drop + 不调下游
  - **`fire_now=True` 但 trigger_id 已被 backlog 旧 fire_now 错位**：handler 仍 stale check → drop（reviewer H3）
  - **consumer 完成后 self-emit 覆盖 latest**：handler conditional DEL 跳过（latest != 自己），新 latest+count 保留
  - **consumer 抛异常**：跳过 conditional DEL（latest 保留供 DLQ replay）+ mq nack 进 DLQ（reviewer H2）
  - **consumer 锁冲突 self-emit 后正常返回**：handler 视为成功 → conditional DEL → latest 已被 self-emit 覆盖 → DEL 跳过

### 5.2 compile_graph 校验测试

- `wire(T).debounce(...)` (单 consumer + transient T + key_by) 通过校验
- `wire(T).debounce().durable()` raise GraphError
- `wire(T).debounce().as_latest()` raise GraphError（reviewer round-2 M4）
- `wire(T).debounce().with_latest(X)` raise GraphError
- `wire(T).debounce().to(Sink.mq("..."))` raise GraphError（reviewer round-2 M6）
- `wire(T).debounce().from_(Source.mq("..."))` raise GraphError
- `wire(T).debounce().to(c1, c2)` (fan-out) raise GraphError（reviewer round-2 M5）
- 同 DataType 两条 `.debounce()` wire 共存 raise GraphError（state 污染防护）
- 非 transient data type 上 `.debounce()` raise GraphError
- 缺 `key_by` raise GraphError

### 5.3 节点单元测试

- `drift_check`（mock redis + mock `_run_drift` + mock emit）：
  - 拿到锁 → 调 `_run_drift` → 释放锁（compare-and-delete 用自己的 token）
  - **没拿到锁 → 调 `emit(DriftTrigger(...))` self-emit → 不调 `_run_drift`**（reviewer round-1 H1）
  - `_run_drift` 抛异常 → finally 释放锁
  - **lock token 化**（reviewer round-2 H2）：mock `_run_drift` 跑超 TTL → 锁过期 → 模拟新 fire 占用 lock_key 写入新 token → 旧 finally 调 Lua → redis 上 token 已不是自己 → 不 DEL → 新锁保留
- `afterthought_check`：同上模式（self-emit AfterthoughtTrigger）
- `_run_drift` / `_generate_fragment` / `_recent_timeline` / `_build_scene`：原 `tests/memory/` 单测搬到 `tests/nodes/test_memory_pipelines.py`，断言不变

### 5.4 端到端集成（in-memory mq + redis fake）

- emit DriftTrigger → wait debounce timer → 确认 drift_check 被调
- 多次 emit 在 debounce window 内 → 仅触发一次 drift_check
- max_buffer 触发：emit max_buffer 次 → 立即触发一次 drift_check（不是多次）
- **phase2 抢占场景（reviewer H1 端到端）**：mock _run_drift sleep 远超 debounce → emit 一次拿锁开始跑 → emit 第二次锁冲突 self-emit → 等 timer + phase2 → 第二次 fire 拿到锁正常处理；最终 _run_drift 共调用 2 次（不是 1 次也不是 dead loop）
- 跨 (chat_id, persona_id) 的并发：互不干扰

### 5.5 泳道集成测试

部署到 `phase3-debounce` 泳道，bind dev bot 跑下面四类：
1. 群里发 1 条消息后等 N 秒 → drift 触发（`make logs APP=agent-service KEYWORD="debounce fire"` + `KEYWORD="drift_check"` 各一条）
2. 群里连发 N 条（>= identity_drift_max_buffer） → drift 立即触发（`fire_now` 路径）
3. 群里发够 15 条消息 → afterthought 立即触发
4. drift_check 跑到一半再发消息 → 看 single-flight return 日志 + 锁释放后下一轮触发

重启验证：
5. 触发 drift（步骤 1）后通过 `/ops` skill 重启泳道 Pod → drift_check 仍按时被触发

## 6. 部署 & 切换

**Phase 3 单镜像（agent-service），必须同步发布三个 Deployment**（CLAUDE.md "一镜像多服务同步" 铁律）：

| Deployment | Phase 3 改动影响 |
|---|---|
| `agent-service` | 启动 `start_debounce_consumers` + 业务节点接收 emit |
| `arq-worker` | 同镜像（`bind` 已限制只在 agent-service 启 consumer），但镜像层代码走查需确认无 regress |
| `vectorize-worker` | 同上 |

**Phase 3 不动 lark-server**。

切换步骤：

1. 泳道部署 `phase3-debounce` + bind dev bot，跑 §5.5 五类全过
2. 检查泳道 mq 上 `debounce_drift_trigger_drift_check_phase3-debounce` + `debounce_afterthought_trigger_afterthought_check_phase3-debounce` 队列正常 declare 且消费正常
3. ship：release `agent-service` / `arq-worker` / `vectorize-worker` 到 prod
4. 部署后 5min 观察：
   - `make logs APP=agent-service KEYWORD="debounce consumer started"` 出现两条
   - 群消息触发 drift_check / afterthought_check（看日志 + redis key 出现-消失）
   - mq 上 `debounce_*` 队列 message rate 跟群消息频率合理对应
5. 观察 24h，关注：
   - drift / afterthought 触发频率（vs 旧链路 baseline）
   - LLM 调用次数（vs 旧链路）—— single-flight 锁应减少同 key 并发
   - mq DLQ 是否有 debounce 消息进入
   - redis 内存：`debounce:latest:*` + `debounce:count:*` key 数量

回滚：单 PR 改动较多，但都是替换同一职责。回滚 = revert PR；schema 没动（transient），无 schema 影响。

## 7. 不在本期范围

- **`.debounce()` 跟 `.durable()` / `.with_latest()` 组合**：本期明确互斥（`compile_graph` 拒绝），将来如有需要再设计 runtime
- **持久化事件 payload 的 debounce 语义**：本期 fire 时只传"上游最后一条 Data"。如果将来出现需要"携带积累期内全部 payload"的业务，再扩展 runtime
- **drift / afterthought 内部 LLM prompt 调整**：本期不改业务逻辑，仅换调度层
- **`app/memory/voice.py` 重构**：drift 调的 `generate_voice` 跟 voice pipeline 共享，本期不动
- **跨副本 leader election**：本期 single-flight 用 redis SETNX 已是分布式 lock，不做更复杂的 leader election
- **Drift / Afterthought debounce 参数走 dynamic config**：本期保持 `settings.*` + 字面量，参数化留 followup
- **`debounce:*` redis key 的告警 / dashboard**：本期靠 mq 队列告警观察，redis key 监控留 followup

## 8. 验收 checklist

- [ ] `apps/agent-service/app/memory/debounce.py` 不存在
- [ ] `apps/agent-service/app/memory/drift.py` 不存在
- [ ] `apps/agent-service/app/memory/afterthought.py` 不存在
- [ ] `grep -rn "DebouncedPipeline\|_Drift\|_Afterthought\|drift\.on_event\|afterthought\.on_event" apps/agent-service/app` 零结果
- [ ] `grep -rn "_phase2_running\|_buffers\b\|_timers\b" apps/agent-service/app` 零结果（仅业务侧 single-flight redis lock）
- [ ] `apps/agent-service/app/runtime/debounce.py` 存在 + `compile_graph()` 接受 `.debounce()` wire
- [ ] `compile_graph` 拒绝：`.debounce().durable()` / `.debounce().with_latest()` / 非 transient data type 上的 `.debounce()` / 缺 `key_by`
- [ ] 泳道 `make logs APP=agent-service KEYWORD="debounce consumer started"` 出现 `debounce_drift_trigger_drift_check_<lane> -> drift_check` + `debounce_afterthought_trigger_afterthought_check_<lane> -> afterthought_check`
- [ ] §5.5 五类泳道场景全过（含重启不丢）
- [ ] redis 上 `debounce:latest:DriftTrigger:*` / `debounce:count:DriftTrigger:*` key 在事件期间出现 + fire 后消失 + 异常路径靠 TTL 清
- [ ] mq DLQ 在测试期间无 debounce 消息进入
- [ ] 单元测试覆盖 §5.1 + §5.2 + §5.3 全部场景
- [ ] `wire.py` 的 `WireBuilder.debounce` 签名包含 `key_by: Callable[[Data], str]`，DSL 层强制必填
- [ ] `app/runtime/emit.py` 的 wire dispatch 循环包含 `if w.debounce is not None` 分支
- [ ] `app/main.py` lifespan 同时启动 `start_consumers` (durable) 和 `start_debounce_consumers` (debounce)
- [ ] `RabbitmqConsumerDown` 告警 regex 扩展包含 `debounce_*`（通过 `/ops` 或运维流程下发，**不直接用 `kubectl apply`**；跟 PR #202 等价的告警下发方式由当时操作者按当前流程决定）
- [ ] `infra/rabbitmq.py` 加 `Route.lane_fallback` 字段 + `_build_queue_args` / `_ensure_lane_queue` / `declare_route` 全链路尊重；debounce route 用 `lane_fallback=False`
- [ ] `app/chat/post_actions.py` 调 `_emit_memory_trigger(...)` helper（包 try/except）而不是直接 `asyncio.create_task(emit(...))`
- [ ] `app/runtime/debounce.py` 的 publish Lua 在 `count >= max_buffer` 时原子 reset count = 0 并返回 fire_now=1
- [ ] `app/runtime/debounce.py` 的 handler 调 consumer 完成后才走 conditional DEL（仅删 `latest == trigger_id`），consumer 抛异常时跳过 DEL
- [ ] `drift_check` / `afterthought_check` 锁冲突时 self-emit 同类型 trigger（不直接 return）
- [ ] 业务节点 `finally` 走 Lua compare-and-delete 释放锁（仅当 redis 上仍是自己 token 时 DEL），**不能用裸 `redis.delete(lock_key)`**
- [ ] handler 里 stale check 用 `latest != trigger_id`，**不能调 `latest.decode()`**（项目 redis client `decode_responses=True`）
- [ ] `compile_graph` 拒绝：`.debounce().as_latest()` / `.debounce().to(Sink.*)` / `.debounce().from_(Source.*)` / `.debounce()` 多 consumer / 同 DataType 多条 `.debounce()` wire
- [ ] `Route` 末尾加 `lane_fallback: bool = True` 默认字段（NamedTuple 兼容；非 dataclass 改造）
