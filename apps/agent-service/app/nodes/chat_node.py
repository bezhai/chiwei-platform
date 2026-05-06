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

# MessageRouter / emit imported at module level so route_chat_node 测试可
# 用 monkeypatch.setattr(chat_node_mod, ...) 替换，且 Task 6 落地后 router
# fan-out 直接复用同名引用。
from app.chat.router import MessageRouter  # noqa: F401
from app.data.queries import is_chat_request_completed
from app.data.session import get_session
from app.domain.chat_dataflow import ChatRequest, ChatTrigger
from app.runtime import node
from app.runtime.emit import emit  # noqa: F401

logger = logging.getLogger(__name__)


@node
async def route_chat_node(t: ChatTrigger) -> None:
    """ChatTrigger -> ChatRequest fan-out。

    步骤：
      0. 入口校验 message_id 非空
      1. redelivered 短路（is_chat_request_completed helper）
      2. MessageRouter.route 决定 persona 列表（Task 6）
      3. fan-out emit ChatRequest（Task 6）
    """
    if t.message_id is None:
        raise ValueError(
            "ChatTrigger.message_id is None; cannot fan out ChatRequest"
        )

    async with get_session() as s:
        already_done = await is_chat_request_completed(
            s, t.session_id, is_proactive=t.is_proactive
        )
    if already_done:
        logger.info(
            "skip redelivered chat_request: session_id=%s, message_id=%s",
            t.session_id,
            t.message_id,
        )
        return

    # Task 6: router + fan-out


@node
async def chat_node(req: ChatRequest) -> None:
    """ChatRequest -> ChatResponseSegment generation (占位)。"""
    raise NotImplementedError("chat_node body added in later tasks")
