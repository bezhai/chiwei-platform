"""DebouncedPipeline base class tests.

Validates buffer / timer / phase-2 state machine with a minimal concrete
implementation that records ``process()`` calls.
"""

import asyncio

import pytest

from app.memory.debounce import DebouncedPipeline


class _Recorder(DebouncedPipeline):
    """Concrete pipeline that records process() invocations."""

    def __init__(self, debounce_seconds: float, max_buffer: int) -> None:
        super().__init__(debounce_seconds=debounce_seconds, max_buffer=max_buffer)
        self.process_calls: list[tuple[str, str, int]] = []
        self._process_started = asyncio.Event()
        self._process_gate = asyncio.Event()
        self._process_gate.set()  # unblocked by default

    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        self._process_started.set()
        await self._process_gate.wait()
        self.process_calls.append((chat_id, persona_id, event_count))


# ---------------------------------------------------------------------------
# Debounce fires after timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_triggers_after_timeout():
    pipe = _Recorder(debounce_seconds=0.05, max_buffer=100)

    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_1", "akao")

    await asyncio.sleep(0.15)

    assert len(pipe.process_calls) == 1
    chat_id, persona_id, count = pipe.process_calls[0]
    assert chat_id == "chat_1"
    assert persona_id == "akao"
    assert count == 2


# ---------------------------------------------------------------------------
# max_buffer forces immediate flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_buffer_forces_flush():
    pipe = _Recorder(debounce_seconds=10, max_buffer=3)

    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_1", "akao")

    await asyncio.sleep(0.05)

    assert len(pipe.process_calls) == 1
    _, _, count = pipe.process_calls[0]
    assert count == 3


# ---------------------------------------------------------------------------
# Events during phase 2 are buffered, then trigger next cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_during_phase2_buffered():
    pipe = _Recorder(debounce_seconds=0.05, max_buffer=100)
    pipe._process_gate.clear()  # block process()

    await pipe.on_event("chat_1", "akao")

    await asyncio.sleep(0.1)
    assert pipe._process_started.is_set()

    # Send events while phase 2 is running
    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_1", "akao")

    key = "chat_1:akao"
    assert pipe._buffers.get(key, 0) == 2

    # Release phase 2
    pipe._process_started.clear()
    pipe._process_gate.set()

    await asyncio.sleep(0.3)

    assert len(pipe.process_calls) == 2
    assert pipe.process_calls[0][2] == 1
    assert pipe.process_calls[1][2] == 2  # exact buffered count, no phantom +1


# ---------------------------------------------------------------------------
# Different keys are independent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_separate_keys_independent():
    pipe = _Recorder(debounce_seconds=0.05, max_buffer=100)

    await pipe.on_event("chat_1", "akao")
    await pipe.on_event("chat_2", "bkao")

    await asyncio.sleep(0.15)

    assert len(pipe.process_calls) == 2
    keys = {(c, p) for c, p, _ in pipe.process_calls}
    assert ("chat_1", "akao") in keys
    assert ("chat_2", "bkao") in keys
    for _, _, count in pipe.process_calls:
        assert count == 1


# ---------------------------------------------------------------------------
# Debounce resets on new event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_resets_on_new_event():
    pipe = _Recorder(debounce_seconds=0.1, max_buffer=100)

    await pipe.on_event("chat_1", "akao")
    await asyncio.sleep(0.03)
    await pipe.on_event("chat_1", "akao")
    await asyncio.sleep(0.03)
    await pipe.on_event("chat_1", "akao")

    await asyncio.sleep(0.05)
    assert len(pipe.process_calls) == 0

    await asyncio.sleep(0.1)
    assert len(pipe.process_calls) == 1
    assert pipe.process_calls[0][2] == 3


# ---------------------------------------------------------------------------
# Process error cleans up state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_error_cleans_up():

    class _Failing(DebouncedPipeline):
        async def process(self, chat_id, persona_id, event_count):
            raise RuntimeError("boom")

    pipe = _Failing(debounce_seconds=0.05, max_buffer=100)
    await pipe.on_event("chat_1", "akao")

    await asyncio.sleep(0.15)

    key = "chat_1:akao"
    assert key not in pipe._phase2_running
