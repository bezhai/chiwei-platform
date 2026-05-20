"""Phase 5a chat 主 pipeline wiring。

  Source.mq("chat_request")
       ─[wire ChatTrigger, in-process]─→  route_chat_node
                                              │
                                              ↓ N × emit(ChatRequest)
       ─[wire ChatRequest, .durable()]─→  chat_node
                                              │
                                              ↓ N × emit(ChatResponseSegment)
       ─[wire ChatResponseSegment, in-process]─→  Sink.mq("chat_response")
                                              ↓
                                  channel-server / chat-response-worker → 飞书

ChatTrigger 用 transient=True，幂等去重在 ChatRequest 上由 (message_id,
persona_id) 联合 Key 完成；ChatRequest 持久化所以走 ``.durable()``，
ChatResponseSegment transient=True 又是 sink 出 graph，不需要 durable。
所有 @node 跑在 agent-service 主进程（默认 app），因此不需要 bind。
"""
from app.domain.chat_dataflow import ChatRequest, ChatResponseSegment, ChatTrigger
from app.domain.chat_events import ConversationMessageContentSynced
from app.nodes.chat_node import chat_node, route_chat_node
from app.nodes.persist_tos_files import persist_tos_files_node
from app.runtime import Sink, Source, wire

wire(ChatTrigger).from_(Source.mq("chat_request")).to(route_chat_node)
wire(ChatRequest).to(chat_node).durable()
wire(ChatResponseSegment).to(Sink.mq("chat_response"))

# Phase 6 v4 Gap 5: build_chat_context emits ConversationMessageContentSynced
# instead of fire-and-forget asyncio.create_task. Durable so the DB write
# runs out of band of the chat stream while still landing in the
# agent-service main process (matching the old asyncio.create_task placement).
wire(ConversationMessageContentSynced).to(persist_tos_files_node).durable()
