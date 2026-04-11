"""test_sub_agent.py — SubAgent 基类测试"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.core.config import AgentConfig, AgentRegistry
from app.agents.core.context import AgentContext, MessageContext
from app.agents.core.sub_agent import SubAgent

pytestmark = pytest.mark.unit


class TestSubAgentResolveTools:
    """工具集解析逻辑"""

    def test_explicit_tools_returned_as_is(self):
        explicit = [MagicMock(), MagicMock()]
        agent = SubAgent("research", tools=explicit)
        assert agent._resolve_tools() is explicit

    def test_none_tools_resolves_to_base_tools(self):
        fake_base = [MagicMock(name="search_web"), MagicMock(name="load_memory")]
        agent = SubAgent("research", tools=None)
        with patch(
            "app.agents.core.sub_agent.BASE_TOOLS",
            fake_base,
            create=True,
        ):
            pass
        with patch(
            "app.agents.domains.main.tools.BASE_TOOLS",
            fake_base,
        ):
            result = agent._resolve_tools()
        assert len(result) == len(fake_base)
        assert result is not fake_base  # 返回新列表

    def test_default_tools_is_none(self):
        agent = SubAgent("research")
        assert agent._tools is None


class TestSubAgentRun:
    """run() 调用逻辑"""

    @pytest.fixture()
    def context(self):
        return AgentContext(
            message=MessageContext(message_id="msg_1", chat_id="chat_1"),
        )

    @pytest.fixture(autouse=True)
    def _register_research(self):
        if not AgentRegistry.has("research"):
            AgentRegistry.register(
                "research",
                AgentConfig(
                    prompt_id="research_agent",
                    model_id="research-model",
                    trace_name="research",
                ),
            )

    @pytest.mark.asyncio
    async def test_run_delegates_to_chat_agent(self, context):
        mock_ai_message = MagicMock()
        mock_ai_message.content = "调研报告内容"

        mock_chat_agent = MagicMock()
        mock_chat_agent.run = AsyncMock(return_value=mock_ai_message)

        agent = SubAgent("research", tools=[MagicMock()])

        with patch(
            "app.agents.core.sub_agent.ChatAgent",
            return_value=mock_chat_agent,
        ) as MockChatAgent:
            result = await agent.run(task="研究葬送的芙莉莲", context=context)

        assert result == "调研报告内容"
        MockChatAgent.assert_called_once_with(
            prompt_id="research_agent",
            tools=agent._resolve_tools(),
            model_id="research-model",
            trace_name="research",
        )
        mock_chat_agent.run.assert_called_once()
        call_kwargs = mock_chat_agent.run.call_args
        assert call_kwargs.kwargs["context"] is context

    @pytest.mark.asyncio
    async def test_run_returns_empty_string_on_none_content(self, context):
        mock_ai_message = MagicMock()
        mock_ai_message.content = None

        mock_chat_agent = MagicMock()
        mock_chat_agent.run = AsyncMock(return_value=mock_ai_message)

        agent = SubAgent("research", tools=[MagicMock()])

        with patch(
            "app.agents.core.sub_agent.ChatAgent",
            return_value=mock_chat_agent,
        ):
            result = await agent.run(task="test", context=context)

        assert result == ""
