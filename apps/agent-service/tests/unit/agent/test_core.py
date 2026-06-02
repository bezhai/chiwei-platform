"""test_core.py -- Agent unified interface tests (post-langchain cutover).

Covers the Agent layer over the neutral ``ModelClient`` (no langchain /
create_agent / CallbackHandler):
  - Agent.run(): returns final neutral Message, retry on transient errors,
    prompt compilation + message threading, recursion_limit passthrough.
  - Agent.stream(): forwards neutral StreamChunks, no retry after first yield.
  - Agent.extract(): structured output validated into a Pydantic model.
  - AgentConfig as frozen dataclass.

The ReAct loop control flow itself is de-risked separately in
``test_react_loop.py`` against a scripted fake ModelClient; here we mock the
loop functions / model client to assert the Agent's own responsibilities.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APITimeoutError
from pydantic import BaseModel

from app.agent.core import Agent, AgentConfig
from app.agent.context import AgentContext
from app.agent.neutral import Message, Role, StreamChunk

pytestmark = pytest.mark.unit

# Reusable test configs
_CFG = AgentConfig("test_prompt", "test-model", "test-agent")
_EXTRACT_CFG = AgentConfig(
    "relationship_filter", "relationship-model", "relationship-filter"
)
_NO_PROMPT_CFG = AgentConfig("", "guard-model", "guard")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_prompt():
    """Mock Langfuse prompt (text prompt)."""
    prompt = MagicMock()
    prompt.type = "text"
    prompt.compile.return_value = "You are a helpful assistant."
    return prompt


@pytest.fixture()
def mock_deps(mock_prompt):
    """Patch build_model_client, get_prompt, compile_to_messages, and the root span.

    The root span is patched to a no-op so tracing/langfuse never reaches the
    network in unit tests. ``compile_to_messages`` returns a neutral SYSTEM
    message by default.
    """
    model = AsyncMock()
    model.complete = AsyncMock(
        return_value=Message(role=Role.ASSISTANT, content="hello")
    )
    model.structured = AsyncMock(return_value={"v": "ok"})

    from contextlib import contextmanager

    @contextmanager
    def _noop_span(**_kwargs):
        yield MagicMock()

    with (
        patch(
            "app.agent.core.build_model_client",
            new_callable=AsyncMock,
            return_value=model,
        ) as mock_build,
        patch(
            "app.agent.core.get_prompt",
            return_value=mock_prompt,
        ) as mock_get_prompt,
        patch(
            "app.agent.core.compile_to_messages",
            return_value=[Message(role=Role.SYSTEM, content="You are a helpful assistant.")],
        ) as mock_compile,
        patch("app.agent.core._root_span", _noop_span),
    ):
        yield {
            "build_model_client": mock_build,
            "get_prompt": mock_get_prompt,
            "compile_to_messages": mock_compile,
            "model": model,
            "prompt": mock_prompt,
        }


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------


class TestRootSpanRobustness:
    """Tracing must never break the call — span machinery failures degrade."""

    def test_root_span_yields_even_when_enter_fails(self):
        from app.agent import core

        class _BadCM:
            def __enter__(self):
                raise RuntimeError("otel context enter exploded")

            def __exit__(self, *a):
                return False

        client = MagicMock()
        client.start_as_current_span.return_value = _BadCM()
        with patch.object(core, "_get_trace_client", return_value=client):
            entered = False
            with core._root_span(name="x", input=[], update_trace=True):
                entered = True
            assert entered  # the body ran despite the span enter blowing up

    def test_root_span_does_not_swallow_body_exception(self):
        from app.agent import core

        client = MagicMock()
        with patch.object(core, "_get_trace_client", return_value=client):
            with pytest.raises(ValueError, match="body boom"):
                with core._root_span(name="x", input=[], update_trace=False):
                    raise ValueError("body boom")

    def test_tool_span_yields_even_when_enter_fails(self):
        from app.agent import core

        class _BadCM:
            def __enter__(self):
                raise RuntimeError("tool span enter exploded")

            def __exit__(self, *a):
                return False

        client = MagicMock()
        client.start_as_current_span.return_value = _BadCM()
        with patch.object(core, "_get_trace_client", return_value=client):
            entered = False
            with core._tool_span(name="t", input={}):
                entered = True
            assert entered


class TestAgentConfig:
    def test_frozen(self):
        cfg = AgentConfig("p", "m", "t")
        with pytest.raises(AttributeError):
            cfg.prompt_id = "x"  # type: ignore[misc]

    def test_defaults(self):
        assert AgentConfig("p", "m").trace_name is None

    def test_replace(self):
        from dataclasses import replace

        cfg = AgentConfig("p", "m", "t")
        new = replace(cfg, model_id="new")
        assert new.model_id == "new"
        assert cfg.model_id == "m"


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun:
    async def test_returns_final_message(self, mock_deps):
        result = await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")],
            prompt_vars={"name": "test"},
        )
        assert isinstance(result, Message)
        assert result.text() == "hello"

    async def test_builds_model_client_from_config_model_id(self, mock_deps):
        await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")])
        mock_deps["build_model_client"].assert_awaited_once_with("test-model")

    async def test_tooldefs_passed_to_model(self, mock_deps):
        from app.agent.tooling import tool

        @tool
        async def my_tool(x: str) -> str:
            """A tool.

            Args:
                x: in.
            """
            return ""

        await Agent(_CFG, tools=[my_tool]).run(
            messages=[Message(role=Role.USER, content="hi")]
        )
        # the model saw the tool's neutral ToolDef
        tools = mock_deps["model"].complete.await_args.kwargs["tools"]
        assert tools is not None
        assert tools[0].name == "my_tool"

    async def test_no_tools_passes_none(self, mock_deps):
        await Agent(_CFG).run(messages=[Message(role=Role.USER, content="hi")])
        tools = mock_deps["model"].complete.await_args.kwargs["tools"]
        assert tools is None

    async def test_model_kwargs_forwarded_to_complete(self, mock_deps):
        # model_kwargs (e.g. reasoning_effort=low for the safety guard) must
        # reach the model call — dropping them is a silent behaviour regression.
        await Agent(_CFG, model_kwargs={"reasoning_effort": "low"}).run(
            messages=[Message(role=Role.USER, content="hi")]
        )
        kwargs = mock_deps["model"].complete.await_args.kwargs
        assert kwargs["reasoning_effort"] == "low"

    async def test_compiles_prompt_via_langfuse(self, mock_deps):
        await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")],
            prompt_vars={"key": "val"},
        )
        mock_deps["compile_to_messages"].assert_called_once()
        call_kwargs = mock_deps["compile_to_messages"].call_args.kwargs
        assert call_kwargs["key"] == "val"
        assert "currDate" in call_kwargs
        assert "currTime" in call_kwargs

    async def test_prompt_messages_prepended(self, mock_deps):
        mock_deps["compile_to_messages"].return_value = [
            Message(role=Role.SYSTEM, content="sys prompt"),
        ]
        user_msg = Message(role=Role.USER, content="hi")
        await Agent(_CFG).run(messages=[user_msg])

        sent = mock_deps["model"].complete.await_args.args[0]
        assert sent[0].role == Role.SYSTEM
        assert sent[0].text() == "sys prompt"
        assert sent[-1].text() == "hi"

    async def test_retries_on_transient_error(self, mock_deps):
        mock_deps["model"].complete = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                Message(role=Role.ASSISTANT, content="retry ok"),
            ]
        )
        result = await Agent(_CFG).run(
            messages=[Message(role=Role.USER, content="hi")],
            max_retries=2,
        )
        assert result.text() == "retry ok"
        assert mock_deps["model"].complete.call_count == 2

    async def test_raises_after_max_retries(self, mock_deps):
        mock_deps["model"].complete = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )
        with pytest.raises(APITimeoutError):
            await Agent(_CFG).run(
                messages=[Message(role=Role.USER, content="hi")],
                max_retries=2,
            )
        assert mock_deps["model"].complete.call_count == 2

    async def test_passes_context_to_tool_dispatch(self, mock_deps):
        from app.agent.runtime_context import get_context
        from app.agent.tooling import tool
        from app.agent.neutral import ToolCall

        seen: dict = {}

        @tool
        async def ctx_tool(x: str) -> str:
            """Reads context.

            Args:
                x: in.
            """
            seen["persona"] = get_context().persona_id
            return "done"

        # first completion requests the tool, second finishes
        mock_deps["model"].complete = AsyncMock(
            side_effect=[
                Message(
                    role=Role.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id="c1", name="ctx_tool", arguments={"x": "v"})],
                ),
                Message(role=Role.ASSISTANT, content="ok"),
            ]
        )
        ctx = AgentContext(message_id="m", chat_id="c", persona_id="luna")
        await Agent(_CFG, tools=[ctx_tool]).run(
            messages=[Message(role=Role.USER, content="go")],
            context=ctx,
        )
        assert seen["persona"] == "luna"


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


class TestStream:
    async def test_yields_chunks(self, mock_deps):
        async def fake_stream(messages, *, tools=None, **kwargs):
            for c in [StreamChunk(text="he"), StreamChunk(text="llo"),
                      StreamChunk(finish_reason="stop")]:
                yield c

        mock_deps["model"].stream = fake_stream

        collected = []
        async for chunk in Agent(_CFG).stream(
            messages=[Message(role=Role.USER, content="hi")],
        ):
            collected.append(chunk)

        texts = [c.text for c in collected if c.text]
        assert "".join(texts) == "hello"

    async def test_no_retry_after_tokens_yielded(self, mock_deps):
        call_count = 0

        async def failing_stream(messages, *, tools=None, **kwargs):
            nonlocal call_count
            call_count += 1
            yield StreamChunk(text="partial")
            raise APITimeoutError(request=MagicMock())

        mock_deps["model"].stream = failing_stream

        with pytest.raises(APITimeoutError):
            async for _ in Agent(_CFG).stream(
                messages=[Message(role=Role.USER, content="hi")],
                max_retries=3,
            ):
                pass

        assert call_count == 1  # no retry after yield

    async def test_retries_before_first_token(self, mock_deps):
        call_count = 0

        async def failing_then_ok(messages, *, tools=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise APITimeoutError(request=MagicMock())
                yield  # pragma: no cover
            yield StreamChunk(text="ok")
            yield StreamChunk(finish_reason="stop")

        mock_deps["model"].stream = failing_then_ok

        collected = []
        async for chunk in Agent(_CFG).stream(
            messages=[Message(role=Role.USER, content="hi")],
            max_retries=3,
        ):
            collected.append(chunk)

        assert call_count == 2  # first attempt failed, second succeeded
        assert "".join(c.text or "" for c in collected) == "ok"


# ---------------------------------------------------------------------------
# empty prompt_id guard
# ---------------------------------------------------------------------------


class TestEmptyPromptGuard:
    async def test_run_rejects_empty_prompt_id(self, mock_deps):
        with pytest.raises(ValueError, match="non-empty prompt_id"):
            await Agent(_NO_PROMPT_CFG).run(
                messages=[Message(role=Role.USER, content="hi")]
            )

    async def test_stream_rejects_empty_prompt_id(self, mock_deps):
        with pytest.raises(ValueError, match="non-empty prompt_id"):
            async for _ in Agent(_NO_PROMPT_CFG).stream(
                messages=[Message(role=Role.USER, content="hi")]
            ):
                pass

    async def test_extract_allows_empty_prompt_id(self, mock_deps):
        """Guard agents have empty prompt_id and only use extract()."""

        class Out(BaseModel):
            v: str

        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        result = await Agent(_NO_PROMPT_CFG).extract(
            Out, messages=[Message(role=Role.USER, content="test")]
        )
        assert result.v == "ok"
        mock_deps["get_prompt"].assert_not_called()


# ---------------------------------------------------------------------------
# recursion_limit
# ---------------------------------------------------------------------------


class TestRecursionLimit:
    async def test_default_recursion_limit_caps_tool_loop(self, mock_deps):
        from app.agent.neutral import ToolCall
        from app.agent.tooling import tool

        @tool
        async def loop_tool(x: str) -> str:
            """loops.

            Args:
                x: in.
            """
            return "again"

        # always request a tool → loop bounded by the model-call budget
        # (default 6, matching the legacy LangGraph ~6 model calls).
        mock_deps["model"].complete = AsyncMock(
            return_value=Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c", name="loop_tool", arguments={"x": "v"})],
            )
        )
        await Agent(_CFG, tools=[loop_tool]).run(
            messages=[Message(role=Role.USER, content="go")]
        )
        assert mock_deps["model"].complete.call_count == 6

    async def test_custom_recursion_limit(self, mock_deps):
        from app.agent.neutral import ToolCall
        from app.agent.tooling import tool

        @tool
        async def loop_tool(x: str) -> str:
            """loops.

            Args:
                x: in.
            """
            return "again"

        cfg = AgentConfig("p", "m", "t", recursion_limit=4)
        mock_deps["model"].complete = AsyncMock(
            return_value=Message(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c", name="loop_tool", arguments={"x": "v"})],
            )
        )
        await Agent(cfg, tools=[loop_tool]).run(
            messages=[Message(role=Role.USER, content="go")]
        )
        assert mock_deps["model"].complete.call_count == 4


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------


class TestExtract:
    async def test_returns_pydantic_model(self, mock_deps):
        class Score(BaseModel):
            name: str
            value: float

        mock_deps["model"].structured = AsyncMock(
            return_value={"name": "test", "value": 0.9}
        )

        result = await Agent(_EXTRACT_CFG).extract(
            Score,
            messages=[Message(role=Role.USER, content="rate this")],
            prompt_vars={"key": "val"},
        )

        assert isinstance(result, Score)
        assert result.name == "test"
        assert result.value == 0.9

    async def test_passes_response_model_schema(self, mock_deps):
        class Score(BaseModel):
            name: str

        mock_deps["model"].structured = AsyncMock(return_value={"name": "x"})

        await Agent(_EXTRACT_CFG).extract(
            Score, messages=[Message(role=Role.USER, content="t")]
        )
        schema = mock_deps["model"].structured.await_args.kwargs["schema"]
        assert "properties" in schema
        assert "name" in schema["properties"]

    async def test_extract_forwards_model_kwargs(self, mock_deps):
        # the safety guard configures Agent(..., model_kwargs={"reasoning_effort":
        # "low"}) and only calls extract — the kwargs must reach structured().
        class Out(BaseModel):
            v: str

        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        await Agent(
            _EXTRACT_CFG, model_kwargs={"reasoning_effort": "low"}
        ).extract(Out, messages=[Message(role=Role.USER, content="t")])
        kwargs = mock_deps["model"].structured.await_args.kwargs
        assert kwargs["reasoning_effort"] == "low"

    async def test_extract_retries(self, mock_deps):
        class Result(BaseModel):
            ok: bool

        mock_deps["model"].structured = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                {"ok": True},
            ]
        )

        result = await Agent(_EXTRACT_CFG).extract(
            Result,
            messages=[Message(role=Role.USER, content="test")],
            max_retries=2,
        )
        assert result.ok is True
        assert mock_deps["model"].structured.call_count == 2

    async def test_extract_skips_prompt_when_empty(self, mock_deps):
        """Guard agents have empty prompt_id — extract should not call get_prompt."""

        class Out(BaseModel):
            v: str

        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        await Agent(_NO_PROMPT_CFG).extract(
            Out,
            messages=[Message(role=Role.USER, content="test")],
        )
        mock_deps["get_prompt"].assert_not_called()

    async def test_extract_with_chat_prompt_messages_prepended(self, mock_deps):
        """Chat prompt should produce multiple messages prepended to input."""

        class Out(BaseModel):
            v: str

        mock_deps["compile_to_messages"].return_value = [
            Message(role=Role.SYSTEM, content="You are a guard."),
            Message(role=Role.USER, content="Check: test input"),
        ]
        mock_deps["model"].structured = AsyncMock(return_value={"v": "ok"})

        await Agent(_EXTRACT_CFG).extract(
            Out, messages=[Message(role=Role.USER, content="more")]
        )

        sent = mock_deps["model"].structured.await_args.args[0]
        assert len(sent) == 3
        assert sent[0].role == Role.SYSTEM
        assert sent[1].role == Role.USER
        assert sent[2].text() == "more"
