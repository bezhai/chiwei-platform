"""De-risk the hand-written ReAct loop (T4b cutover) against a fake ModelClient.

These tests prove the three loops in ``Agent.run / stream / extract`` are
correct *before* any real langchain removal — the loops are the largest net-new
logic of the cutover and must be exercised in isolation:

  - run: complete → (tool_calls? dispatch each, append tool messages, loop) →
    final assistant Message; guarded by recursion_limit; retry wraps the whole.
  - stream: forward neutral StreamChunks; on a tool-call turn, dispatch and feed
    results back, looping for more turns; never replay already-yielded tokens.
  - extract: structured(dict) → response_model.model_validate.

The fake ModelClient is a scripted ``ModelClient`` returning canned neutral
``Message`` / ``StreamChunk`` sequences, so the loop's control flow (not a real
provider) is what's under test. Tools are synthetic neutral ``@tool``s.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.agent.client import ModelClient
from app.agent.context import AgentContext
from app.agent.neutral import (
    ContentBlock,
    Message,
    Role,
    StreamChunk,
    ToolCall,
    ToolDef,
)
from app.agent.runtime_context import get_context
from app.agent.tooling import tool

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake ModelClient — scripted neutral responses
# ---------------------------------------------------------------------------


class FakeModelClient(ModelClient):
    """A ModelClient that replays scripted responses, recording what it saw.

    ``complete_script`` / ``structured_script`` are lists popped per call.
    ``stream_script`` is a list of chunk-lists, one per ``stream`` call. Each
    call records the messages + tools it was handed so the loop's message
    threading (assistant turn + tool results fed back) can be asserted.
    """

    def __init__(
        self,
        *,
        complete_script: list[Message] | None = None,
        stream_script: list[list[StreamChunk]] | None = None,
        structured_script: list[dict] | None = None,
    ) -> None:
        self._complete = list(complete_script or [])
        self._stream = list(stream_script or [])
        self._structured = list(structured_script or [])
        self.complete_calls: list[tuple[list[Message], list[ToolDef] | None]] = []
        self.stream_calls: list[tuple[list[Message], list[ToolDef] | None]] = []
        self.structured_calls: list[tuple[list[Message], dict]] = []
        # per-call kwargs so passthrough (e.g. session_id for the prompt-cache
        # key) can be asserted.
        self.complete_kwargs: list[dict] = []
        self.stream_kwargs: list[dict] = []

    async def complete(self, messages, *, tools=None, **kwargs):
        self.complete_calls.append((list(messages), tools))
        self.complete_kwargs.append(dict(kwargs))
        return self._complete.pop(0)

    async def stream(self, messages, *, tools=None, **kwargs) -> AsyncIterator[StreamChunk]:
        self.stream_calls.append((list(messages), tools))
        self.stream_kwargs.append(dict(kwargs))
        chunks = self._stream.pop(0)
        for c in chunks:
            yield c

    async def structured(self, messages, *, schema, **kwargs) -> dict:
        self.structured_calls.append((list(messages), schema))
        return self._structured.pop(0)


# ---------------------------------------------------------------------------
# Synthetic tools
# ---------------------------------------------------------------------------


@tool
async def echo_tool(text: str) -> str:
    """Echo the text back.

    Args:
        text: in.
    """
    return f"echoed:{text}"


@tool
async def ctx_tool(x: str) -> str:
    """Read the ambient persona id from the agent context.

    Args:
        x: in.
    """
    ctx = get_context()
    return f"persona={ctx.persona_id};x={x}"


@tool
async def dict_tool(x: str) -> dict:
    """A tool that returns a dict (like recall / notes / a tool_error outcome).

    Args:
        x: in.
    """
    return {"ok": True, "value": x}


@tool
async def blocks_tool(x: str) -> list:
    """A tool returning OpenAI-style content blocks (like read_images).

    Args:
        x: in.
    """
    return [
        {"type": "text", "text": "@3.png:"},
        {"type": "image_url", "image_url": {"url": "https://x/3.png"}},
    ]


@tool
async def no_reply() -> str:
    """End the turn without sending any reply."""
    return "ok"


# ---------------------------------------------------------------------------
# Helpers to import the loop functions under test
# ---------------------------------------------------------------------------


def _import_loops():
    from app.agent.core import _run_loop, _stream_loop

    return _run_loop, _stream_loop


# ---------------------------------------------------------------------------
# run loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_no_tool_call_returns_final_message(self):
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi there")]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            context=None,
            recursion_limit=12,
        )
        assert isinstance(result, Message)
        assert result.text() == "hi there"
        assert len(fake.complete_calls) == 1

    async def test_single_tool_call_then_final(self):
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="done"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == "done"
        # second completion saw the assistant tool-call turn + the tool result
        second_msgs = fake.complete_calls[1][0]
        roles = [m.role for m in second_msgs]
        assert Role.ASSISTANT in roles
        assert Role.TOOL in roles
        tool_msg = next(m for m in second_msgs if m.role == Role.TOOL)
        assert tool_msg.tool_call_id == "c1"
        assert tool_msg.text() == "echoed:x"

    async def test_no_reply_tool_ends_without_second_model_call(self):
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="no_reply", arguments={})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="should not run"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[no_reply, echo_tool],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == ""
        assert len(fake.complete_calls) == 1

    async def test_real_no_reply_tool_with_reason_ends_the_turn(self):
        """Wires the real ``app.agent.tools.no_reply`` (required ``reason``
        param) through the real ``_run_loop`` end to end, not just the local
        zero-arg stub above — this is what production actually dispatches."""
        from app.agent.tools.no_reply import no_reply as real_no_reply

        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="no_reply", arguments={"reason": "对方在钓鱼式逼回应"})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="should not run"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[real_no_reply, echo_tool],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == ""
        assert len(fake.complete_calls) == 1

    async def test_real_no_reply_tool_missing_reason_still_ends_without_crash(self):
        """Same real tool, but the model omits ``reason`` — the binding
        pre-check must turn this into a graceful termination, not a raised
        TypeError that would kill the whole turn."""
        from app.agent.tools.no_reply import no_reply as real_no_reply

        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="no_reply", arguments={})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="should not run"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[real_no_reply, echo_tool],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == ""
        assert len(fake.complete_calls) == 1

    async def test_parallel_tool_calls_all_dispatched(self):
        _run_loop, _ = _import_loops()
        calls = [
            ToolCall(id="c1", name="echo_tool", arguments={"text": "a"}),
            ToolCall(id="c2", name="echo_tool", arguments={"text": "b"}),
        ]
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=calls),
                Message(role=Role.ASSISTANT, content="fin"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == "fin"
        second_msgs = fake.complete_calls[1][0]
        tool_msgs = [m for m in second_msgs if m.role == Role.TOOL]
        assert {m.tool_call_id for m in tool_msgs} == {"c1", "c2"}

    async def test_context_is_bound_during_dispatch(self):
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="ctx_tool", arguments={"x": "v"})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="ok"),
            ]
        )
        ctx = AgentContext(message_id="m", chat_id="c", persona_id="luna")
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[ctx_tool],
            context=ctx,
            recursion_limit=12,
        )
        tool_msg = next(
            m for m in fake.complete_calls[1][0] if m.role == Role.TOOL
        )
        assert tool_msg.text() == "persona=luna;x=v"

    async def test_recursion_limit_stops_runaway_tool_loop(self):
        _run_loop, _ = _import_loops()
        # Model always asks for a tool — would loop forever without a guard.
        looping = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c", name="echo_tool", arguments={"text": "x"})],
        )
        fake = FakeModelClient(complete_script=[looping] * 100)
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=3,
        )
        # Stops after the limit; returns the last assistant message it had.
        assert isinstance(result, Message)
        assert len(fake.complete_calls) <= 3

    async def test_tools_passed_as_tooldefs(self):
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[echo_tool],
            context=None,
            recursion_limit=12,
        )
        _msgs, tools = fake.complete_calls[0]
        assert tools is not None
        assert all(isinstance(t, ToolDef) for t in tools)
        assert tools[0].name == "echo_tool"

    async def test_dict_tool_result_serialised_to_json_string(self):
        # recall / notes / tool_error outcomes return dicts. The tool message
        # fed back must be a STRING the adapter can wire (a raw dict would crash
        # Message.text() and the adapter's content serialisation).
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="dict_tool", arguments={"x": "v"})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="done"),
            ]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[dict_tool],
            context=None,
            recursion_limit=12,
        )
        tool_msg = next(
            m for m in fake.complete_calls[1][0] if m.role == Role.TOOL
        )
        assert isinstance(tool_msg.content, str)
        # round-trips back to the original dict
        assert json.loads(tool_msg.content) == {"ok": True, "value": "v"}
        # .text() must not crash
        assert tool_msg.text() == tool_msg.content

    async def test_block_list_tool_result_becomes_content_blocks(self):
        # read_images / generate_image return list[dict] OpenAI content blocks.
        # The tool message must carry neutral ContentBlocks (multimodal), not
        # raw dicts the adapter can't wire.
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="blocks_tool", arguments={"x": "v"})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="done"),
            ]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[blocks_tool],
            context=None,
            recursion_limit=12,
        )
        tool_msg = next(
            m for m in fake.complete_calls[1][0] if m.role == Role.TOOL
        )
        assert isinstance(tool_msg.content, list)
        assert all(isinstance(b, ContentBlock) for b in tool_msg.content)
        assert tool_msg.content[0].type == "text"
        assert tool_msg.content[1].type == "image_url"
        # .text() must not crash and yields the text blocks
        assert tool_msg.text() == "@3.png:"


# ---------------------------------------------------------------------------
# stream loop
# ---------------------------------------------------------------------------


class TestStreamLoop:
    async def test_forwards_text_chunks(self):
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [
                    StreamChunk(text="he"),
                    StreamChunk(text="llo"),
                    StreamChunk(finish_reason="stop"),
                ]
            ]
        )
        out = []
        async for chunk in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="hi")],
            tools=[],
            context=None,
            recursion_limit=12,
        ):
            out.append(chunk)
        texts = [c.text for c in out if c.text]
        assert "".join(texts) == "hello"

    async def test_tool_call_turn_dispatches_and_continues(self):
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                # first turn: a tool call
                [StreamChunk(tool_call=call), StreamChunk(finish_reason="tool_calls")],
                # second turn: final text
                [StreamChunk(text="final"), StreamChunk(finish_reason="stop")],
            ]
        )
        out = []
        async for chunk in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=12,
        ):
            out.append(chunk)
        # the loop looped: second stream call saw the tool result fed back
        assert len(fake.stream_calls) == 2
        second_msgs = fake.stream_calls[1][0]
        tool_msg = next(m for m in second_msgs if m.role == Role.TOOL)
        assert tool_msg.tool_call_id == "c1"
        assert tool_msg.text() == "echoed:x"
        # downstream consumer sees the tool_call chunk, a tool_result chunk, and text
        assert any(c.tool_call is not None for c in out)
        assert any(c.tool_result is not None for c in out)
        assert "".join(c.text or "" for c in out) == "final"

    async def test_no_reply_tool_stream_ends_without_text_or_second_model_call(self):
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="no_reply", arguments={})
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(tool_call=call), StreamChunk(finish_reason="tool_calls")],
                [StreamChunk(text="should not stream"), StreamChunk(finish_reason="stop")],
            ]
        )
        out = []
        async for chunk in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[no_reply, echo_tool],
            context=None,
            recursion_limit=12,
        ):
            out.append(chunk)
        assert len(fake.stream_calls) == 1
        assert any(c.tool_call is not None for c in out)
        assert any(c.tool_result is not None for c in out)
        assert "".join(c.text or "" for c in out) == ""

    async def test_no_tool_calls_does_not_loop(self):
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(text="just text"), StreamChunk(finish_reason="stop")]
            ]
        )
        out = [
            c
            async for c in _stream_loop(
                fake,
                messages=[Message(role=Role.USER, content="hi")],
                tools=[echo_tool],
                context=None,
                recursion_limit=12,
            )
        ]
        assert len(fake.stream_calls) == 1
        assert "".join(c.text or "" for c in out) == "just text"

    async def test_context_bound_during_stream_dispatch(self):
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="ctx_tool", arguments={"x": "v"})
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(tool_call=call), StreamChunk(finish_reason="tool_calls")],
                [StreamChunk(text="ok"), StreamChunk(finish_reason="stop")],
            ]
        )
        ctx = AgentContext(message_id="m", chat_id="c", persona_id="sol")
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[ctx_tool],
            context=ctx,
            recursion_limit=12,
        ):
            pass
        tool_msg = next(m for m in fake.stream_calls[1][0] if m.role == Role.TOOL)
        assert tool_msg.text() == "persona=sol;x=v"

    async def test_recursion_limit_caps_stream_tool_loop(self):
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c", name="echo_tool", arguments={"text": "x"})
        # every turn requests a tool → infinite without the guard
        turn = [StreamChunk(tool_call=call), StreamChunk(finish_reason="tool_calls")]
        fake = FakeModelClient(stream_script=[turn] * 100)
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=3,
        ):
            pass
        assert len(fake.stream_calls) <= 3

    async def test_stream_dict_tool_result_serialised_to_string(self):
        # same dict-result wire safety as run, but on the streaming path.
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="dict_tool", arguments={"x": "v"})
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(tool_call=call), StreamChunk(finish_reason="tool_calls")],
                [StreamChunk(text="ok"), StreamChunk(finish_reason="stop")],
            ]
        )
        out = []
        async for chunk in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[dict_tool],
            context=None,
            recursion_limit=12,
        ):
            out.append(chunk)
        # tool message fed back is a json string, not a raw dict
        tool_msg = next(m for m in fake.stream_calls[1][0] if m.role == Role.TOOL)
        assert isinstance(tool_msg.content, str)
        assert json.loads(tool_msg.content) == {"ok": True, "value": "v"}
        # the emitted tool_result chunk carries the normalised content too
        tr_chunk = next(c for c in out if c.tool_result is not None)
        assert isinstance(tr_chunk.tool_result.content, str)

    async def test_stream_rebuilt_assistant_turn_carries_reasoning(self):
        # On a tool-call turn the streaming loop rebuilds the assistant turn it
        # feeds back into the transcript. That rebuild must carry the streamed
        # reasoning (reasoning chunks accumulated → Message.reasoning_content),
        # mirroring the non-streaming _run_loop where model.complete returns a
        # Message that already carries reasoning_content. Dropping it loses the
        # model's thoughts from the next turn's context.
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                [
                    StreamChunk(reasoning="let me "),
                    StreamChunk(reasoning="think"),
                    StreamChunk(text="calling tool"),
                    StreamChunk(tool_call=call),
                    StreamChunk(finish_reason="tool_calls"),
                ],
                [StreamChunk(text="done"), StreamChunk(finish_reason="stop")],
            ]
        )
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=12,
        ):
            pass
        # the assistant turn fed into the SECOND model call carries reasoning
        second_msgs = fake.stream_calls[1][0]
        assistant_turn = next(
            m for m in second_msgs if m.role == Role.ASSISTANT and m.tool_calls
        )
        assert assistant_turn.reasoning_content == "let me think"
        assert assistant_turn.text() == "calling tool"


# ---------------------------------------------------------------------------
# tool span output — record the dispatched tool's result on its span so
# langfuse shows the output instead of `undefined`
# ---------------------------------------------------------------------------


def _recording_tool_span(spans: list):
    """A ``_tool_span`` replacement that hands back a recording MagicMock span."""

    @contextmanager
    def _span(*, name, input):
        span = MagicMock()
        span.tool_name = name
        spans.append(span)
        yield span

    return _span


class TestToolSpanOutput:
    """The tool span must record the dispatched tool's output. langfuse rendered
    tool outputs as ``undefined`` because the loop opened the span (capturing the
    arguments as ``input``) but never wrote the result back to it."""

    async def test_run_loop_records_string_tool_output(self, monkeypatch):
        from app.agent import core

        spans: list = []
        monkeypatch.setattr(core, "_tool_span", _recording_tool_span(spans))
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="done"),
            ]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=12,
        )
        assert len(spans) == 1
        spans[0].update.assert_called_once_with(output="echoed:x")

    async def test_run_loop_block_list_output_is_json_serialisable(self, monkeypatch):
        from app.agent import core

        spans: list = []
        monkeypatch.setattr(core, "_tool_span", _recording_tool_span(spans))
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="blocks_tool", arguments={"x": "v"})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="done"),
            ]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[blocks_tool],
            context=None,
            recursion_limit=12,
        )
        output = spans[0].update.call_args.kwargs["output"]
        # plain dicts (not ContentBlock objects) so langfuse can serialise it
        assert isinstance(output, list)
        assert all(isinstance(b, dict) for b in output)
        json.dumps(output)  # must not raise
        assert output[0]["type"] == "text"
        assert output[1]["type"] == "image_url"

    async def test_stream_loop_records_tool_output(self, monkeypatch):
        from app.agent import core

        spans: list = []
        monkeypatch.setattr(core, "_tool_span", _recording_tool_span(spans))
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(tool_call=call), StreamChunk(finish_reason="tool_calls")],
                [StreamChunk(text="final"), StreamChunk(finish_reason="stop")],
            ]
        )
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=12,
        ):
            pass
        assert len(spans) == 1
        spans[0].update.assert_called_once_with(output="echoed:x")


# ---------------------------------------------------------------------------
# session_id passthrough — the loop forwards session_id to model.complete /
# model.stream so the adapter can use it as the prompt-cache key. Default (no
# session_id) forwards None, which the adapter no-ops on.
# ---------------------------------------------------------------------------


class TestSessionIdPassthrough:
    async def test_run_loop_forwards_session_id_to_complete(self):
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            context=None,
            recursion_limit=12,
            session_id="coe-world-life2:world:2026-06-06",
        )
        assert (
            fake.complete_kwargs[0]["session_id"]
            == "coe-world-life2:world:2026-06-06"
        )

    async def test_run_loop_default_session_id_is_none(self):
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            context=None,
            recursion_limit=12,
        )
        assert fake.complete_kwargs[0].get("session_id") is None

    async def test_stream_loop_forwards_session_id_to_stream(self):
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(text="hi"), StreamChunk(finish_reason="stop")]
            ]
        )
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="hi")],
            tools=[],
            context=None,
            recursion_limit=12,
            session_id="sess-1",
        ):
            pass
        assert fake.stream_kwargs[0]["session_id"] == "sess-1"

    async def test_run_loop_session_id_survives_model_kwargs_collision(self):
        """A session_id in model_kwargs must not TypeError-clash with the loop's
        explicit session_id; the loop's trace session_id wins, others survive."""
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            context=None,
            recursion_limit=12,
            session_id="real",
            model_kwargs={"session_id": "stale", "reasoning_effort": "low"},
        )
        assert fake.complete_kwargs[0]["session_id"] == "real"
        assert fake.complete_kwargs[0]["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# native_web_search passthrough — the loop forwards the signal to the model
# ONLY when it is True, so every existing (non-native) call is byte-for-byte
# unchanged: a model never sees an unknown ``native_web_search`` kwarg unless
# the agent layer decided to enable native search this run.
# ---------------------------------------------------------------------------


class TestNativeWebSearchPassthrough:
    async def test_run_loop_forwards_native_web_search_when_true(self):
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            context=None,
            recursion_limit=12,
            native_web_search=True,
        )
        assert fake.complete_kwargs[0]["native_web_search"] is True

    async def test_run_loop_omits_native_web_search_by_default(self):
        # The default (False) must NOT appear in the kwargs at all, so existing
        # adapters that don't know the kwarg are never handed it.
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            context=None,
            recursion_limit=12,
        )
        assert "native_web_search" not in fake.complete_kwargs[0]

    async def test_run_loop_omits_native_web_search_when_false(self):
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            context=None,
            recursion_limit=12,
            native_web_search=False,
        )
        assert "native_web_search" not in fake.complete_kwargs[0]

    async def test_stream_loop_forwards_native_web_search_when_true(self):
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(text="hi"), StreamChunk(finish_reason="stop")]
            ]
        )
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="hi")],
            tools=[],
            context=None,
            recursion_limit=12,
            native_web_search=True,
        ):
            pass
        assert fake.stream_kwargs[0]["native_web_search"] is True

    async def test_stream_loop_omits_native_web_search_by_default(self):
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(text="hi"), StreamChunk(finish_reason="stop")]
            ]
        )
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="hi")],
            tools=[],
            context=None,
            recursion_limit=12,
        ):
            pass
        assert "native_web_search" not in fake.stream_kwargs[0]


