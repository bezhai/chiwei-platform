"""End-to-end: emit -> publish_debounce -> handler -> consumer for drift / afterthought.

Uses fake redis + fake mq stubs. Covers spec §5.5 cases:
- single emit -> single fire after debounce_seconds
- multiple emits within window -> single fire (latest wins, others stale-drop)
- max_buffer hit -> immediate fire
- phase2 contention -> raise DebounceReschedule -> _do_reschedule -> next fire
- per-(chat, persona) isolation
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.memory_triggers import DriftTrigger
from app.runtime.debounce import (
    DebounceReschedule, _build_handler, publish_debounce,
)
from app.runtime.node import NODE_REGISTRY, _NODE_META, node
from app.runtime.wire import WireSpec


@pytest.fixture(autouse=True)
def _node_registry_isolation():
    """Snapshot/restore NODE_REGISTRY + _NODE_META so @node decorations
    on inline test consumers don't leak across tests."""
    nodes_snapshot = set(NODE_REGISTRY)
    meta_snapshot = dict(_NODE_META)
    yield
    NODE_REGISTRY.clear()
    NODE_REGISTRY.update(nodes_snapshot)
    _NODE_META.clear()
    _NODE_META.update(meta_snapshot)


class _FakeRedis:
    """In-memory redis stub for the 4 atomic Lua scripts used by debounce.

    Discriminates the script by content + numkeys:
      * _PUBLISH_LUA       — contains "INCR"
      * _CLAIM_LUA         — 2 keys, has GET+SET, no DEL
      * _CONDITIONAL_DEL_LUA — contains literal redis.call('DEL'
      * _RESCHEDULE_CAS_LUA — 1 key, has GET+SET
    """

    def __init__(self):
        self.kv: dict[str, object] = {}
        self.ttls: dict[str, int] = {}

    async def eval(self, script, numkeys, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])

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

        if "redis.call('DEL'" in script:  # _CONDITIONAL_DEL_LUA
            current = self.kv.get(keys[0])
            if current == argv[0]:
                self.kv.pop(keys[0], None)
                self.kv.pop(keys[1], None)
                return 1
            return 0

        if "GET" in script and "SET" in script and len(keys) == 2:
            # _CLAIM_LUA: stale check + clear count
            current = self.kv.get(keys[0])
            if current != argv[0]:
                return 0
            self.kv[keys[1]] = 0
            self.ttls[keys[1]] = int(argv[1])
            return 1

        if "GET" in script and "SET" in script and len(keys) == 1:
            # _RESCHEDULE_CAS_LUA: CAS swap latest
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
    """In-memory mq stub: stores published bodies in a list."""

    def __init__(self):
        self.published: list[tuple] = []  # list of (route, body, headers, delay_ms)

    async def publish(self, route, body, headers=None, delay_ms=None):
        self.published.append((route, body, headers, delay_ms))


class _FakeMessage:
    """Minimal aio_pika message stub matching what _build_handler needs."""

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


def _patch_runtime(monkeypatch, redis_fake, mq_fake):
    monkeypatch.setattr(
        "app.runtime.debounce.get_redis", AsyncMock(return_value=redis_fake)
    )
    monkeypatch.setattr("app.runtime.debounce.mq", mq_fake)
    monkeypatch.setattr(
        "app.runtime.debounce.trace_id_var", MagicMock(get=lambda: "")
    )
    monkeypatch.setattr(
        "app.runtime.debounce.lane_var", MagicMock(get=lambda: "")
    )


# ---------------------------------------------------------------------------
# 5 e2e cases (spec §5.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_single_emit_one_fire(monkeypatch):
    """Single emit -> handler claims latest -> consumer fires once."""
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    _patch_runtime(monkeypatch, redis_fake, mq_fake)

    fired: list[DriftTrigger] = []

    @node
    async def consumer(t: DriftTrigger) -> None:
        fired.append(t)

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 5},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )

    await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))
    assert len(mq_fake.published) == 1
    assert mq_fake.published[0][3] == 60_000

    handler = _build_handler(w, consumer)
    _route, body, headers, _delay = mq_fake.published[0]
    msg = _FakeMessage(body, headers or {})
    await handler(msg)

    assert len(fired) == 1
    assert fired[0].chat_id == "c1"


