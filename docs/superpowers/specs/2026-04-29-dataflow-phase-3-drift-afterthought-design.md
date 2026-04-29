# Dataflow Phase 3 — Drift / Afterthought 进 Graph

**状态**: Draft v1 (2026-04-29)
**前置**: PR #203 (Phase 2 safety) + PR #204 (followup) shipped to prod
**后续**: Phase 4 Life Engine / Schedule / Glimpse

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
@node
async def drift_check(trigger: DriftTrigger) -> None:
    """Single-flight per (chat, persona)."""
    lock_key = f"phase2:drift:{trigger.chat_id}:{trigger.persona_id}"
    redis = get_redis()
    if not await redis.set(lock_key, "1", nx=True, ex=600):
        logger.info(
            "drift_check: already running for chat_id=%s persona=%s, skip",
            trigger.chat_id, trigger.persona_id,
        )
        return
    try:
        await _run_drift(trigger.chat_id, trigger.persona_id)
    finally:
        await redis.delete(lock_key)


@node
async def afterthought_check(trigger: AfterthoughtTrigger) -> None:
    lock_key = f"phase2:afterthought:{trigger.chat_id}:{trigger.persona_id}"
    redis = get_redis()
    if not await redis.set(lock_key, "1", nx=True, ex=900):
        logger.info(
            "afterthought_check: already running for chat_id=%s persona=%s, skip",
            trigger.chat_id, trigger.persona_id,
        )
        return
    try:
        await _generate_fragment(trigger.chat_id, trigger.persona_id)
    finally:
        await redis.delete(lock_key)
```

**业务幂等机制（single-flight）**：
- redis SETNX 锁，TTL = 600s (drift) / 900s (afterthought)，作为兜底防泄漏（异常 finally 释放 + TTL 兜底）
- 拿不到锁 → 立即 return；本次 fire 信号被作废**是预期行为**，因为：
  1. fire 是幂等检查信号，不携带不可恢复的事件 payload
  2. 下游业务从 db 拉时间窗口数据，不依赖 fire 携带具体内容
  3. phase2 跑完释放锁 + 后续新事件来 → 下一轮 timer 到期会读到所有积累的内容
- TTL 选 600/900 是给 LLM 调用足够 timeout 余量（drift 约 10s LLM、afterthought 约 30s LLM；TTL 是"卡死兜底"层级，不是预期路径）

### 3.3 Wiring（`apps/agent-service/app/wiring/memory.py`）

```python
from app.runtime.placement import bind
from app.runtime.wire import wire
from app.domain.memory_triggers import DriftTrigger, AfterthoughtTrigger
from app.nodes.memory_pipelines import drift_check, afterthought_check
from app.infra.config import settings

bind(drift_check, "agent-service")
bind(afterthought_check, "agent-service")

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

`bind(*, "agent-service")` 让 `start_debounce_consumers(app_name="agent-service")` 只在 agent-service 主进程启动这俩 consumer（arq-worker / vectorize-worker 同镜像但不启动）。

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

**新增** 校验段：

