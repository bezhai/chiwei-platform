"""Memory recall tool — v4 Qdrant semantic + graph traversal.

FTS path is deprecated and removed. See ``app/memory/recall_engine.py`` for the
pure function; this file only wires it into the agent tool system.
"""

from __future__ import annotations

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.memory.recall_engine import run_recall


async def _recall_impl(
    *,
    persona_id: str,
    queries: list[str],
) -> dict:
    result = await run_recall(
        persona_id=persona_id,
        queries=queries,
    )
    return {"abstracts": result.abstracts, "facts": result.facts}


@tool
@tool_error("想不起来了...")
async def recall(queries: list[str]) -> dict:
    """回忆过去。传一个或多个关键词/描述，按语义在记忆里搜。

    每个 query 都是一次独立搜索；批量传可以一次查多条线索。
    返回你记得的抽象认识 + 每条认识下具体的事实支撑。

    例子：
      recall(queries=["浩南最近怎么了"])
      recall(queries=["学习 Rust", "他答应过我什么"])

    Args:
        queries: 自然语言查询列表（批量）
    """
    context = get_runtime(AgentContext).context
    return await _recall_impl(
        persona_id=context.persona_id,
        queries=queries,
    )
