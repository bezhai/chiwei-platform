"""test_core.py -- Agent unified interface tests.

Covers:
  - Agent.run() plain path: returns AIMessage, retry, system prompt injection
  - Agent.stream() plain path: yields chunks
  - Agent.extract(): structured output with Pydantic model
  - Agent.run() agentic path: delegates to LangGraph agent
  - Agent.stream() agentic path: yields tokens, no retry after yield
  - AGENTS registry lookups and overrides
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

from app.agent.core import AGENTS, Agent, AgentConfig, _resolve_config

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_model():
    """Mock BaseChatModel."""
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="hello"))
    model.with_structured_output = MagicMock()
    return model


@pytest.fixture()
def mock_prompt():
    """Mock Langfuse prompt that returns a compiled string."""
    prompt = MagicMock()
    prompt.compile.return_value = "You are a helpful assistant."
    return prompt


@pytest.fixture()
def mock_deps(mock_model, mock_prompt):
    """Patch build_chat_model, get_prompt, and CallbackHandler."""
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
            "app.agent.core.CallbackHandler",
            return_value=MagicMock(),
        ),
    ):
        yield {
            "build_chat_model": mock_build,
            "get_prompt": mock_get_prompt,
            "model": mock_model,
            "prompt": mock_prompt,
        }


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    """AGENTS registry and _resolve_config."""

    def test_all_13_agents_registered(self):
        assert len(AGENTS) == 13

    def test_resolve_known_agent(self):
        cfg = _resolve_config("main")
        assert cfg.prompt_id == "main"
        assert cfg.model_id == "main-chat-model"
        assert cfg.trace_name == "main"

    def test_resolve_unknown_agent_raises(self):
        with pytest.raises(KeyError, match="no-such-agent"):
            _resolve_config("no-such-agent")

    def test_override_model_id(self):
        cfg = _resolve_config("main", model_id="gpt-4o")
        assert cfg.model_id == "gpt-4o"
        assert cfg.prompt_id == "main"  # unchanged

    def test_override_prompt_id(self):
        cfg = _resolve_config("main", prompt_id="custom-prompt")
        assert cfg.prompt_id == "custom-prompt"

    def test_agent_config_repr(self):
        cfg = AgentConfig("p", "m", "t")
        assert "p" in repr(cfg)


# ---------------------------------------------------------------------------
# Plain path: run()
# ---------------------------------------------------------------------------


class TestRunPlain:
    """Agent.run() without tools (plain LLM path)."""

    async def test_returns_ai_message(self, mock_deps):
        result = await Agent("afterthought").run(
            messages=[{"role": "user", "content": "hi"}],
            prompt_vars={"name": "test"},
        )
        assert isinstance(result, AIMessage)
        assert result.content == "hello"

    async def test_compiles_prompt_into_system_message(self, mock_deps):
        await Agent("afterthought").run(
            messages=[{"role": "user", "content": "hi"}],
            prompt_vars={"key": "val"},
        )
        mock_deps["prompt"].compile.assert_called_once_with(key="val")

        call_args = mock_deps["model"].ainvoke.call_args
        sent = call_args[0][0]
        assert isinstance(sent[0], SystemMessage)
        assert sent[0].content == "You are a helpful assistant."

    async def test_retries_on_transient_error(self, mock_deps):
        mock_deps["model"].ainvoke = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                AIMessage(content="retry ok"),
            ]
        )
        result = await Agent("afterthought").run(
            messages=[HumanMessage(content="hi")],
            max_retries=2,
        )
        assert result.content == "retry ok"
        assert mock_deps["model"].ainvoke.call_count == 2

    async def test_raises_after_max_retries(self, mock_deps):
        mock_deps["model"].ainvoke = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )
        with pytest.raises(APITimeoutError):
            await Agent("afterthought").run(
                messages=[HumanMessage(content="hi")],
                max_retries=2,
            )
        assert mock_deps["model"].ainvoke.call_count == 2

    async def test_passes_config_with_callbacks(self, mock_deps):
        await Agent("afterthought").run(
            messages=[HumanMessage(content="hi")],
        )
        call_args = mock_deps["model"].ainvoke.call_args
        config = call_args.kwargs.get("config") or call_args[1].get("config")
        assert "callbacks" in config
        assert config["run_name"] == "afterthought"


# ---------------------------------------------------------------------------
# Plain path: stream()
# ---------------------------------------------------------------------------


class TestStreamPlain:
    """Agent.stream() without tools."""

    async def test_yields_chunks(self, mock_deps):
        chunks = [AIMessageChunk(content="hel"), AIMessageChunk(content="lo")]

        async def fake_astream(messages, *, config=None):
            for c in chunks:
                yield c

        mock_deps["model"].astream = fake_astream

        collected = []
        async for chunk in Agent("afterthought").stream(
            messages=[HumanMessage(content="hi")],
        ):
            collected.append(chunk)

        assert len(collected) == 2
        assert collected[0].content == "hel"
        assert collected[1].content == "lo"


# ---------------------------------------------------------------------------
# Plain path: extract()
# ---------------------------------------------------------------------------


class TestExtract:
    """Agent.extract() structured output."""

    async def test_returns_pydantic_model(self, mock_deps):
        class Score(BaseModel):
            name: str
            value: float

        expected = Score(name="test", value=0.9)
        structured_model = AsyncMock()
        structured_model.ainvoke = AsyncMock(return_value=expected)
        mock_deps["model"].with_structured_output.return_value = structured_model

        result = await Agent("relationship-filter").extract(
            Score,
            messages=[HumanMessage(content="rate this")],
            prompt_vars={"key": "val"},
        )

        assert isinstance(result, Score)
        assert result.name == "test"
        mock_deps["model"].with_structured_output.assert_called_once_with(Score)

    async def test_extract_retries_on_transient_error(self, mock_deps):
        class Result(BaseModel):
            ok: bool

        structured_model = AsyncMock()
        structured_model.ainvoke = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                Result(ok=True),
            ]
        )
        mock_deps["model"].with_structured_output.return_value = structured_model

        result = await Agent("relationship-filter").extract(
            Result,
            messages=[HumanMessage(content="test")],
            max_retries=2,
        )
        assert result.ok is True
        assert structured_model.ainvoke.call_count == 2

    async def test_extract_passes_model_kwargs(self, mock_deps):
        class Out(BaseModel):
            v: str

        structured_model = AsyncMock()
        structured_model.ainvoke = AsyncMock(return_value=Out(v="ok"))
        mock_deps["model"].with_structured_output.return_value = structured_model

        await Agent(
            "relationship-filter", model_kwargs={"reasoning_effort": "low"}
        ).extract(
            Out,
            messages=[],
        )

        mock_deps["build_chat_model"].assert_called_once_with(
            "relationship-model", reasoning_effort="low"
        )


# ---------------------------------------------------------------------------
# Agentic path: run() with tools
# ---------------------------------------------------------------------------


class TestRunAgentic:
    """Agent.run() with tools (LangGraph agent path)."""

    async def test_delegates_to_langgraph_agent(self, mock_deps):
        fake_agent = AsyncMock()
        fake_agent.ainvoke = AsyncMock(
            return_value={"messages": [AIMessage(content="tool result")]}
        )

        mock_prompt_obj = MagicMock()
        mock_prompt_obj.get_langchain_prompt.return_value = "sys prompt"
        mock_deps["get_prompt"].return_value = mock_prompt_obj

        with patch("app.agent.core.create_agent", return_value=fake_agent):
            result = await Agent("main", tools=["tool1"]).run(
                messages=[HumanMessage(content="do something")],
                prompt_vars={"persona": "test"},
            )

        assert result.content == "tool result"
        fake_agent.ainvoke.assert_called_once()

    async def test_agentic_retries_on_transient_error(self, mock_deps):
        fake_agent = AsyncMock()
        fake_agent.ainvoke = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                {"messages": [AIMessage(content="ok")]},
            ]
        )

        mock_prompt_obj = MagicMock()
        mock_prompt_obj.get_langchain_prompt.return_value = "sys"
        mock_deps["get_prompt"].return_value = mock_prompt_obj

        with patch("app.agent.core.create_agent", return_value=fake_agent):
            result = await Agent("main", tools=["t"]).run(
                messages=[HumanMessage(content="hi")],
                max_retries=2,
            )

        assert result.content == "ok"
        assert fake_agent.ainvoke.call_count == 2


# ---------------------------------------------------------------------------
# Agentic path: stream() with tools
# ---------------------------------------------------------------------------


class TestStreamAgentic:
    """Agent.stream() with tools."""

    async def test_yields_tokens(self, mock_deps):
        chunks = [
            (AIMessageChunk(content="he"), None),
            (AIMessageChunk(content="llo"), None),
        ]

        fake_agent = AsyncMock()

        async def fake_astream(inp, *, context=None, stream_mode=None, config=None):
            for c in chunks:
                yield c

        fake_agent.astream = fake_astream

        mock_prompt_obj = MagicMock()
        mock_prompt_obj.get_langchain_prompt.return_value = "sys"
        mock_deps["get_prompt"].return_value = mock_prompt_obj

        with patch("app.agent.core.create_agent", return_value=fake_agent):
            collected = []
            async for token in Agent("main", tools=["t"]).stream(
                messages=[HumanMessage(content="hi")],
            ):
                collected.append(token)

        assert len(collected) == 2
        assert collected[0].content == "he"

    async def test_no_retry_after_tokens_yielded(self, mock_deps):
        """Once tokens have been yielded, retry would cause duplicates."""

        fake_agent = AsyncMock()
        call_count = 0

        async def failing_astream(inp, *, context=None, stream_mode=None, config=None):
            nonlocal call_count
            call_count += 1
            yield AIMessageChunk(content="partial"), None
            raise APITimeoutError(request=MagicMock())

        fake_agent.astream = failing_astream

        mock_prompt_obj = MagicMock()
        mock_prompt_obj.get_langchain_prompt.return_value = "sys"
        mock_deps["get_prompt"].return_value = mock_prompt_obj

        with patch("app.agent.core.create_agent", return_value=fake_agent):
            with pytest.raises(APITimeoutError):
                async for _ in Agent("main", tools=["t"]).stream(
                    messages=[HumanMessage(content="hi")],
                    max_retries=3,
                ):
                    pass

        # Should NOT retry — only 1 attempt because tokens were already yielded
        assert call_count == 1
