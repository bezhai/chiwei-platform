"""Phase 5a chat 主 pipeline 节点（占位骨架）。

  route_chat_node:  ChatTrigger -> N × emit(ChatRequest)
  chat_node:        ChatRequest -> N × emit(ChatResponseSegment)

骨架阶段两个 @node 的 body 都是 ``raise NotImplementedError``；
真正的逻辑（消息校验 / fan-out / prep / 主流 / pre-safety 短路 / final）
由 Phase 5a Task 4-11 逐步填入。占位的目的是让 wiring/chat.py + compile_graph
先建立可验证的拓扑骨架。
"""
from __future__ import annotations

import logging

from app.domain.chat_dataflow import ChatRequest, ChatTrigger
from app.runtime import node

logger = logging.getLogger(__name__)


@node
async def route_chat_node(t: ChatTrigger) -> None:
    """ChatTrigger -> ChatRequest fan-out (占位)。"""
    raise NotImplementedError("route_chat_node body added in later tasks")


@node
async def chat_node(req: ChatRequest) -> None:
    """ChatRequest -> ChatResponseSegment generation (占位)。"""
    raise NotImplementedError("chat_node body added in later tasks")
