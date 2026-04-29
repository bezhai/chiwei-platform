"""emit() routes debounce wires to publish_debounce, not in-process /
sink / durable dispatch.

compile_graph 已经保证 .debounce() 不能跟 .durable() / .as_latest() /
.when() / sink 等组合，emit 这边的责任就是看到 ``w.debounce is not None``
就走独立的 mq publish 分支并跳过其他所有 dispatch。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.domain.memory_triggers import DriftTrigger
from app.runtime.emit import emit, reset_emit_runtime
from app.runtime.node import NODE_REGISTRY, _NODE_META, node
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring, wire


@pytest.fixture(autouse=True)
def _isolation():
    """Snapshot/restore NODE_REGISTRY + _NODE_META + wires + bindings +
    compiled graph cache, so inline @node consumers don't leak across tests."""
    nodes_snap = set(NODE_REGISTRY)
    meta_snap = dict(_NODE_META)
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    try:
        yield
    finally:
        NODE_REGISTRY.clear()
        NODE_REGISTRY.update(nodes_snap)
        _NODE_META.clear()
        _NODE_META.update(meta_snap)
        clear_wiring()
        clear_bindings()
        reset_emit_runtime()


@pytest.mark.asyncio
async def test_emit_debounce_wire_calls_publish_debounce(monkeypatch):
    """emit(DriftTrigger) 在 debounce wire 上必须路由到 publish_debounce，
    consumer 本身不能被 in-process 直接 await。"""

    @node
    async def my_drift_check(t: DriftTrigger) -> None:
        # 如果 emit 错误地走了 in-process 分支，这里会被直接调用 —
        # mock publish_debounce 检测不到，但下面的 consumer_called
        # 会暴露这个 bug。
        consumer_called.append(t)

    consumer_called: list = []

    wire(DriftTrigger).debounce(
        seconds=60,
        max_buffer=5,
        key_by=lambda e: f"k:{e.chat_id}",
    ).to(my_drift_check)

    fake_publish_debounce = AsyncMock()
    monkeypatch.setattr(
        "app.runtime.debounce.publish_debounce", fake_publish_debounce
    )

    t = DriftTrigger(chat_id="c1", persona_id="p1")
    await emit(t)

    fake_publish_debounce.assert_awaited_once()
    args = fake_publish_debounce.call_args.args
    # publish_debounce(w, consumer, data)
    assert args[1] is my_drift_check
    assert args[2] is t
    # in-process consumer 必须没被直接调用
    assert consumer_called == []
