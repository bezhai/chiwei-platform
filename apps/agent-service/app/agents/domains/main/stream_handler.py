"""Stream output processing

处理 agent.stream() 产出的 token 流：
- AIMessageChunk: 文本 token / finish_reason / tool_call 边界
- ToolMessage: 工具调用结果
- SPLIT_MARKER: 分段标记（consumer 侧检测并拆分为多条消息）
"""

import logging
from dataclasses import dataclass, field

from langchain.messages import AIMessageChunk, ToolMessage

logger = logging.getLogger(__name__)

# 分段标记（consumer 侧检测并拆分为多条消息）
SPLIT_MARKER = "---split---"


@dataclass
class StreamState:
    """流处理状态跟踪"""

    full_content: str = ""
    agent_token_count: int = 0
    tool_call_count: int = 0
    _has_text_in_current_turn: bool = field(default=False, repr=False)


def handle_token(
    token: object,
    state: StreamState,
) -> list[str | None]:
    """处理单个 stream token，返回需要 yield 的文本列表。

    返回值含义：
    - 空列表 []: 无需 yield（ToolMessage / 无内容的 chunk）
    - [text, ...]: 按顺序 yield 每个非 None 元素
    - [None]: 特殊信号 — content_filter，调用方应 yield 错误消息并终止
    - 包含 "(后续内容被截断)" 的列表: length 截断，调用方应 yield 后终止

    注意：content_filter 返回 [None]，调用方需自行获取 bot_ctx 的错误消息。
    """
    if isinstance(token, AIMessageChunk):
        finish_reason = token.response_metadata.get("finish_reason")

        if finish_reason == "content_filter":
            return [None]  # 信号：调用方 yield error message 并 return
        if finish_reason == "length":
            return ["(后续内容被截断)"]

        result: list[str | None] = []

        if token.text:
            state._has_text_in_current_turn = True
            state.agent_token_count += 1
            state.full_content += token.text
            result.append(token.text)

        # text -> tool call 边界，注入分隔符
        if token.tool_call_chunks and state._has_text_in_current_turn:
            result.append(SPLIT_MARKER)
            state._has_text_in_current_turn = False

        return result

    if isinstance(token, ToolMessage):
        state.tool_call_count += 1
        state._has_text_in_current_turn = False

    return []


def is_content_filter(result: list[str | None]) -> bool:
    """检查 handle_token 返回值是否为 content_filter 信号"""
    return len(result) == 1 and result[0] is None


def is_length_truncated(result: list[str | None]) -> bool:
    """检查 handle_token 返回值是否为 length 截断"""
    return len(result) == 1 and result[0] == "(后续内容被截断)"
