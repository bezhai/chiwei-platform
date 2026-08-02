"""Phase 6 v4 Gap 2: emit cross-process via wire source.mq."""
from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, patch

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


class _ChatReqProbe(Data):
    """Uses a queue that really exists in ALL_ROUTES so the publish path
    (``_mq_publish_for_source`` -> ``_route_by_queue``) resolves a Route."""

    chat_id: Annotated[str, Key]

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
async def test_emit_raises_when_no_mq_source_and_consumer_other_app(monkeypatch):
    """A0 W4a：Consumer in another app + 无 Source.mq + 无 durable → emit 必须
    raise RuntimeError，不允许 silent skip（contract "禁止静默兜底"）。"""

    @node
    async def x_handler(r: _XReq) -> None:
        pass

    wire(_XReq).to(x_handler)  # no Source.mq, no .durable()
    bind(x_handler).to_app("vectorize-worker")

    monkeypatch.setenv("APP_NAME", "agent-service")
    fake_publish = AsyncMock()
    import sys

    emit_mod = sys.modules["app.runtime.emit"]
    monkeypatch.setattr(emit_mod, "_mq_publish_for_source", fake_publish)

    emit_mod.reset_emit_runtime()

    with pytest.raises(RuntimeError, match="cross-app dispatch has no transport"):
        await emit(_XReq(x_id="x3"))

    fake_publish.assert_not_called()


@pytest.mark.asyncio
async def test_emit_mq_publish_injects_trace_and_lane_headers(monkeypatch):
    """Cross-process emit over Source.mq must write trace/lane into the
    message header.

    The consumer end (``Runtime._source_loop_mq``) rebuilds contextvars from
    headers *only* — there is deliberately no body / env fallback, because a
    prod pod also drains lane messages that TTL'd back from a lane queue.
    So a publish without headers means the consuming process runs the whole
    downstream chain with ``lane=None`` and ``lane_router`` sends every
    outbound HTTP call to the prod service.
    """
    from app.runtime.propagation import Context, bind_context

    @node
    async def chat_handler(r: _ChatReqProbe) -> None:
        pass

    wire(_ChatReqProbe).to(chat_handler).from_(Source.mq("chat_request"))
    bind(chat_handler).to_app("vectorize-worker")

    monkeypatch.setenv("APP_NAME", "agent-service")

    import sys

    emit_mod = sys.modules["app.runtime.emit"]
    emit_mod.reset_emit_runtime()

    fake_publish = AsyncMock()
    with patch("app.infra.rabbitmq.mq.publish", fake_publish):
        async with bind_context(Context(trace_id="t-emit", lane="ppe-emit")):
            await emit(_ChatReqProbe(chat_id="c9"))

    fake_publish.assert_awaited_once()
    args, kwargs = fake_publish.await_args
    assert args[0].queue == "chat_request"
    assert args[1]["chat_id"] == "c9"
    headers = kwargs.get("headers")
    assert headers is not None, (
        "emit() published to Source.mq without headers — trace/lane are lost "
        "at the process boundary"
    )
    assert headers["lane"] == "ppe-emit"
    assert headers["trace_id"] == "t-emit"


@pytest.mark.asyncio
async def test_emit_mq_publish_writes_empty_lane_header_outside_a_lane(monkeypatch):
    """No lane in context -> header still present, written as "" (the
    on-wire shape ``inject_context`` guarantees and ``extract_context``
    coerces back to None)."""

    @node
    async def chat_handler(r: _ChatReqProbe) -> None:
        pass

    wire(_ChatReqProbe).to(chat_handler).from_(Source.mq("chat_request"))
    bind(chat_handler).to_app("vectorize-worker")

    monkeypatch.setenv("APP_NAME", "agent-service")

    import sys

    emit_mod = sys.modules["app.runtime.emit"]
    emit_mod.reset_emit_runtime()

    fake_publish = AsyncMock()
    with patch("app.infra.rabbitmq.mq.publish", fake_publish):
        await emit(_ChatReqProbe(chat_id="c10"))

    fake_publish.assert_awaited_once()
    _args, kwargs = fake_publish.await_args
    headers = kwargs.get("headers")
    assert headers is not None
    assert headers["lane"] == ""
    assert headers["data_type"] == "_ChatReqProbe"
