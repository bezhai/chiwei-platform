"""Phase 6 v4 Gap 2: emit cross-process via wire source.mq."""
from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock

import pytest

from app.runtime import Data, Key, Source, bind, emit, node, wire
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
    """consumer bound to a different app + wire has Source.mq → emit auto-publishes."""

    @node
    async def x_handler(r: _XReq) -> None:
        pass

    wire(_XReq).to(x_handler).from_(Source.mq("x_queue"))
    bind(x_handler).to_app("vectorize-worker")

    monkeypatch.setenv("APP_NAME", "agent-service")

    fake_publish = AsyncMock()
    import sys

    emit_mod = sys.modules["app.runtime.emit"]
    monkeypatch.setattr(emit_mod, "_mq_publish_for_source", fake_publish)

    emit_mod.reset_emit_runtime()

    await emit(_XReq(x_id="x1"))

    fake_publish.assert_awaited_once()
    args = fake_publish.await_args.args
    assert args[0].kind == "mq"
    assert args[0].params["queue"] == "x_queue"
    assert args[1].x_id == "x1"


@pytest.mark.asyncio
async def test_emit_inprocess_when_consumer_in_same_app(monkeypatch):
    """Consumer in this app's binding (or default fall-through) → in-process call, no publish."""
    captured: list = []

    @node
    async def x_handler(r: _XReq) -> None:
        captured.append(r)

    wire(_XReq).to(x_handler).from_(Source.mq("x_queue"))
    # Don't bind — falls through to default app (agent-service).

    monkeypatch.setenv("APP_NAME", "agent-service")
    fake_publish = AsyncMock()
    import sys

    emit_mod = sys.modules["app.runtime.emit"]
    monkeypatch.setattr(emit_mod, "_mq_publish_for_source", fake_publish)

    emit_mod.reset_emit_runtime()

    await emit(_XReq(x_id="x2"))

    fake_publish.assert_not_called()
    assert len(captured) == 1
    assert captured[0].x_id == "x2"


@pytest.mark.asyncio
async def test_emit_skips_when_no_mq_source_and_consumer_other_app(monkeypatch):
    """Consumer in another app + wire has NO Source.mq → silently skip (no publish, no error)."""

    @node
    async def x_handler(r: _XReq) -> None:
        pass

    wire(_XReq).to(x_handler)  # no Source.mq
    bind(x_handler).to_app("vectorize-worker")

    monkeypatch.setenv("APP_NAME", "agent-service")
    fake_publish = AsyncMock()
    import sys

    emit_mod = sys.modules["app.runtime.emit"]
    monkeypatch.setattr(emit_mod, "_mq_publish_for_source", fake_publish)

    emit_mod.reset_emit_runtime()

    await emit(_XReq(x_id="x3"))  # should not raise

    fake_publish.assert_not_called()
