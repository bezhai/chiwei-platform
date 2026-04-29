import json
from contextlib import asynccontextmanager

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.runtime.debounce import (
    DebounceReschedule, _route_for, _DEFAULT_TTL_SECONDS, publish_debounce,
    _do_reschedule, _build_handler,
)
from app.runtime.node import NODE_REGISTRY, _NODE_META, node
from app.runtime.wire import WireSpec
from app.domain.memory_triggers import DriftTrigger


@pytest.fixture(autouse=True)
def _node_registry_isolation():
    """每个测试前后 snapshot/restore NODE_REGISTRY + _NODE_META，
    防止 @node 装饰过的内联 consumer 跨测试累积污染（Task 10 按
    app_name 过滤启动 consumer 时会受影响）。"""
    nodes_snapshot = set(NODE_REGISTRY)
    meta_snapshot = dict(_NODE_META)
    yield
    NODE_REGISTRY.clear()
    NODE_REGISTRY.update(nodes_snapshot)
    _NODE_META.clear()
    _NODE_META.update(meta_snapshot)


async def _drift_check_stub(t: DriftTrigger) -> None:
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
           route.rk == "debounce.drift_trigger._drift_check_stub"
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

    fake_redis.eval.assert_awaited_once()
    args = fake_redis.eval.call_args
    assert args.args[1] == 2  # numkeys
    assert "debounce:latest:DriftTrigger:drift:c1:p1" in args.args
    assert "debounce:count:DriftTrigger:drift:c1:p1" in args.args
    assert 86400 in args.args
    assert 3 in args.args

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
    fake_publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# _build_handler tests (Task 9)
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Minimal aio_pika message stub."""

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


def _make_handler_wire(consumer):
    return WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )


@pytest.mark.asyncio
async def test_handler_atomic_claim_success_runs_consumer_then_conditional_del(monkeypatch):
    fake_redis = AsyncMock()
    # _CLAIM_LUA = 1 (claimed); _CONDITIONAL_DEL_LUA = 1 (deleted)
    fake_redis.eval = AsyncMock(side_effect=[1, 1])
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    consumer_called: list = []

    @node
    async def consumer(trigger: DriftTrigger) -> None:
        consumer_called.append(trigger)

    w = _make_handler_wire(consumer)
    handler = _build_handler(w, consumer)
    msg = _make_message("trig-1",
                        {"chat_id": "c1", "persona_id": "p1"},
                        "k:c1")
    await handler(msg)

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

    consumer_called: list = []

    @node
    async def consumer(trigger: DriftTrigger) -> None:
        consumer_called.append(trigger)

    w = _make_handler_wire(consumer)
    handler = _build_handler(w, consumer)
    msg = _make_message("trig-stale",
                        {"chat_id": "c1", "persona_id": "p1"},
                        "k:c1")
    await handler(msg)

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

    consumer_called: list = []

    @node
    async def consumer(trigger: DriftTrigger) -> None:
        consumer_called.append(trigger)

    w = _make_handler_wire(consumer)
    handler = _build_handler(w, consumer)
    msg = _make_message("trig-now",
                        {"chat_id": "c1", "persona_id": "p1"},
                        "k:c1", fire_now=True)
    await handler(msg)

    # claim 仍然执行（fire_now 不绕过 stale check）
    assert fake_redis.eval.await_count == 2
    assert len(consumer_called) == 1


@pytest.mark.asyncio
async def test_handler_consumer_raises_skips_conditional_del(monkeypatch):
    """Consumer 抛非 DebounceReschedule 异常 → 跳过 conditional DEL → DLQ 路径。"""
    fake_redis = AsyncMock()
    fake_redis.eval = AsyncMock(return_value=1)  # claim succeeded
    monkeypatch.setattr("app.runtime.debounce.get_redis",
                        AsyncMock(return_value=fake_redis))

    @node
    async def consumer(trigger: DriftTrigger) -> None:
        raise RuntimeError("boom")

    w = _make_handler_wire(consumer)
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
    # claim succeeded (1), 然后 _do_reschedule 内部 CAS swap = 1
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

    @node
    async def consumer(trigger: DriftTrigger) -> None:
        raise DebounceReschedule(new_data)

    w = _make_handler_wire(consumer)
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


# ---------------------------------------------------------------------------
# start_debounce_consumers / stop_debounce_consumers (Task 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_debounce_consumers_filters_by_app_name(monkeypatch):
    """start_debounce_consumers(app_name) 用 nodes_for_app 过滤；
    其他 app 的 wire 不启动 consumer。"""
    from app.runtime.wire import clear_wiring, wire
    from app.runtime.placement import clear_bindings
    from app.runtime.debounce import (
        start_debounce_consumers, stop_debounce_consumers,
    )

    clear_wiring()
    clear_bindings()

    @node
    async def my_drift_check(t: DriftTrigger) -> None:
        return None

    wire(DriftTrigger).debounce(
        seconds=60, max_buffer=5,
        key_by=lambda e: f"k:{e.chat_id}",
    ).to(my_drift_check)

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

    await stop_debounce_consumers()

    # 启动到不同 app（vectorize-worker）→ wire 被过滤掉
    fake_mq.connect.reset_mock()
    fake_mq.consume.reset_mock()
    fake_mq.declare_route.reset_mock()

    await start_debounce_consumers(app_name="vectorize-worker")

    fake_mq.connect.assert_not_awaited()
    fake_mq.consume.assert_not_awaited()

    await stop_debounce_consumers()
    clear_wiring()
    clear_bindings()
