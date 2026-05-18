"""Phase 5a chat pipeline Data 类。

ChatTrigger:        mq(chat_request) 入口的原始 body（channel-server publish）。
ChatRequest:        route_chat_node fan-out 后 per-persona 的请求。
ChatResponseSegment: chat_node 输出的段，最终 publish 到 mq(chat_response)。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.runtime import Data, Key


class ChatTrigger(Data):
    """mq(chat_request) 入口原始 body。

    transient=True：source.mq 不做 insert_idempotent，business 幂等由
    route_chat_node 进入 graph 后的 (message_id, persona_id) 联合 Key
    在 ChatRequest 上完成。message_id 设 Optional 以容忍 channel-server
    偶发缺字段的 payload 反序列化失败。
    """
    # 来源 channel。agent-service 对它无感知、只透传；默认 "lark" 保证停机
    # 迁移时 MQ/outbox 残留的旧 payload（不带 channel）反序列化不炸。
    channel: str = "lark"
    message_id: Annotated[str | None, Key] = None
    session_id: str | None = None
    chat_id: str | None = None
    is_p2p: bool = False
    root_id: str | None = None
    user_id: str | None = None
    lane: str | None = None
    is_proactive: bool = False
    bot_name: str | None = None
    mentions: list[str] = Field(default_factory=list)
    enqueued_at: int | None = None

    class Meta:
        transient = True


class ChatRequest(Data):
    """route_chat_node fan-out 后 per-persona 的请求。

    transient=False（默认）：runtime 自动建 ``data_chat_request`` 表，
    (message_id, persona_id) 联合 Key 提供 in-graph durable redelivery
    去重。
    """
    channel: str = "lark"
    message_id: Annotated[str, Key] = ""
    persona_id: Annotated[str, Key] = ""
    session_id: str | None = None
    chat_id: str | None = None
    is_p2p: bool = False
    root_id: str | None = None
    user_id: str | None = None
    is_proactive: bool = False
    bot_name: str | None = None
    lane: str | None = None
    enqueued_at: int | None = None

    class Meta:
        # transient 显式不设 —— runtime 默认 transient=False，自动建
        # ``data_chat_request`` 表用于 in-graph durable redelivery 去重。
        # 留空 Meta 让 ``getattr(Meta, "transient", False)`` 拿到 False。
        pass


class ChatResponseSegment(Data):
    """chat_node 产出的回复段，经 sink.mq(chat_response) 出 graph。

    (message_id, persona_id, part_index) 联合 Key 用于段内去重；
    lane 必须显式带在 body —— sink dispatch 不注入 header lane，
    chat-response-worker 直接读 payload.lane 路由飞书回复。
    transient=True：段是事件流，不落 agent-service 自己的表。
    """
    channel: str = "lark"
    message_id: Annotated[str, Key] = ""
    persona_id: Annotated[str, Key] = ""
    part_index: Annotated[int, Key] = 0
    session_id: str | None = None
    chat_id: str | None = None
    is_p2p: bool = False
    root_id: str | None = None
    user_id: str | None = None
    is_proactive: bool = False
    bot_name: str | None = None
    lane: str | None = None
    content: str = ""
    status: str = "success"
    is_last: bool = False
    full_content: str | None = None
    published_at: int | None = None

    class Meta:
        transient = True
