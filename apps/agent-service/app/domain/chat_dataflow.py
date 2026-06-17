"""Phase 5a chat pipeline Data 类。

ChatTrigger:        mq(chat_request) 入口的原始 body（channel-server publish）。
ChatRequest:        route_chat_node fan-out 后 per-persona 的请求。
ChatResponseSegment: chat_node 输出的段，最终 publish 到 mq(chat_response)。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.runtime import Data, Key

# 主动发（life send_message → 真人飞书私聊）出站段 message_id 的命名空间前缀。
# 主动发没有来源消息，message_id 不能是指向真实来源消息的 id（worker 会反查炸）。
# 用这个前缀派生一个本地键，标记「这是主动发、worker 别反查来源消息」；worker 的
# is_proactive 分支据 is_proactive 走不反查的路径，这个前缀只是让 message_id 在语义
# 上明确「非来源消息 id」。单一定义处（宪法「禁止重复定义」），write 端（life_tools
# 派生）与读端（本模块 Data 契约文档 / 测试）都从这里取。
PROACTIVE_MESSAGE_ID_PREFIX = "proactive:"


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
    persona_ids: list[str] = Field(default_factory=list)
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

    两类来源对 ``message_id`` 的契约不同：

      * **被动回复**（chat_node 回飞书来的消息）：``message_id`` 是触发这次回复的
        真实来源 ``common_message_id``，worker 据它反查渠道裸消息地址做 reply。
      * **主动发**（life ``send_message`` 给真人飞书私聊，``is_proactive=True``）：
        **没有来源消息**，所以 ``message_id`` **绝不是**指向任何真实来源消息的 id ——
        它是带 ``proactive:`` 命名空间前缀的本地派生键（:data:`PROACTIVE_MESSAGE_ID_PREFIX`，
        从发送者本轮 act_id + 序号派生、整轮重投稳定），``root_id`` 留空。worker 的
        主动发分支据 ``is_proactive`` **不反查来源消息**、直接用 ``chat_id``
        （= 真实 p2p ``common_conversation_id``）+ ``bot_name`` 投递（不靠伪 id，
        见 chat-response-worker 的 is_proactive 出站路径 / task 4）。
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
