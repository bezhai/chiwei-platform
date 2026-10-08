"""Phase 6 v4 Gap 2: emit() and a consumer that runs in another app."""
from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime import Data, Key, bind, emit, node, wire
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
async def test_emit_inprocess_when_consumer_in_same_app(monkeypatch):
    """Consumer in this app's binding (or default fall-through) → in-process call."""
    captured: list = []

    @node
    async def x_handler(r: _XReq) -> None:
        captured.append(r)

    wire(_XReq).to(x_handler)
    # Don't bind — falls through to default app (agent-service).

    monkeypatch.setenv("APP_NAME", "agent-service")
    import sys

    sys.modules["app.runtime.emit"].reset_emit_runtime()

    await emit(_XReq(x_id="x2"))

    assert len(captured) == 1
    assert captured[0].x_id == "x2"


@pytest.mark.asyncio
async def test_emit_raises_when_consumer_in_other_app_without_durable(monkeypatch):
    """A0 W4a：Consumer in another app + 无 durable → emit 必须 raise RuntimeError，
    不允许 silent skip（contract "禁止静默兜底"）。"""
    called: list = []

    @node
    async def x_handler(r: _XReq) -> None:
        called.append(r)

    wire(_XReq).to(x_handler)  # no .durable()
    bind(x_handler).to_app("vectorize-worker")

    monkeypatch.setenv("APP_NAME", "agent-service")
    import sys

    sys.modules["app.runtime.emit"].reset_emit_runtime()

    with pytest.raises(RuntimeError, match="cross-app dispatch has no transport"):
        await emit(_XReq(x_id="x3"))

    assert called == []