@pytest.mark.asyncio
async def test_e2e_multiple_emits_one_fire(monkeypatch):
    """Within debounce window, only the latest publish's fire wins;
    earlier triggers are stale-dropped by the atomic claim."""
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    _patch_runtime(monkeypatch, redis_fake, mq_fake)

    fired: list[DriftTrigger] = []

    @node
    async def consumer(t: DriftTrigger) -> None:
        fired.append(t)

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 100},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )

    for _ in range(3):
        await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))

    handler = _build_handler(w, consumer)

    # 前 2 条 message 被新 publish 作废，atomic claim fail → drop
    for _route, body, headers, _delay in mq_fake.published[:2]:
        msg = _FakeMessage(body, headers or {})
        await handler(msg)
    assert len(fired) == 0

    # 最后一条 latest 匹配 → fire
    _route, body, headers, _delay = mq_fake.published[2]
    msg = _FakeMessage(body, headers or {})
    await handler(msg)
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_e2e_max_buffer_immediate_fire(monkeypatch):
    """count 跨 max_buffer 阈值 → publish 内 atomic 复位 + body.fire_now=True
    + delay_ms=0；之后下一条 publish 重新攒 count，不再 fire_now。"""
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    _patch_runtime(monkeypatch, redis_fake, mq_fake)

    @node
    async def consumer(t: DriftTrigger) -> None:
        return None

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 3},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )

    for _ in range(3):
        await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))

    last = mq_fake.published[-1]
    assert last[1]["fire_now"] is True
    assert last[3] == 0

    # 后续再 publish：count 从 0 重新累计，不再 fire_now
    await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))
    assert mq_fake.published[-1][1]["fire_now"] is False
    assert mq_fake.published[-1][3] == 60_000


@pytest.mark.asyncio
async def test_e2e_phase2_contention_via_debounce_reschedule(monkeypatch):
    """phase2 跑期间收到第二个 fire → consumer raise DebounceReschedule →
    handler 跑 _do_reschedule（CAS swap latest + publish 新 delay）→
    第二条 message 拿到 fresh latest，consumer 正常处理。"""
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    _patch_runtime(monkeypatch, redis_fake, mq_fake)

    call_log: list[str] = []

    @node
    async def consumer(t: DriftTrigger) -> None:
        call_log.append(t.chat_id)
        if len(call_log) == 1:
            raise DebounceReschedule(
                DriftTrigger(chat_id=t.chat_id, persona_id=t.persona_id)
            )

    w = WireSpec(
        data_type=DriftTrigger,
        consumers=[consumer],
        debounce={"seconds": 60, "max_buffer": 100},
        debounce_key_by=lambda e: f"k:{e.chat_id}",
    )

    handler = _build_handler(w, consumer)

    # publish 1 → handler 1：consumer raise Reschedule → _do_reschedule
    # → CAS swap + publish 2
    await publish_debounce(w, consumer, DriftTrigger(chat_id="c1", persona_id="p1"))
    msg1 = _FakeMessage(mq_fake.published[0][1], mq_fake.published[0][2] or {})
    await handler(msg1)

    assert len(call_log) == 1
    assert len(mq_fake.published) == 2

    # publish 2 → handler 2：consumer 正常 return
    msg2 = _FakeMessage(mq_fake.published[1][1], mq_fake.published[1][2] or {})
    await handler(msg2)

    assert len(call_log) == 2


@pytest.mark.asyncio
async def test_e2e_per_key_isolation(monkeypatch):
    """两个 (chat_id, persona_id) 各自的 debounce 状态互不干扰：
    其中一个 key 的 publish 不影响另一个 key 的 latest / count。"""
    redis_fake = _FakeRedis()
    mq_fake = _FakeMq()
    _patch_runtime(monkeypatch, redis_fake, mq_fake)

    fired: list[tuple[str, str]] = []

    @node
    async def consumer(t: DriftTrigger) -> None:
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
    for _route, body, headers, _delay in mq_fake.published:
        await handler(_FakeMessage(body, headers or {}))

    assert sorted(fired) == [("c1", "p1"), ("c2", "p2")]