# ---------------------------------------------------------------------------
# empty-turn retry — a turn that comes back with no text AND no tool_calls
# (e.g. the collapsed ``{"text": "", "tool_calls": []}`` observed after a
# generate_image tool call in trace 82323210372fe067ec2a60abd8e9fdb3) is
# transparently retried in place — same ``convo``, no new messages appended,
# no tools re-dispatched — up to a bounded number of attempts for that ONE
# turn. Exhausting retries never raises a new exception or changes the return
# shape: the loop falls back to exactly the pre-retry behaviour (return /
# end the generator with whatever the last attempt produced), just logging the
# exhaustion so it is observable. A normal non-empty result must never trigger
# a second request.
# ---------------------------------------------------------------------------


class TestRunLoopEmptyTurnRetry:
    async def test_empty_completion_is_retried_then_succeeds(self):
        _run_loop, _ = _import_loops()
        empty = Message(role=Role.ASSISTANT, content="")
        fake = FakeModelClient(
            complete_script=[empty, Message(role=Role.ASSISTANT, content="real reply")]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == "real reply"
        assert len(fake.complete_calls) == 2

    async def test_reasoning_only_completion_counts_as_empty_and_is_retried(self):
        # text blank, no tool_calls, but reasoning_content set — reasoning is
        # never surfaced to the user, so a "thought but didn't answer" turn
        # must still count as empty and get retried.
        _run_loop, _ = _import_loops()
        reasoning_only = Message(
            role=Role.ASSISTANT, content="", reasoning_content="thinking..."
        )
        fake = FakeModelClient(
            complete_script=[
                reasoning_only,
                Message(role=Role.ASSISTANT, content="ok"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == "ok"
        assert len(fake.complete_calls) == 2

    async def test_whitespace_only_text_counts_as_empty_and_is_retried(self):
        _run_loop, _ = _import_loops()
        whitespace_only = Message(role=Role.ASSISTANT, content="   \n\t  ")
        fake = FakeModelClient(
            complete_script=[
                whitespace_only,
                Message(role=Role.ASSISTANT, content="ok"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == "ok"
        assert len(fake.complete_calls) == 2

    async def test_empty_completion_exhausts_retries_and_returns_empty_message(
        self, caplog
    ):
        # every attempt comes back empty — the loop must give up after a
        # bounded number of tries, return the SAME shape it always has (a
        # Message, never an exception), and log the exhaustion so the
        # occurrence rate can be observed in prod.
        import logging

        _run_loop, _ = _import_loops()
        empty = Message(role=Role.ASSISTANT, content="")
        fake = FakeModelClient(complete_script=[empty, empty, empty])
        with caplog.at_level(logging.WARNING):
            result = await _run_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[],
                context=None,
                recursion_limit=12,
            )
        assert isinstance(result, Message)
        assert result.text() == ""
        assert not result.tool_calls
        assert len(fake.complete_calls) == 3  # 3 tries total, then give up
        assert any("empty" in r.message.lower() for r in caplog.records)

    async def test_tool_call_turn_with_blank_text_is_not_retried(self):
        # tool_calls present -> not "empty" even though text is blank; a real
        # tool-call turn must never trigger the empty-retry path.
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="done"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == "done"
        # exactly 2 calls: the tool-call turn + the final turn, no extra retry
        assert len(fake.complete_calls) == 2

    async def test_normal_non_empty_result_is_not_retried(self):
        # the common case: a single model call, zero added request cost.
        _run_loop, _ = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi there")]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            context=None,
            recursion_limit=12,
        )
        assert result.text() == "hi there"
        assert len(fake.complete_calls) == 1


class TestStreamLoopEmptyTurnRetry:
    async def test_empty_turn_is_retried_then_succeeds(self):
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(finish_reason="stop")],  # empty: no text, no tool_calls
                [StreamChunk(text="real reply"), StreamChunk(finish_reason="stop")],
            ]
        )
        out = [
            c
            async for c in _stream_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[],
                context=None,
                recursion_limit=12,
            )
        ]
        assert "".join(c.text or "" for c in out) == "real reply"
        assert len(fake.stream_calls) == 2

    async def test_reasoning_only_turn_counts_as_empty_and_is_retried(self):
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [
                    StreamChunk(reasoning="thinking..."),
                    StreamChunk(finish_reason="stop"),
                ],
                [StreamChunk(text="ok"), StreamChunk(finish_reason="stop")],
            ]
        )
        out = [
            c
            async for c in _stream_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[],
                context=None,
                recursion_limit=12,
            )
        ]
        assert "".join(c.text or "" for c in out) == "ok"
        assert len(fake.stream_calls) == 2

    async def test_empty_turn_exhausts_retries_and_ends_generator(self, caplog):
        import logging

        _, _stream_loop = _import_loops()
        empty_turn = [StreamChunk(finish_reason="stop")]
        fake = FakeModelClient(stream_script=[empty_turn, empty_turn, empty_turn])
        with caplog.at_level(logging.WARNING):
            out = [
                c
                async for c in _stream_loop(
                    fake,
                    messages=[Message(role=Role.USER, content="go")],
                    tools=[],
                    context=None,
                    recursion_limit=12,
                )
            ]
        assert "".join(c.text or "" for c in out) == ""
        assert len(fake.stream_calls) == 3  # 3 tries total, then give up
        assert any("empty" in r.message.lower() for r in caplog.records)

    async def test_tool_call_turn_with_blank_text_is_not_retried(self):
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(tool_call=call), StreamChunk(finish_reason="tool_calls")],
                [StreamChunk(text="done"), StreamChunk(finish_reason="stop")],
            ]
        )
        out = [
            c
            async for c in _stream_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[echo_tool],
                context=None,
                recursion_limit=12,
            )
        ]
        assert "".join(c.text or "" for c in out) == "done"
        assert len(fake.stream_calls) == 2

    async def test_normal_non_empty_turn_is_not_retried(self):
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(text="hello"), StreamChunk(finish_reason="stop")]
            ]
        )
        out = [
            c
            async for c in _stream_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[],
                context=None,
                recursion_limit=12,
            )
        ]
        assert "".join(c.text or "" for c in out) == "hello"
        assert len(fake.stream_calls) == 1

    async def test_content_filter_only_turn_is_empty_per_is_empty_turn(self):
        """``_is_empty_turn`` only looks at text/tool_calls — it has no
        opinion on ``finish_reason``. A turn whose only chunk is
        ``finish_reason="content_filter"`` (no text, no tool_calls) therefore
        DOES match the retry condition if something keeps draining
        ``_stream_loop`` to exhaustion, as this test does directly. This is
        not a correctness bug for the real chat path: ``render_chat_turn``
        (``app/chat/render.py``) reacts to a content_filter chunk the instant
        it sees one — it yields the persona content_filter message and
        ``return``s, which abandons (never resumes) this same generator
        *before* it would reach the retry-decision point below the inner
        ``async for``. See the next test for that production-safety property.
        This test exists so that fact is asserted, not just reasoned about in
        a docstring — if ``_is_empty_turn`` ever grows a
        content_filter/length exclusion, this test's expected call count
        must change too."""
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(finish_reason="content_filter")],
                [StreamChunk(text="ignored if reached"), StreamChunk(finish_reason="stop")],
            ]
        )
        out = [
            c
            async for c in _stream_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[],
                context=None,
                recursion_limit=12,
            )
        ]
        assert len(fake.stream_calls) == 2, (
            "_stream_loop in isolation has no content_filter awareness, so a "
            "fully-drained consumer does retry this turn once — this is the "
            "documented, acceptable behavior, not the production path"
        )
        assert out[0].finish_reason == "content_filter"

    async def test_early_abandonment_on_content_filter_prevents_retry(self):
        """The actual safety net for the case above: render_chat_turn's real
        consumption pattern is "see a content_filter/length chunk, stop
        pulling more chunks immediately" (app/chat/render.py's
        ``is_content_filter``/``is_length_truncated`` checks, which return
        without ever calling ``.__anext__()`` again). This test reproduces
        that exact consumption pattern directly against ``_stream_loop``
        (without going through the full render_chat_turn + Agent + model
        registry machinery) and proves ``model.stream()`` is called exactly
        once — the retry-decision code below the inner ``async for`` in
        ``_stream_loop`` is never reached because nothing ever asks this
        generator for its next chunk after the content_filter one."""
        _, _stream_loop = _import_loops()
        fake = FakeModelClient(
            stream_script=[
                [StreamChunk(finish_reason="content_filter")],
                [StreamChunk(text="should never be requested"), StreamChunk(finish_reason="stop")],
            ]
        )
        gen = _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            context=None,
            recursion_limit=12,
        )
        first = await gen.__anext__()
        assert first.finish_reason == "content_filter"
        # render_chat_turn stops here — it never calls __anext__() again.
        await gen.aclose()
        assert len(fake.stream_calls) == 1
