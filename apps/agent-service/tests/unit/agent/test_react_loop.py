"""De-risk the hand-written ReAct loop (T4b cutover) against a fake ModelClient.

These tests prove the loop behind ``Agent.run`` in isolation: complete →
(tool_calls? dispatch each, append tool messages, loop) → final assistant
Message; guarded by recursion_limit; a call to a tool declared to end the round
(``ends_round``) ends it, and the calls after it are answered as never run.

The fake ModelClient is a scripted ``ModelClient`` returning canned neutral
``Message`` sequences, so the loop's control flow (not a real provider) is
what's under test. Tools are synthetic neutral ``@tool``s.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.agent.client import ModelClient
from app.agent.neutral import (
    ContentBlock,
    Message,
    Role,
    ToolCall,
    ToolDef,
    TurnPart,
)
from app.agent.tooling import tool

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake ModelClient — scripted neutral responses
# ---------------------------------------------------------------------------


class FakeModelClient(ModelClient):
    """A ModelClient that replays scripted responses, recording what it saw.

    ``complete_script`` / ``structured_script`` are lists popped per call. Each
    call records the messages + tools it was handed so the loop's message
    threading (assistant turn + tool results fed back) can be asserted.
    """

    def __init__(
        self,
        *,
        complete_script: list[Message] | None = None,
        structured_script: list[dict] | None = None,
    ) -> None:
        self._complete = list(complete_script or [])
        self._structured = list(structured_script or [])
        self.complete_calls: list[tuple[list[Message], list[ToolDef] | None]] = []
        self.structured_calls: list[tuple[list[Message], dict]] = []
        # per-call kwargs so passthrough (e.g. session_id for the prompt-cache
        # key) can be asserted.
        self.complete_kwargs: list[dict] = []

    async def complete(self, messages, *, tools=None, **kwargs):
        self.complete_calls.append((list(messages), tools))
        self.complete_kwargs.append(dict(kwargs))
        return self._complete.pop(0)

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
async def ends_it() -> str:
    """A tool declared to end the round."""
    return "ok"


# ---------------------------------------------------------------------------
# Helpers to import the loop functions under test
# ---------------------------------------------------------------------------


def _import_loops():
    from app.agent.core import _run_loop

    return _run_loop


# ---------------------------------------------------------------------------
# run loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_no_tool_call_returns_final_message(self):
        _run_loop = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi there")]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            recursion_limit=12,
        )
        assert isinstance(result, Message)
        assert result.text() == "hi there"
        assert len(fake.complete_calls) == 1

    async def test_single_tool_call_then_final(self):
        _run_loop = _import_loops()
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

    async def test_a_tool_that_ends_the_round_ends_without_second_model_call(self):
        _run_loop = _import_loops()
        call = ToolCall(id="c1", name="ends_it", arguments={})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call]),
                Message(role=Role.ASSISTANT, content="should not run"),
            ]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[ends_it, echo_tool],
            ends_round=frozenset({"ends_it"}),
            recursion_limit=12,
        )
        assert result.text() == ""
        assert len(fake.complete_calls) == 1

    async def test_parallel_tool_calls_all_dispatched(self):
        _run_loop = _import_loops()
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
            recursion_limit=12,
        )
        assert result.text() == "fin"
        second_msgs = fake.complete_calls[1][0]
        tool_msgs = [m for m in second_msgs if m.role == Role.TOOL]
        assert {m.tool_call_id for m in tool_msgs} == {"c1", "c2"}

    async def test_a_terminal_tool_leaves_no_empty_message_in_the_sink(self):
        """A terminal tool ends the run; the empty assistant message it returns
        stays out of ``transcript_sink``.

        The sink is what a caller stores as the agent's continuous context. An
        assistant message with no text and no tool calls carries nothing, and a
        caller that ends most of its rounds on a terminal tool would accumulate
        one of them per round forever.
        """
        _run_loop = _import_loops()
        call = ToolCall(id="c1", name="ends_it", arguments={})
        fake = FakeModelClient(
            complete_script=[
                Message(role=Role.ASSISTANT, content="", tool_calls=[call])
            ]
        )
        sink: list[Message] = []
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[ends_it, echo_tool],
            ends_round=frozenset({"ends_it"}),
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
        _run_loop = _import_loops()
        calls = [
            ToolCall(id="c1", name="ends_it", arguments={}),
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
            tools=[ends_it, echo_tool],
            ends_round=frozenset({"ends_it"}),
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

        _run_loop = _import_loops()
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
        _run_loop = _import_loops()
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
        _run_loop = _import_loops()
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
            recursion_limit=2,
            transcript_sink=sink,
        )

        assert result.text() == ""
        assert result.tool_calls == []
        assert all(
            m.text().strip() or m.tool_calls or m.role is Role.TOOL for m in sink
        ), "一条既没正文也没调用的消息进了上下文"

    async def test_tools_passed_as_tooldefs(self):
        _run_loop = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[echo_tool],
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
        _run_loop = _import_loops()
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
        _run_loop = _import_loops()
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
        _run_loop = _import_loops()
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
            recursion_limit=12,
        )
        assert len(spans) == 1
        spans[0].update.assert_called_once_with(output="echoed:x")

    async def test_run_loop_block_list_output_is_json_serialisable(self, monkeypatch):
        from app.agent import core

        spans: list = []
        monkeypatch.setattr(core, "_tool_span", _recording_tool_span(spans))
        _run_loop = _import_loops()
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
            recursion_limit=12,
        )
        output = spans[0].update.call_args.kwargs["output"]
        # plain dicts (not ContentBlock objects) so langfuse can serialise it
        assert isinstance(output, list)
        assert all(isinstance(b, dict) for b in output)
        json.dumps(output)  # must not raise
        assert output[0]["type"] == "text"
        assert output[1]["type"] == "image_url"

# ---------------------------------------------------------------------------
# session_id passthrough — the loop forwards session_id to model.complete so
# the adapter can use it as the prompt-cache key. Default (no session_id)
# forwards None, which the adapter no-ops on.
# ---------------------------------------------------------------------------


class TestSessionIdPassthrough:
    async def test_run_loop_forwards_session_id_to_complete(self):
        _run_loop = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            recursion_limit=12,
            session_id="coe-world-life2:world:2026-06-06",
        )
        assert (
            fake.complete_kwargs[0]["session_id"]
            == "coe-world-life2:world:2026-06-06"
        )

    async def test_run_loop_default_session_id_is_none(self):
        _run_loop = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            recursion_limit=12,
        )
        assert fake.complete_kwargs[0].get("session_id") is None

    async def test_run_loop_session_id_survives_model_kwargs_collision(self):
        """A session_id in model_kwargs must not TypeError-clash with the loop's
        explicit session_id; the loop's trace session_id wins, others survive."""
        _run_loop = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi")]
        )
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="hello")],
            tools=[],
            recursion_limit=12,
            session_id="real",
            model_kwargs={"session_id": "stale", "reasoning_effort": "low"},
        )
        assert fake.complete_kwargs[0]["session_id"] == "real"
        assert fake.complete_kwargs[0]["reasoning_effort"] == "low"


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
        _run_loop = _import_loops()
        empty = Message(role=Role.ASSISTANT, content="")
        fake = FakeModelClient(
            complete_script=[empty, Message(role=Role.ASSISTANT, content="real reply")]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            recursion_limit=12,
        )
        assert result.text() == "real reply"
        assert len(fake.complete_calls) == 2

    async def test_reasoning_only_completion_counts_as_empty_and_is_retried(self):
        # text blank, no tool_calls, but a thought part on the turn — thinking
        # is never surfaced to the user, so a "thought but didn't answer" turn
        # must still count as empty and get retried.
        _run_loop = _import_loops()
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
            recursion_limit=12,
        )
        assert result.text() == "ok"
        assert len(fake.complete_calls) == 2

    async def test_whitespace_only_text_counts_as_empty_and_is_retried(self):
        _run_loop = _import_loops()
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

        _run_loop = _import_loops()
        empty = Message(role=Role.ASSISTANT, content="")
        fake = FakeModelClient(complete_script=[empty, empty, empty])
        with caplog.at_level(logging.WARNING):
            result = await _run_loop(
                fake,
                messages=[Message(role=Role.USER, content="go")],
                tools=[],
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
        _run_loop = _import_loops()
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
            recursion_limit=12,
        )
        assert result.text() == "done"
        # exactly 2 calls: the tool-call turn + the final turn, no extra retry
        assert len(fake.complete_calls) == 2

    async def test_normal_non_empty_result_is_not_retried(self):
        # the common case: a single model call, zero added request cost.
        _run_loop = _import_loops()
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="hi there")]
        )
        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            recursion_limit=12,
        )
        assert result.text() == "hi there"
        assert len(fake.complete_calls) == 1


