"""Pre Graph 定义

前置处理链路，包含：
1. 安全检测（并行）
2. 复杂度分类（与安全检测并行）
"""

from functools import lru_cache
from typing import Literal

from langfuse.langchain import CallbackHandler
from langgraph.graph import END, START, StateGraph

from app.agents.graphs.pre.nodes import (
    aggregate_results,
    check_banned_word_node,
    check_prompt_injection,
    check_sensitive_politics,
    classify_complexity,
)
from app.agents.graphs.pre.state import PreState


def route_after_aggregate(state: PreState) -> Literal["reject", "pass"]:
    """根据聚合结果决定路由"""
    if state["is_blocked"]:
        return "reject"
    return "pass"


def _create_pre_graph() -> StateGraph:
    """创建 Pre 处理图

    图结构：
                        ┌─────────────────┐
                        │     START       │
                        └────────┬────────┘
                                 │
            ┌────────────────────┼────────────────────┐
            │                    │                    │
            ▼                    ▼                    ▼
    ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
    │ safety_checks │   │  classify     │   │  (可扩展...)  │
    │ ├─banned_word │   │  complexity   │   │               │
    │ ├─injection   │   │  (轻量LLM)    │   │               │
    │ └─politics    │   │               │   │               │
    └───────┬───────┘   └───────┬───────┘   └───────┬───────┘
            │                    │                    │
            └────────────────────┼────────────────────┘
                                 │
                                 ▼
                        ┌─────────────────┐
                        │   aggregate     │
                        └────────┬────────┘
                                 │
                  ┌──────────────┴──────────────┐
                  │                             │
                  ▼                             ▼
             [reject]                       [pass]
                  │                             │
                  ▼                             ▼
                 END                           END
    """
    builder = StateGraph(PreState)

    # 安全检测节点（并行）
    builder.add_node("check_banned_word", check_banned_word_node)
    builder.add_node("check_prompt_injection", check_prompt_injection)
    builder.add_node("check_sensitive_politics", check_sensitive_politics)

    # 复杂度分类节点（与安全检测并行）
    builder.add_node("classify_complexity", classify_complexity)

    # 聚合节点
    builder.add_node("aggregate", aggregate_results)

    # 从 START 并行分发
    builder.add_edge(START, "check_banned_word")
    builder.add_edge(START, "check_prompt_injection")
    builder.add_edge(START, "check_sensitive_politics")
    builder.add_edge(START, "classify_complexity")

    # 汇聚到 aggregate
    builder.add_edge("check_banned_word", "aggregate")
    builder.add_edge("check_prompt_injection", "aggregate")
    builder.add_edge("check_sensitive_politics", "aggregate")
    builder.add_edge("classify_complexity", "aggregate")

    # 条件路由
    builder.add_conditional_edges(
        "aggregate",
        route_after_aggregate,
        {"reject": END, "pass": END},
    )

    return builder


@lru_cache(maxsize=1)
def get_pre_graph():
    """获取编译后的 Pre 图（单例）"""
    return _create_pre_graph().compile()


async def run_pre(message_content: str) -> PreState:
    """运行 Pre 处理图

    Args:
        message_content: 待处理的消息内容

    Returns:
        PreState: 包含安全检测结果和复杂度分类结果
    """
    graph = get_pre_graph()

    config = {
        "callbacks": [CallbackHandler()],
        "run_name": "pre",
    }

    initial_state: PreState = {
        "message_content": message_content,
        "safety_results": [],
        "complexity_result": None,
        "is_blocked": False,
        "block_reason": None,
    }

    result = await graph.ainvoke(initial_state, config=config)
    return result
