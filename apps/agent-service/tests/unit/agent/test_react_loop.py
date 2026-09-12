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
    TurnPart,
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
    """A tool returning OpenAI-style content blocks (the shape a picture tool uses).

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

    async def test_a_terminal_tool_leaves_no_empty_message_in_the_sink(self):
        """A terminal tool ends the run; the empty assistant message it returns
        stays out of ``transcript_sink``.

        The sink is what a caller stores as the agent's continuous context. An
        assistant message with no text and no tool calls carries nothing, and a
        caller that ends most of its rounds on a terminal tool would accumulate
        one of them per round forever.
        """
        _run_loop, _ = _import_loops()
        call = ToolCall(id="c1", name="no_reply", arguments={})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call])
            ]
        )
        sink: list[Message] = []
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[no_reply, echo_tool],
            context=None,
            recursion_limit=12,
            transcript_sink=sink,
        )

        assert result.text() == ""
        assert [m.role for m in sink] == [Role.ASSISTANT, Role.TOOL]
        assert sink[-1].tool_call_id == "c1"

    async def test_a_terminal_tool_answers_the_calls_that_never_ran(self):
        """One turn asking for ``[terminal, other]``: the loop returns at the
        terminal tool, so ``other`` is never dispatched.

        Its call still sits on the assistant turn already in the sink. A stored
        assistant turn whose calls are not all answered makes the provider
        reject the whole next request, so the loop has to answer the calls it
        cut off.
        """
        _run_loop, _ = _import_loops()
        calls = [
            ToolCall(id="c1", name="no_reply", arguments={}),
            ToolCall(id="c2", name="echo_tool", arguments={"text": "x"}),
        ]
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=calls)
            ]
        )
        sink: list[Message] = []
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[no_reply, echo_tool],
            context=None,
            recursion_limit=12,
            transcript_sink=sink,
        )

        answered = [m.tool_call_id for m in sink if m.role is Role.TOOL]
        assert answered == ["c1", "c2"], "终止之后那个调用没有结果"
        requested = [c.id for m in sink for c in m.tool_calls]
        assert requested == answered
        cut_off = next(m for m in sink if m.tool_call_id == "c2")
        assert "echoed" not in cut_off.text(), "被切掉的那只手不该真的跑过"

    async def test_recursion_limit_closes_the_run_with_a_toolless_call(self, caplog):
        """Hitting the limit hands the dispatched tool results back to the model
        one last time, with no tools, so the run ends on the assistant's words.

        Returning the tool-call turn instead (what the loop used to do) made the
        run's result an assistant message whose ``text()`` is usually empty —
        a caller storing that as "what it said" stored an empty string, with no
        error and no log, and the tool results of the last turn never reached
        the model at all.
        """
        import logging

        _run_loop, _ = _import_loops()
        looping = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c", name="echo_tool", arguments={"text": "x"})],
        )
        fake = FakeModelClient(
            complete_script=[looping] * 3
            + [Message(role=Role.ASSISTANT, content="先到这儿")]
        )
        sink: list[Message] = []
        with caplog.at_level(logging.WARNING, logger="app.agent.core"):
            result = await _run_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[echo_tool],
                context=None,
                recursion_limit=3,
                transcript_sink=sink,
            )

        assert result.text() == "先到这儿"
        assert len(fake.complete_calls) == 4
        closing_msgs, closing_tools = fake.complete_calls[3]
        assert closing_tools is None, "收口那一次还带着工具 —— 她能接着调下去"
        assert closing_msgs[-1].role is Role.TOOL, (
            "上限那一轮的工具返回没有喂回模型"
        )
        assert sink[-1] is result
        assert any("budget" in r.message for r in caplog.records), caplog.text

    async def test_the_closing_call_never_stores_an_unanswered_tool_call(self):
        """The toolless closing call can still come back asking for a tool (a
        model that ignores an empty tool list). Storing that call would leave the
        context with a call nothing ever answered, and the provider rejects the
        whole next request over it.
        """
        _run_loop, _ = _import_loops()
        looping = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c", name="echo_tool", arguments={"text": "x"})],
        )
        still_calling = Message(
            role=Role.ASSISTANT,
            content="还想再查一下",
            tool_calls=[ToolCall(id="z", name="echo_tool", arguments={"text": "y"})],
        )
        fake = FakeModelClient(complete_script=[looping] * 2 + [still_calling])
        sink: list[Message] = []
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=2,
            transcript_sink=sink,
        )

        assert result.text() == "还想再查一下"
        assert result.tool_calls == []
        answered = {m.tool_call_id for m in sink if m.role is Role.TOOL}
        assert [
            c.id for m in sink for c in m.tool_calls if c.id not in answered
        ] == []

    async def test_a_closing_call_left_with_nothing_stays_out_of_the_sink(self):
        """The toolless closing call can come back with no text and only a tool
        call. Stripping the call leaves an assistant turn carrying nothing —
        the gemini adapter renders it as ``parts=[]`` and the provider rejects
        the whole next request over it.
        """
        _run_loop, _ = _import_loops()
        looping = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c", name="echo_tool", arguments={"text": "x"})],
        )
        nothing_but_a_call = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="z", name="echo_tool", arguments={"text": "y"})],
        )
        fake = FakeModelClient(complete_script=[looping] * 2 + [nothing_but_a_call])
        sink: list[Message] = []
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=2,
            transcript_sink=sink,
        )

        assert result.text() == ""
        assert result.tool_calls == []
        assert all(
            m.text().strip() or m.tool_calls or m.role is Role.TOOL for m in sink
        ), "一条既没正文也没调用的消息进了上下文"

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
        # A tool that hands back pictures returns list[dict] OpenAI content blocks.
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

    async def test_stream_rebuilt_assistant_turn_keeps_the_streamed_sequence(self):
        # On a tool-call turn the streaming loop rebuilds the assistant turn it
        # feeds back into the transcript. The rebuild is hand-copied field by
        # field, so it must carry the sequence that was streamed — thoughts,
        # text and calls in order, each with the signature that rode on it —
        # mirroring the non-streaming _run_loop where model.complete returns a
        # Message that already holds it. Dropping it hands the next request a
        # turn the model never produced.
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                [
                    StreamChunk(reasoning="let me "),
                    StreamChunk(reasoning="think", signature=b"sig-t1"),
                    StreamChunk(text="calling tool", signature=b"sig-x"),
                    StreamChunk(tool_call=call),
                    StreamChunk(reasoning="one more", signature=b"sig-t2"),
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
        # the assistant turn fed into the SECOND model call carries the sequence
        second_msgs = fake.stream_calls[1][0]
        assistant_turn = next(
            m for m in second_msgs if m.role == Role.ASSISTANT and m.tool_calls
        )
        assert assistant_turn.thought_text() == "let me thinkone more"
        assert assistant_turn.text() == "calling tool"
        assert [str(p.kind) for p in assistant_turn.turn_parts] == [
            "thought",
            "text",
            "tool_call",
            "thought",
        ]
        assert [p.signature for p in assistant_turn.turn_parts] == [
            b"sig-t1",
            b"sig-x",
            None,
            b"sig-t2",
        ]
        assert assistant_turn.turn_parts[2].call_id == "c1"

    async def test_stream_keeps_a_signed_segment_apart_from_the_next_one(self):
        """一段签名一段：签名落下之后，后面的字属于下一段，不能并进它。"""
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                [
                    StreamChunk(text="第一段", signature=b"sig-1"),
                    StreamChunk(text="第二"),
                    StreamChunk(text="段"),
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
        assistant_turn = next(
            m
            for m in fake.stream_calls[1][0]
            if m.role == Role.ASSISTANT and m.tool_calls
        )
        text_parts = [p for p in assistant_turn.turn_parts if str(p.kind) == "text"]
        assert [p.text for p in text_parts] == ["第一段", "第二段"]
        assert [p.signature for p in text_parts] == [b"sig-1", None]
        assert assistant_turn.text() == "第一段第二段"


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
        # text blank, no tool_calls, but a thought part on the turn — thinking
        # is never surfaced to the user, so a "thought but didn't answer" turn
        # must still count as empty and get retried.
        _run_loop, _ = _import_loops()
        reasoning_only = Message.from_model_turn(
            [TurnPart.from_thought("thinking...")], []
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


# ---------------------------------------------------------------------------
# a model turn's sequence has to live through the loop's own rebuilds
#
# Two places hand-copy an assistant turn field by field (the closing call with
# its tool calls stripped, and the streamed turn rebuilt from chunks) and one
# decides whether a turn carries anything at all. All three used to be written
# on the premise that nothing replays a model's thoughts — now they do replay,
# and a turn that only thought is not empty on the wire any more.
# ---------------------------------------------------------------------------


class TestTheTurnSequenceSurvivesTheLoop:
    async def test_the_closing_turn_keeps_its_thoughts_and_drops_the_stripped_call(
        self,
    ):
        _run_loop, _ = _import_loops()
        looping = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c", name="echo_tool", arguments={"text": "x"})],
        )
        still_calling_call = ToolCall(
            id="z", name="echo_tool", arguments={"text": "y"}, signature=b"sig-z"
        )
        still_calling = Message.from_model_turn(
            [
                TurnPart.from_thought("再查一下", signature=b"sig-t"),
                TurnPart.from_text("还想再查一下"),
                TurnPart.from_tool_call(still_calling_call),
            ],
            [still_calling_call],
        )
        fake = FakeModelClient(complete_script=[looping] * 2 + [still_calling])
        sink: list[Message] = []
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=2,
            transcript_sink=sink,
        )

        assert result.tool_calls == []
        assert result.thought_text() == "再查一下"
        assert [str(p.kind) for p in result.turn_parts] == ["thought", "text"]
        assert result.turn_parts[0].signature == b"sig-t"
        assert sink[-1] is result

    async def test_a_turn_that_only_thought_is_stored(self):
        """只有思考的那一轮在 wire 上不再是空的 —— 它带着签名，得留在上下文里。"""
        _run_loop, _ = _import_loops()
        looping = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c", name="echo_tool", arguments={"text": "x"})],
        )
        only_thought = Message.from_model_turn(
            [TurnPart.from_thought("想了想", signature=b"sig-t")], []
        )
        fake = FakeModelClient(
            complete_script=[looping] * 2 + [only_thought, only_thought, only_thought]
        )
        sink: list[Message] = []
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=2,
            transcript_sink=sink,
        )

        stored = sink[-1]
        assert stored.thought_text() == "想了想"
        assert stored.turn_parts[0].signature == b"sig-t"

    async def test_a_part_boundary_the_adapter_saw_is_not_folded_away(self):
        """adapter 说这块开的是新的一段，就不能并进上一段里去。

        段边界只有 adapter 知道（它遍历的是 wire 上的 parts）。上层拿到的是一串
        chunk，靠"签名封段"去猜的结果是：同一块里回来的两段被并成一段，签名跟着
        搬到并起来的那一段上。
        """
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                [
                    StreamChunk(reasoning="先想一下"),
                    StreamChunk(
                        reasoning="再想一下", signature=b"sig-B", starts_part=True
                    ),
                    StreamChunk(tool_call=call),
                    StreamChunk(finish_reason="tool_calls"),
                ],
                [StreamChunk(text="好了"), StreamChunk(finish_reason="stop")],
            ]
        )
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=4,
        ):
            pass

        replayed = fake.stream_calls[1][0][1]
        assert [(str(p.kind), p.text, p.signature) for p in replayed.turn_parts] == [
            ("thought", "先想一下", None),
            ("thought", "再想一下", b"sig-B"),
            ("tool_call", "", None),
        ]

    async def test_a_part_cut_across_chunks_is_still_one_part(self):
        """边界不知道的时候（一段被切成多块）仍然并成一段，签名在收尾那块上。"""
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                [
                    StreamChunk(reasoning="先想"),
                    StreamChunk(reasoning="一下", signature=b"sig-A"),
                    StreamChunk(tool_call=call),
                    StreamChunk(finish_reason="tool_calls"),
                ],
                [StreamChunk(text="好了"), StreamChunk(finish_reason="stop")],
            ]
        )
        async for _ in _stream_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=4,
        ):
            pass

        replayed = fake.stream_calls[1][0][1]
        assert [(str(p.kind), p.text, p.signature) for p in replayed.turn_parts] == [
            ("thought", "先想一下", b"sig-A"),
            ("tool_call", "", None),
        ]


# ---------------------------------------------------------------------------
# 被重试掉的那一轮想过的东西
#
# "这一轮有没有形成最终回答"和"这一轮要不要留下来回放"是两个问题。前者决定重不重
# 试（思考不算回答，只想不答的一轮不能当成回复发出去），后者决定它进不进上下文
# ——它带着签名，模型正是从那里接着推理的。压成一个判断的结果是：只想不答的那一轮
# 被重试掉之后，思考和签名一起消失。
# ---------------------------------------------------------------------------


class TestARetriedTurnKeepsWhatItThought:
    async def test_the_thoughts_of_a_retried_attempt_reach_the_retry(self):
        _run_loop, _ = _import_loops()
        thought_only = Message.from_model_turn(
            [TurnPart.from_thought("想了想", signature=b"sig-a")], []
        )
        fake = FakeModelClient(
            complete_script=[thought_only, Message(role=Role.ASSISTANT, content="好")]
        )
        sink: list[Message] = []
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            context=None,
            recursion_limit=12,
            transcript_sink=sink,
        )

        assert result.text() == "好"
        assert len(fake.complete_calls) == 2
        replayed = fake.complete_calls[1][0][-1]
        assert replayed.thought_text() == "想了想"
        assert replayed.turn_parts[0].signature == b"sig-a"
        # 存下来的转录里也在——下一轮请求同样要把它发回去
        assert [m.thought_text() for m in sink] == ["想了想", ""]
        assert sink[0].turn_parts[0].signature == b"sig-a"
        assert sink[-1] is result

    async def test_every_attempt_that_only_thought_is_kept_and_none_is_the_reply(
        self,
    ):
        _run_loop, _ = _import_loops()

        def _thought(text: str, sig: bytes) -> Message:
            return Message.from_model_turn(
                [TurnPart.from_thought(text, signature=sig)], []
            )

        fake = FakeModelClient(
            complete_script=[
                _thought("一", b"sig-1"),
                _thought("二", b"sig-2"),
                _thought("三", b"sig-3"),
            ]
        )
        sink: list[Message] = []
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            context=None,
            recursion_limit=12,
            transcript_sink=sink,
        )

        # 重试照旧跑满：只想不答不是回答，所以它没被当成最终回复发出去
        assert len(fake.complete_calls) == 3
        assert result.text() == ""
        assert not result.tool_calls
        # 三轮的思考和签名都留在上下文里，每一次重试也都带着前面那些
        assert [len(msgs) for msgs, _ in fake.complete_calls] == [1, 2, 3]
        assert [m.thought_text() for m in sink] == ["一", "二", "三"]
        assert [m.turn_parts[0].signature for m in sink] == [
            b"sig-1",
            b"sig-2",
            b"sig-3",
        ]

    async def test_an_attempt_that_carried_nothing_is_not_sent_back(self):
        """什么都没带回来的那一轮不能进重试的请求：wire 上它就是一个空 turn。"""
        _run_loop, _ = _import_loops()
        empty = Message(role=Role.ASSISTANT, content="")
        fake = FakeModelClient(complete_script=[empty, empty, empty])
        sink: list[Message] = []
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            context=None,
            recursion_limit=12,
            transcript_sink=sink,
        )

        assert len(fake.complete_calls) == 3
        assert [len(msgs) for msgs, _ in fake.complete_calls] == [1, 1, 1]

    async def test_the_closing_call_keeps_what_its_retried_attempt_thought(self):
        """收尾那一次调用（预算用完、不带工具）也走同一条重试路径。"""
        _run_loop, _ = _import_loops()
        looping = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c", name="echo_tool", arguments={"text": "x"})],
        )
        thought_only = Message.from_model_turn(
            [TurnPart.from_thought("收尾前想了想", signature=b"sig-c")], []
        )
        fake = FakeModelClient(
            complete_script=[
                looping,
                looping,
                thought_only,
                Message(role=Role.ASSISTANT, content="收尾"),
            ]
        )
        sink: list[Message] = []
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[echo_tool],
            context=None,
            recursion_limit=2,
            transcript_sink=sink,
        )

        assert result.text() == "收尾"
        assert sink[-2].thought_text() == "收尾前想了想"
        assert sink[-2].turn_parts[0].signature == b"sig-c"
        assert sink[-1] is result

    async def test_the_stream_keeps_what_a_retried_attempt_thought(self):
        _, _stream_loop = _import_loops()
        call = ToolCall(id="c1", name="echo_tool", arguments={"text": "x"})
        fake = FakeModelClient(
            stream_script=[
                [
                    StreamChunk(reasoning="想了想", signature=b"sig-a"),
                    StreamChunk(finish_reason="stop"),
                ],
                [
                    StreamChunk(tool_call=call),
                    StreamChunk(finish_reason="tool_calls"),
                ],
                [StreamChunk(text="好了"), StreamChunk(finish_reason="stop")],
            ]
        )
        out = [
            c
            async for c in _stream_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[echo_tool],
                context=None,
                recursion_limit=4,
            )
        ]

        assert "".join(c.text or "" for c in out) == "好了"
        assert len(fake.stream_calls) == 3
        # 重试那一次带着刚想过的东西
        retried = fake.stream_calls[1][0][-1]
        assert retried.thought_text() == "想了想"
        assert retried.turn_parts[0].signature == b"sig-a"
        # 再往后它仍然在上下文里，排在工具那一轮前面
        third = fake.stream_calls[2][0]
        assert third[1].turn_parts[0].signature == b"sig-a"
        assert third[2].tool_calls == [call]