# ---------------------------------------------------------------------------
# a model turn's sequence has to live through the loop's own rebuilds
#
# One place hand-copies an assistant turn field by field (the closing call with
# its tool calls stripped) and one decides whether a turn carries anything at
# all. Both used to be written on the premise that nothing replays a model's thoughts — now they do replay,
# and a turn that only thought is not empty on the wire any more.
# ---------------------------------------------------------------------------


class TestTheTurnSequenceSurvivesTheLoop:
    async def test_the_closing_turn_keeps_its_thoughts_and_drops_the_stripped_call(
        self,
    ):
        _run_loop = _import_loops()
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
        _run_loop = _import_loops()
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
            recursion_limit=2,
            transcript_sink=sink,
        )

        stored = sink[-1]
        assert stored.thought_text() == "想了想"
        assert stored.turn_parts[0].signature == b"sig-t"

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
        _run_loop = _import_loops()
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
        _run_loop = _import_loops()

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
        _run_loop = _import_loops()
        empty = Message(role=Role.ASSISTANT, content="")
        fake = FakeModelClient(complete_script=[empty, empty, empty])
        sink: list[Message] = []
        await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[],
            recursion_limit=12,
            transcript_sink=sink,
        )

        assert len(fake.complete_calls) == 3
        assert [len(msgs) for msgs, _ in fake.complete_calls] == [1, 1, 1]

    async def test_the_closing_call_keeps_what_its_retried_attempt_thought(self):
        """收尾那一次调用（预算用完、不带工具）也走同一条重试路径。"""
        _run_loop = _import_loops()
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
            recursion_limit=2,
            transcript_sink=sink,
        )

        assert result.text() == "收尾"
        assert sink[-2].thought_text() == "收尾前想了想"
        assert sink[-2].turn_parts[0].signature == b"sig-c"
        assert sink[-1] is result

