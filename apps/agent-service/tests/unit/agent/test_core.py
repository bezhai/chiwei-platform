"""test_core.py -- Agent unified interface tests.

Covers:
  - Agent.run(): returns AIMessage, retry on transient errors
  - Agent.stream(): yields chunks, no retry after yield
  - Agent.extract(): structured output with Pydantic model
  - AgentConfig as frozen dataclass
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
)
from openai import APITimeoutError
from pydantic import BaseModel

from app.agent.core import Agent, AgentConfig

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
def fake_agent():
    """Mock LangGraph agent returned by create_agent."""
    agent = AsyncMock()
    agent.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="hello")]}
    )
    return agent


@pytest.fixture()
def mock_prompt():
    """Mock Langfuse prompt."""
    prompt = MagicMock()
    prompt.type = "text"
    prompt.compile.return_value = "You are a helpful assistant."
    return prompt


@pytest.fixture()
def mock_deps(fake_agent, mock_prompt):
    """Patch build_chat_model, get_prompt, create_agent, compile_to_messages, and CallbackHandler."""
    mock_model = AsyncMock()
    mock_model.with_structured_output = MagicMock()

    with (
        patch(
            "app.agent.core.build_chat_model",
            new_callable=AsyncMock,
            return_value=mock_model,
        ) as mock_build,
        patch(
            "app.agent.core.get_prompt",
            return_value=mock_prompt,
        ) as mock_get_prompt,
        patch(
            "app.agent.core.create_agent",
            return_value=fake_agent,
        ) as mock_create,
        patch(
            "app.agent.core.compile_to_messages",
            return_value=[SystemMessage(content="You are a helpful assistant.")],
        ) as mock_compile,
        patch(
            "app.agent.core.CallbackHandler",
            return_value=MagicMock(),
        ) as mock_callback,
    ):
        yield {
            "build_chat_model": mock_build,
            "get_prompt": mock_get_prompt,
            "create_agent": mock_create,
            "compile_to_messages": mock_compile,
            "callback_handler": mock_callback,
            "agent": fake_agent,
            "model": mock_model,
            "prompt": mock_prompt,
        }


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------


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
    async def test_returns_ai_message(self, mock_deps):
        result = await Agent(_CFG).run(
            messages=[HumanMessage(content="hi")],
            prompt_vars={"name": "test"},
        )
        assert isinstance(result, AIMessage)
        assert result.content == "hello"

    async def test_creates_agent_with_tools(self, mock_deps):
        tools = ["tool_a", "tool_b"]
        await Agent(_CFG, tools=tools).run(messages=[HumanMessage(content="hi")])

        mock_deps["create_agent"].assert_called_once()
        call_kwargs = mock_deps["create_agent"].call_args
        assert call_kwargs[0][1] == tools  # second positional arg = tools

    async def test_creates_agent_without_tools(self, mock_deps):
        await Agent(_CFG).run(messages=[HumanMessage(content="hi")])

        mock_deps["create_agent"].assert_called_once()
        call_kwargs = mock_deps["create_agent"].call_args
        assert call_kwargs[0][1] == []  # empty tools

    async def test_compiles_prompt_via_langfuse(self, mock_deps):
        await Agent(_CFG).run(
            messages=[HumanMessage(content="hi")],
            prompt_vars={"key": "val"},
        )
        mock_deps["compile_to_messages"].assert_called_once()
        call_kwargs = mock_deps["compile_to_messages"].call_args.kwargs
        assert call_kwargs["key"] == "val"
        assert "currDate" in call_kwargs
        assert "currTime" in call_kwargs

    async def test_create_agent_called_without_system_prompt(self, mock_deps):
        await Agent(_CFG).run(messages=[HumanMessage(content="hi")])

        call_kwargs = mock_deps["create_agent"].call_args.kwargs
        assert "system_prompt" not in call_kwargs or call_kwargs.get("system_prompt") is None

    async def test_prompt_messages_prepended(self, mock_deps):
        mock_deps["compile_to_messages"].return_value = [
            SystemMessage(content="sys prompt"),
        ]
        user_msg = HumanMessage(content="hi")
        await Agent(_CFG).run(messages=[user_msg])

        invoke_args = mock_deps["agent"].ainvoke.call_args[0][0]
        msgs = invoke_args["messages"]
        assert isinstance(msgs[0], SystemMessage)
        assert msgs[0].content == "sys prompt"
        assert msgs[-1] is user_msg

    async def test_retries_on_transient_error(self, mock_deps):
        mock_deps["agent"].ainvoke = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                {"messages": [AIMessage(content="retry ok")]},
            ]
        )
        result = await Agent(_CFG).run(
            messages=[HumanMessage(content="hi")],
            max_retries=2,
        )
        assert result.content == "retry ok"
        assert mock_deps["agent"].ainvoke.call_count == 2

    async def test_raises_after_max_retries(self, mock_deps):
        mock_deps["agent"].ainvoke = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )
        with pytest.raises(APITimeoutError):
            await Agent(_CFG).run(
                messages=[HumanMessage(content="hi")],
                max_retries=2,
            )
        assert mock_deps["agent"].ainvoke.call_count == 2

    async def test_config_has_trace_name(self, mock_deps):
        await Agent(_CFG).run(messages=[HumanMessage(content="hi")])

        call_kwargs = mock_deps["agent"].ainvoke.call_args.kwargs
        assert call_kwargs["config"]["run_name"] == "test-agent"

    async def test_updates_trace_by_default(self, mock_deps):
        await Agent(_CFG).run(messages=[HumanMessage(content="hi")])

        mock_deps["callback_handler"].assert_called_once_with(update_trace=True)

    async def test_can_disable_trace_updates(self, mock_deps):
        await Agent(_CFG, update_trace=False).run(messages=[HumanMessage(content="hi")])

        mock_deps["callback_handler"].assert_called_once_with(update_trace=False)

    async def test_passes_context(self, mock_deps):
        from app.agent.context import AgentContext

        ctx = AgentContext(message_id="m1", chat_id="c1", persona_id="p1")
        await Agent(_CFG).run(
            messages=[HumanMessage(content="hi")],
            context=ctx,
        )
        call_kwargs = mock_deps["agent"].ainvoke.call_args.kwargs
        assert call_kwargs["context"] is ctx


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


class TestStream:
    async def test_yields_chunks(self, mock_deps):
        chunks = [
            (AIMessageChunk(content="he"), None),
            (AIMessageChunk(content="llo"), None),
        ]

        async def fake_astream(inp, *, context=None, stream_mode=None, config=None):
            for c in chunks:
                yield c

        mock_deps["agent"].astream = fake_astream

        collected = []
        async for token in Agent(_CFG).stream(
            messages=[HumanMessage(content="hi")],
        ):
            collected.append(token)

        assert len(collected) == 2
        assert collected[0].content == "he"

    async def test_no_retry_after_tokens_yielded(self, mock_deps):
        call_count = 0

        async def failing_astream(inp, *, context=None, stream_mode=None, config=None):
            nonlocal call_count
            call_count += 1
            yield AIMessageChunk(content="partial"), None
            raise APITimeoutError(request=MagicMock())

        mock_deps["agent"].astream = failing_astream

        with pytest.raises(APITimeoutError):
            async for _ in Agent(_CFG).stream(
                messages=[HumanMessage(content="hi")],
                max_retries=3,
            ):
                pass

        assert call_count == 1  # no retry after yield

    async def test_retries_before_first_token(self, mock_deps):
        """Failure before any token is yielded should trigger retry."""
        call_count = 0

        async def failing_then_ok(inp, *, context=None, stream_mode=None, config=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise APITimeoutError(request=MagicMock())
            yield AIMessageChunk(content="ok"), None

        mock_deps["agent"].astream = failing_then_ok

        collected = []
        async for token in Agent(_CFG).stream(
            messages=[HumanMessage(content="hi")],
            max_retries=3,
        ):
            collected.append(token)

        assert call_count == 2  # first attempt failed, second succeeded
        assert len(collected) == 1
        assert collected[0].content == "ok"


# ---------------------------------------------------------------------------
# empty prompt_id guard
# ---------------------------------------------------------------------------


class TestEmptyPromptGuard:
    async def test_run_rejects_empty_prompt_id(self, mock_deps):
        with pytest.raises(ValueError, match="non-empty prompt_id"):
            await Agent(_NO_PROMPT_CFG).run(messages=[HumanMessage(content="hi")])

    async def test_stream_rejects_empty_prompt_id(self, mock_deps):
        with pytest.raises(ValueError, match="non-empty prompt_id"):
            async for _ in Agent(_NO_PROMPT_CFG).stream(
                messages=[HumanMessage(content="hi")]
            ):
                pass

    async def test_extract_allows_empty_prompt_id(self, mock_deps):
        """Guard agents have empty prompt_id and only use extract()."""

        class Out(BaseModel):
            v: str

        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=Out(v="ok"))
        mock_deps["model"].with_structured_output.return_value = structured

        result = await Agent(_NO_PROMPT_CFG).extract(
            Out, messages=[HumanMessage(content="test")]
        )
        assert result.v == "ok"
        mock_deps["get_prompt"].assert_not_called()


# ---------------------------------------------------------------------------
# recursion_limit
# ---------------------------------------------------------------------------


class TestRecursionLimit:
    async def test_default_recursion_limit(self, mock_deps):
        await Agent(_CFG).run(messages=[HumanMessage(content="hi")])
        config = mock_deps["agent"].ainvoke.call_args.kwargs["config"]
        assert config["recursion_limit"] == 12

    async def test_custom_recursion_limit(self, mock_deps):
        cfg = AgentConfig("p", "m", "t", recursion_limit=42)
        await Agent(cfg).run(messages=[HumanMessage(content="hi")])
        config = mock_deps["agent"].ainvoke.call_args.kwargs["config"]
        assert config["recursion_limit"] == 42


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------


class TestExtract:
    async def test_returns_pydantic_model(self, mock_deps):
        class Score(BaseModel):
            name: str
            value: float

        expected = Score(name="test", value=0.9)
        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=expected)
        mock_deps["model"].with_structured_output.return_value = structured

        result = await Agent(_EXTRACT_CFG).extract(
            Score,
            messages=[HumanMessage(content="rate this")],
            prompt_vars={"key": "val"},
        )

        assert isinstance(result, Score)
        assert result.name == "test"
        mock_deps["model"].with_structured_output.assert_called_once_with(Score)

    async def test_extract_retries(self, mock_deps):
        class Result(BaseModel):
            ok: bool

        structured = AsyncMock()
        structured.ainvoke = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                Result(ok=True),
            ]
        )
        mock_deps["model"].with_structured_output.return_value = structured

        result = await Agent(_EXTRACT_CFG).extract(
            Result,
            messages=[HumanMessage(content="test")],
            max_retries=2,
        )
        assert result.ok is True
        assert structured.ainvoke.call_count == 2

    async def test_extract_skips_prompt_when_empty(self, mock_deps):
        """Guard agents have empty prompt_id — extract should not call get_prompt."""

        class Out(BaseModel):
            v: str

        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=Out(v="ok"))
        mock_deps["model"].with_structured_output.return_value = structured

        await Agent(_NO_PROMPT_CFG).extract(
            Out,
            messages=[HumanMessage(content="test")],
        )

        mock_deps["get_prompt"].assert_not_called()

    async def test_extract_passes_model_kwargs(self, mock_deps):
        class Out(BaseModel):
            v: str

        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=Out(v="ok"))
        mock_deps["model"].with_structured_output.return_value = structured

        await Agent(
            _EXTRACT_CFG, model_kwargs={"reasoning_effort": "low"}
        ).extract(Out, messages=[])

        mock_deps["build_chat_model"].assert_called_once_with(
            "relationship-model", reasoning_effort="low"
        )

    async def test_extract_with_chat_prompt_messages(self, mock_deps):
        """Chat prompt should produce multiple messages prepended to input."""

        class Out(BaseModel):
            v: str

        mock_deps["compile_to_messages"].return_value = [
            SystemMessage(content="You are a guard."),
            HumanMessage(content="Check: test input"),
        ]

        structured = AsyncMock()
        structured.ainvoke = AsyncMock(return_value=Out(v="ok"))
        mock_deps["model"].with_structured_output.return_value = structured

        await Agent(_EXTRACT_CFG).extract(Out, messages=[])

        invoke_args = structured.ainvoke.call_args[0][0]
        assert len(invoke_args) == 2
        assert isinstance(invoke_args[0], SystemMessage)
        assert isinstance(invoke_args[1], HumanMessage)