```python
# .debounce() 必须搭配 key_by（DSL 层面已经强制，这里 defensive）
# .debounce() 跟 .durable() 互斥：debounce 自己实现 mq 跨进程
# .debounce() 跟 .with_latest() 互斥：handler 单 input，跟 durable 同样限制
# .debounce() 数据类型必须 transient：fire 信号语义
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
    if w.with_latest:
        raise GraphError(
            f"wire({w.data_type.__name__}).debounce().with_latest(...): "
            f"debounce handlers are single-input; .with_latest() not supported"
        )
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

# Lua: 原子设置 latest + 增加 count，返回 new_count
_PUBLISH_LUA = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
local n = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ARGV[2])
return n
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

    new_count = await redis.eval(
        _PUBLISH_LUA, 2,
        redis_latest, redis_count,
        trigger_id, seconds * 2,
    )

    body = {
        "trigger_id": trigger_id,
        "data": data.model_dump(mode="json"),
        "key": key,
        "fire_now": new_count >= max_buffer,
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
                fire_now = payload.get("fire_now", False)

                redis = get_redis()
                redis_latest = f"debounce:latest:{data_cls.__name__}:{key}"
                redis_count = f"debounce:count:{data_cls.__name__}:{key}"

                if not fire_now:
                    latest = await redis.get(redis_latest)
                    if latest is None or latest.decode() != trigger_id:
                        # 被新事件作废 — 这是 debounce 的核心机制，不是错误
                        logger.debug(
                            "debounce drop: stale trigger_id for %s (key=%s)",
                            data_cls.__name__, key,
                        )
                        return

                # Fire: 清状态 + 调下游
                await redis.delete(redis_latest, redis_count)
                obj = data_cls(**data_dict)
                logger.info(
                    "debounce fire: %s key=%s fire_now=%s",
                    data_cls.__name__, key, fire_now,
                )
                await consumer(**{param_name: obj})
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

#### 3.4.4 emit.py 集成

`app/runtime/emit.py` 在 wire dispatch 循环里加一支：

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

#### 3.4.5 关键决策记录

- **不用 sweeper / leader election**：mq x-delayed-message 自己负责"到时投递"，没有后台 task
- **不用 redis ZSET**：仅一对 SET (latest) + INCR (count)，状态紧凑
- **作废检查在 consumer 端**：拿到 delay 消息时比对 redis latest，不匹配就 drop（消息正常 ack）—— 这是 publish-then-drop 模式，QPS 不高场景可接受（用户已确认）
- **max_buffer 提前触发**：count 在 publish 端就达阈值时直接发 `fire_now=True` + delay=0 消息，consumer 拿到时跳过作废检查
- **TTL = `seconds * 2`**：覆盖 mq delay 时间 + 一点 buffer，过期 redis 自然清；事件停止后状态不会泄漏
- **不写 `insert_idempotent`**：transient data type，没有 pg 表（runtime/migrator.py 跳过 transient）
- **Atomic Lua**：`SET latest` + `INCR count` + `EXPIRE count` 三步原子，避免并发 emit 时 count 偏差

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

```python
# 旧
asyncio.create_task(drift.on_event(chat_id, persona_id))
asyncio.create_task(afterthought.on_event(chat_id, persona_id))