# ---------------------------------------------------------------------------
# ends_round — which tool ends the run is declared, not named in the loop
# ---------------------------------------------------------------------------


@tool
async def finish() -> str:
    """Finish this round."""
    return "done"


@tool
async def stop_for_now() -> str:
    """A tool that happens to carry her stopping tool's name."""
    return "就到这儿。"


class TestEndsRound:
    async def test_a_tool_declared_to_end_the_round_ends_it_and_answers_the_rest(self):
        from app.agent.core import NEVER_RAN, _run_loop

        calls = [
            ToolCall(id="c1", name="finish", arguments={}),
            ToolCall(id="c2", name="echo_tool", arguments={"text": "x"}),
        ]
        fake = FakeModelClient(
            complete_script=[Message(role=Role.ASSISTANT, content="", tool_calls=calls)]
        )
        sink: list[Message] = []

        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[finish, echo_tool],
            ends_round=frozenset({"finish"}),
            recursion_limit=12,
            transcript_sink=sink,
        )

        assert result.text() == ""
        assert len(fake.complete_calls) == 1
        answered = {m.tool_call_id: m.text() for m in sink if m.role is Role.TOOL}
        assert answered == {"c1": "done", "c2": NEVER_RAN}

    async def test_a_tool_not_declared_to_end_the_round_does_not_end_it_whatever_its_name(
        self,
    ):
        from app.agent.core import _run_loop

        fake = FakeModelClient(
            complete_script=[
                Message(
                    role=Role.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id="c1", name="stop_for_now", arguments={})],
                ),
                Message(role=Role.ASSISTANT, content="还在。"),
            ]
        )

        result = await _run_loop(
            fake,
            messages=[Message(role=Role.USER, content="go")],
            tools=[stop_for_now],
            ends_round=frozenset(),
            recursion_limit=12,
        )

        assert result.text() == "还在。"
        assert len(fake.complete_calls) == 2
