"""她开口那一侧的 Data 契约。

ChatResponseSegment: 她说的每一段，经 sink.mq(chat_response) 出 graph。

**这个模块里没有入站契约。** 原先还有 ChatTrigger（消息队列入口的原始 body）和
ChatRequest（fan-out 之后 per-persona 的请求），它们随那条队列一起删了：她不从队列
拿消息 —— 每一缝直接查 ``common_message``、自己决定要不要开口（见 ``app.living``）。
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key

# 主动发（life send_message → 真人飞书私聊）出站段 message_id 的命名空间前缀。
# 主动发没有来源消息，message_id 不能是指向真实来源消息的 id（worker 会反查炸）。
# 用这个前缀派生一个本地键，标记「这是主动发、worker 别反查来源消息」；worker 的
# is_proactive 分支据 is_proactive 走不反查的路径，这个前缀只是让 message_id 在语义
# 上明确「非来源消息 id」。单一定义处（宪法「禁止重复定义」），write 端（life_tools
# 派生）与读端（本模块 Data 契约文档 / 测试）都从这里取。
#
# 它同时是一个**跨语言线格式**：出站投递方 lark-service（TS）剥掉这个前缀取出 uuid，
# 落进 common_message.agent_outbound_id。跨语言没法共享一个运行时定义（两个镜像都不
# COPY contracts/），所以线格式落在一份两侧测试共读的向量上：
# contracts/proactive-message-id.json。改这个字面量而不改那份向量，
# tests/domain/test_proactive_message_id_contract.py 立刻转红 —— 没有这道闸的话，
# 只改一边的症状是投递方静默认不出主动消息、那次开口在库里永久失联，全程零报错。
PROACTIVE_MESSAGE_ID_PREFIX = "proactive:"


class ChatResponseSegment(Data):
    """她说的每一段，经 sink.mq(chat_response) 出 graph。

    (message_id, persona_id, part_index) 联合 Key 用于段内去重；
    lane 必须显式带在 body —— sink dispatch 拿它当 ``outbound_context`` 的
    fallback，最终写进 AMQP header，而消费侧（chat_response_{channel} 的订阅方）
    只认 header。transient=True：段是事件流，不落 agent-service 自己的表。

    两类来源对 ``message_id`` 的契约不同：

      * **回话**（她读到一条消息之后接着说）：``message_id`` 是触发这次回复的
        真实来源 ``common_message_id``，worker 据它反查渠道裸消息地址做 reply。
      * **主动发**（life ``send_message`` 给真人飞书私聊，``is_proactive=True``）：
        **没有来源消息**，所以 ``message_id`` **绝不是**指向任何真实来源消息的 id ——
        它是带 ``proactive:`` 命名空间前缀的本地派生键（:data:`PROACTIVE_MESSAGE_ID_PREFIX`，
        从发送者本轮 act_id + 序号派生、整轮重投稳定），``root_id`` 留空。worker 的
        主动发分支据 ``is_proactive`` **不反查来源消息**、直接用 ``chat_id``
        （= 真实 p2p ``common_conversation_id``）+ ``bot_name`` 投递（不靠伪 id，
        见 chat-response-worker 的 is_proactive 出站路径 / task 4）。

    ``channel`` 必填：sink dispatch 按它现算 routing key（``chat_response_{channel}``）。
    ``channel_route_for_payload`` 已经对缺字段 fail-closed，但那道校验跑在 pydantic
    填完默认值之后 —— 有默认值时它永远看到 "lark"，拦不住错投，只有把默认值去掉
    才真的生效（同 :class:`app.domain.safety.Recall`）。
    """
    channel: str
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