# 新
from app.runtime.emit import emit
from app.domain.memory_triggers import DriftTrigger, AfterthoughtTrigger
asyncio.create_task(emit(DriftTrigger(chat_id=chat_id, persona_id=persona_id)))
asyncio.create_task(emit(AfterthoughtTrigger(chat_id=chat_id, persona_id=persona_id)))
```

`from app.memory.drift import drift` / `from app.memory.afterthought import afterthought` 这两行 import 跟着删除。

`asyncio.create_task` 包一层是因为 emit 是 async 调用，post_actions 现有调用方期望 fire-and-forget；保持现状不改调用习惯。

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
- redis latest / count 标记：用现有 redis 实例（已配置持久化 `--save` 参数，跟其他 redis state 同等级别），broker 重启不丢
- 重启过程：未到期的 delay 消息留在 broker → agent-service 重启后 consumer 重新绑定 → 到期投递 → 比对 redis latest → fire 或 drop

**跟现状对比**：旧 in-memory debouncer 重启后 `_buffers` / `_timers` 全丢，待触发的 drift / afterthought 全丢一轮（直到下次新消息再触发）。新架构 0 丢失。

### 4.2 迁移期 in-flight 状态

切换瞬间（部署 = 杀 Pod，CLAUDE.md "部署铁律"）：
- 旧进程内 buffer / timer 全丢（重启即灰飞烟灭）
- 新进程从 emit() 开始走新链路

**不需要兜底**：drift / afterthought 是"持续触发"管线，下次新消息来时自然触发新一轮。Migration 期间最多丢一两个未触发的 cycle，业务无感（drift 的偏差是几个分钟一档；afterthought 没生成的 fragment 等下次 chat 又会重启 timer）。

### 4.3 Single-flight 锁泄漏

redis SETNX 锁 TTL = 600s/900s 是兜底防泄漏。正常路径 `finally` 释放；异常路径靠 TTL 自动过期。

**已知边界 case**：
- 节点 LLM 卡住超过 TTL → 锁自然过期，新一轮 fire 拿到锁开始处理 → **可能重复 LLM 调用**（旧 phase2 跑完 finally 释放，但锁此时已过期失效，DEL 是 no-op，无害）
- 进程崩溃 → 锁靠 TTL 释放
- TTL 选 600/900 是给 LLM "异常 timeout" 余量；正常路径都在 30s 内

### 4.4 LLM 调用 / langfuse trace

业务节点（drift_check / afterthought_check）的 LLM 调用复用现有 trace 机制：
- `_run_drift` 调 `generate_voice` —— langfuse trace 已经接入
- `_generate_fragment` 调 `Agent(_AFTERTHOUGHT_CFG).run(...)` —— langfuse trace 已经接入

dataflow 重构不改 LLM 调用本身，只换调度层。Trace context 通过 mq headers (`trace_id` / `lane`) 跨进程传递（参考 `runtime/durable.py:73-79` 同模式），跟 Phase 2 范式一致。

### 4.5 Mq 消息堆积观察

Publish-then-drop 模式下，drift 高频时 mq 上会有"被作废的延迟消息"。指标解读：
- `debounce_drift_trigger_drift_check_<lane>` 队列消费速率 = 上游 emit 速率
- 真正 fire 调下游 = 消费量减去"作废 drop"
- "进 vs fire" 比例 = debounce 压制率（通常应该很高，比如 10:1 或更高）

**告警**：PR #202 的 `RabbitmqConsumerDown` 队列正则当前覆盖 `durable_*` + `memory_fragment_vectorize` + `memory_abstract_vectorize`，**不含 `debounce_*`**。Phase 3 deploy 前需扩展 regex 包含 `debounce_*`，否则 debounce consumer 挂了不告警。这步随 spec 验收 checklist 跟踪。

### 4.6 监控影响

- 旧链路无 mq 流量（drift / afterthought 进程内）
- 新链路：`debounce_drift_trigger_drift_check` + `debounce_afterthought_trigger_afterthought_check` 两个新队列出现
- redis key 数量增长：`debounce:latest:*` + `debounce:count:*` 数量 ≈ 活跃 (chat_id, persona_id) 对数 × 2，TTL 控制总量

### 4.7 灰度

- 泳道部署 `phase3-debounce`，单镜像（agent-service，三个 Deployment 同步发布）
- bind dev bot，跑 §4.5 四类
- 重启验证：触发后立即 `kubectl rollout restart` → 看 mq 上 delay 消息存活 + 重启后 consumer 接管 → drift_check 仍触发

## 5. 测试

### 5.1 Runtime 单元测试（`tests/runtime/test_debounce.py`，新增）

- `publish_debounce`（fake redis + fake mq）：
  - 单事件：写 redis latest + count + mq publish delay 消息
  - max_buffer 触发：count >= max_buffer 时 publish 一条 `fire_now=True` + delay=0 消息
  - 多事件覆盖：第二次 publish 后 redis latest 是新 trigger_id，旧 trigger_id 的 delay 消息后续消费时会 drop
- `_build_handler`（fake redis + fake mq + mock consumer）：
  - latest 匹配 → 调下游 + 删 redis 状态
  - latest 不匹配 → ack drop + 不调下游
  - latest 为 None（TTL 过期）→ ack drop + 不调下游
  - `fire_now=True` → 跳过作废检查直接 fire
  - consumer 抛异常 → mq nack 进 DLQ（验证 message.process(requeue=False) 行为）

### 5.2 compile_graph 校验测试

- `wire(T).debounce(...)` 通过校验
- `wire(T).debounce().durable()` raise GraphError
- `wire(T).debounce().with_latest(X)` raise GraphError
- 非 transient data type 上 `.debounce()` raise GraphError
- 缺 `key_by` raise GraphError

### 5.3 节点单元测试

- `drift_check`（mock redis + mock `_run_drift`）：
  - 拿到锁 → 调 `_run_drift` → 释放锁
  - 没拿到锁 → return（`_run_drift` 不被调用）
  - `_run_drift` 抛异常 → finally 释放锁
- `afterthought_check`：同上模式
- `_run_drift` / `_generate_fragment` / `_recent_timeline` / `_build_scene`：原 `tests/memory/` 单测搬到 `tests/nodes/test_memory_pipelines.py`，断言不变

### 5.4 端到端集成（in-memory mq + redis fake）

- emit DriftTrigger → wait debounce timer → 确认 drift_check 被调
- 多次 emit 在 debounce window 内 → 仅触发一次 drift_check
- max_buffer 触发：emit max_buffer 次 → 立即触发 drift_check
- 跨 (chat_id, persona_id) 的并发：互不干扰

### 5.5 泳道集成测试

部署到 `phase3-debounce` 泳道，bind dev bot 跑下面四类：
1. 群里发 1 条消息后等 N 秒 → drift 触发（`make logs APP=agent-service KEYWORD="debounce fire"` + `KEYWORD="drift_check"` 各一条）
2. 群里连发 N 条（>= identity_drift_max_buffer） → drift 立即触发（`fire_now` 路径）
3. 群里发够 15 条消息 → afterthought 立即触发
4. drift_check 跑到一半再发消息 → 看 single-flight return 日志 + 锁释放后下一轮触发

重启验证：
5. 触发 drift（步骤 1）后立即 `kubectl rollout restart deployment/agent-service-phase3-debounce` → drift_check 仍按时被触发

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
- [ ] `RabbitmqConsumerDown` 告警 regex 扩展包含 `debounce_*`（K8s 上 `kubectl apply` 下发，跟 PR #202 同模式）
