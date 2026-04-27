"""End-to-end emit -> insert_append -> select_latest closure.

Verifies the as_latest / with_latest contract:

  - ``wire(X).as_latest()`` makes ``emit(X)`` append a versioned row,
    so the row is observable to ``query(X)`` callers and to
    ``with_latest(X)`` resolution downstream;
  - ``wire(Y).to(consumer).with_latest(X)`` resolves the consumer's
    ``X`` parameter from the latest persisted X row (joined by the
    same Key field name);
  - missing X raises rather than handing the consumer a ``None``;
  - a wire WITHOUT ``.as_latest()`` does not persist (the previous
    half-implementation contract that emit silently no-op'd persistence
    is gone — non-as_latest wires stay strictly in-memory).
"""

from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import Data, Key, Version
from app.runtime.emit import emit
from app.runtime.graph import compile_graph
from app.runtime.node import node
from app.runtime.persist import select_latest
from app.runtime.wire import wire
from tests.runtime.conftest import migrate


class Profile(Data):
    user_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    mood: str


class Trigger(Data):
    user_id: Annotated[str, Key]
    reason: str


class Plain(Data):
    pid: Annotated[str, Key]
    text: str


@pytest.mark.integration
async def test_emit_with_as_latest_persists_one_versioned_row(test_db):
    """emit(X) under wire(X).as_latest() must append a versioned row."""
    await migrate(Profile, test_db)

    @node
    async def sink(p: Profile) -> None:  # noqa: ARG001
        return None

    wire(Profile).to(sink).as_latest()
    compile_graph()

    await emit(Profile(user_id="u1", mood="curious"))
    await emit(Profile(user_id="u1", mood="restless"))

    latest = await select_latest(Profile, {"user_id": "u1"})
    assert latest is not None
    assert latest.mood == "restless"


@pytest.mark.integration
async def test_emit_without_as_latest_does_not_persist(test_db):
    """A plain wire (no .as_latest) must not append. Persistence is
    opt-in via the wire flag, not a side effect of emit().
    """
    await migrate(Plain, test_db)

    @node
    async def sink(p: Plain) -> None:  # noqa: ARG001
        return None

    wire(Plain).to(sink)
    compile_graph()

    await emit(Plain(pid="p1", text="hi"))

    assert await select_latest(Plain, {"pid": "p1"}) is None


@pytest.mark.integration
async def test_with_latest_resolves_to_persisted_row(test_db):
    """A consumer with ``with_latest(X)`` sees the latest persisted X."""
    await migrate(Profile, test_db)

    received: list[tuple[Trigger, Profile]] = []

    @node
    async def handle(t: Trigger, p: Profile) -> None:
        received.append((t, p))

    @node
    async def profile_sink(p: Profile) -> None:  # noqa: ARG001
        return None

    wire(Profile).to(profile_sink).as_latest()
    wire(Trigger).to(handle).with_latest(Profile)
    compile_graph()

    await emit(Profile(user_id="u1", mood="happy"))
    await emit(Profile(user_id="u1", mood="furious"))
    await emit(Trigger(user_id="u1", reason="ping"))

    assert len(received) == 1
    t, p = received[0]
    assert t.reason == "ping"
    assert p.mood == "furious"  # latest version


@pytest.mark.integration
async def test_with_latest_raises_when_no_persisted_row(test_db):
    """Consumer with ``with_latest(X)`` cannot run without an X row.
    Previously select_latest returned None and the consumer received
    a None — now we raise so the wiring bug is loud.
    """
    await migrate(Profile, test_db)

    @node
    async def handle(t: Trigger, p: Profile) -> None:  # noqa: ARG001
        return None

    @node
    async def profile_sink(p: Profile) -> None:  # noqa: ARG001
        return None

    wire(Profile).to(profile_sink).as_latest()
    wire(Trigger).to(handle).with_latest(Profile)
    compile_graph()

    # Note: no emit(Profile(...)) before the Trigger — pg is empty.
    with pytest.raises(RuntimeError, match="found no row"):
        await emit(Trigger(user_id="u1", reason="ping"))
