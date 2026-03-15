"""子 Agent 基类

将子 Agent 包装为可调用的形式，统一处理：
- 工具集默认继承主 Agent（排除委派工具）
- AgentContext 透传
- Langfuse trace 继承
"""

import logging

from langchain.messages import AIMessage, HumanMessage

from app.agents.core.agent import ChatAgent
from app.agents.core.config import AgentRegistry
from app.agents.core.context import AgentContext

logger = logging.getLogger(__name__)


class SubAgent:
    """子 Agent 基类

    子 Agent 使用 ChatAgent.run()（非流式）执行任务，
    结果作为 ToolMessage 返回给主 Agent。

    工具集默认与主 Agent 一致（排除委派工具），也可显式指定。
    """

    def __init__(self, config_name: str, tools: list | None = None):
        self.config_name = config_name
        self._tools = tools

    def _resolve_tools(self) -> list:
        """解析工具集：显式指定则用之，否则取 BASE_TOOLS"""
        if self._tools is not None:
            return self._tools
        from app.agents.domains.main.tools import BASE_TOOLS

        return list(BASE_TOOLS)

    async def run(
        self,
        task: str,
        context: AgentContext,
        prompt_vars: dict | None = None,
    ) -> str:
        """执行子 Agent 任务

        Args:
            task: 任务描述（作为 HumanMessage 传入）
            context: 主 Agent 透传的执行上下文
            prompt_vars: 额外的 prompt 模板变量

        Returns:
            子 Agent 的文本输出
        """
        config = AgentRegistry.get(self.config_name)
        tools = self._resolve_tools()

        agent = ChatAgent(
            prompt_id=config.prompt_id,
            tools=tools,
            model_id=config.model_id,
            trace_name=config.trace_name,
        )

        messages = [HumanMessage(content=task)]
        result: AIMessage = await agent.run(
            messages,
            context=context,
            prompt_vars=prompt_vars or {},
        )
        return result.content or ""
