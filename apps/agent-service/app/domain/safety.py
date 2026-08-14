"""Safety Data — Phase 2 dataflow types.

PreSafetyRequest / PreSafetyVerdict 是请求路径内的瞬时控制面数据（transient）；
PostSafetyRequest is a transient graph trigger. Durable state is stored in
``common_agent_response`` by the data query layer.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class PreSafetyRequest(Data):
    """Pre-safety check 请求（chat pipeline 内部触发）。

    pre_request_id 每次 pre-check 独立 uuid4，避免并发 / DLQ replay 时
    waiter Future 互相覆盖。跟 session_id 完全解耦。
    """
    pre_request_id: Annotated[str, Key]
    message_id: str
    message_content: str
    persona_id: str

    class Meta:
        transient = True


class PreSafetyVerdict(Data):
    """Pre-safety check 结果，由 run_pre_safety @node 产出。"""
    pre_request_id: Annotated[str, Key]
    message_id: str
    is_blocked: bool
    block_reason: str | None = None  # BlockReason.value 字符串化
    detail: str | None = None

    class Meta:
        transient = True


class PostSafetyRequest(Data):
    """Post-safety check request keyed by response session_id.

    ``channel`` 必填。它一路传给 ``Recall``，最终决定撤回投哪个队列；
    默认值会让缺 channel 的请求静默变成飞书，而 sink dispatch 的
    fail-closed 校验在 pydantic 填完默认值之后才跑，拦不住。
    """
    session_id: Annotated[str, Key]
    trigger_message_id: str
    chat_id: str
    response_text: str
    channel: str

    class Meta:
        transient = True


class Recall(Data):
    """撤回事件，通过 Sink.mq("recall") 出 graph 给拥有该 channel 的渠道服务。

    实际投的是 ``recall_{channel}``（sink dispatch 按 ``channel`` 现算 rk），
    飞书那条由 lark-service 消费。payload schema 与旧 ``mq.publish(RECALL, ...)``
    一致；``lane`` 必须显式带 —— sink dispatch 拿它当 ``outbound_context`` 的
    fallback，最终写进 AMQP header，而消费侧只认 header。

    ``channel`` 必填：它直接决定 routing key，猜错就把撤回投到别的渠道去了。
    """
    session_id: Annotated[str, Key]
    chat_id: str
    trigger_message_id: str
    reason: str
    channel: str
    detail: str | None = None
    lane: str | None = None

    class Meta:
        transient = True
