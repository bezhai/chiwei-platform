"""Deep research delegation tool — dispatches to a sub-agent."""

from __future__ import annotations

import logging

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error

logger = logging.getLogger(__name__)


@tool
@tool_error("深度调研失败")
async def deep_research(task: str) -> str:
    """深度调研工具。将复杂的研究任务委派给专门的调研 Agent。

    适用场景：
    - 需要多轮搜索、从多个来源收集信息并综合分析
    - 需要对比不同来源的信息，形成全面的认识
    - 需要深入了解某个作品、人物、事件的来龙去脉
    - 问题本身涉及多个维度，单次搜索无法覆盖

    不适用场景：
    - 简单的事实性问题（直接用 search_web）
    - 不需要搜索就能回答的问题

    Args:
        task: 调研任务的详细描述，应包含用户的具体问题和需要关注的方面
    """
    # Lazy import to avoid circular dependency
    from langchain_core.messages import HumanMessage

    from app.agent.core import Agent, AgentConfig
    from app.agent.tools.search import search_web

    _RESEARCH_CFG = AgentConfig("research_agent", "research-model", "research")
    context = get_runtime(AgentContext).context
    agent = Agent(_RESEARCH_CFG, tools=[search_web], update_trace=False)
    result = await agent.run(
        [HumanMessage(content=task)],
        context=context,
    )

    text = result.content if hasattr(result, "content") else str(result)
    logger.info("Research agent completed, result length: %d", len(text))
    return text
