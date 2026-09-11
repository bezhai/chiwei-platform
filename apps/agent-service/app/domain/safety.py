"""Safety Data — Phase 2 dataflow types.

PreSafetyRequest / PreSafetyVerdict 是请求路径内的瞬时控制面数据（transient）；
PostSafetyRequest is a transient graph trigger. Durable state is stored in
``common_agent_response`` by the data query layer.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import model_validator

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
    飞书那条由 lark-service 消费。``lane`` 必须显式带 —— sink dispatch 拿它当
    ``outbound_context`` 的 fallback，最终写进 AMQP header，而消费侧只认 header。

    ``channel`` 必填：它直接决定 routing key，猜错就把撤回投到别的渠道去了。

    **两种定位方式，恰好用一种**（共享向量 ``contracts/recall-locators.json``）：

      * ``session_id`` —— 真人问她、她答的那条链。投递侧按它查台账拿到那次会话落下
        的全部回复，逐条撤。
      * ``outbound_id`` —— 她自己开口那条链。她没有会话，只有一次开口；投递侧按它
        等值反查公共层那条消息行，**不碰台账** —— 主动消息在台账上一行都没有
        （投递方对那张表只 UPDATE 不 INSERT，而主动消息没有会话标识，一路被守卫
        跳过）。

    所以 ``session_id`` 和 ``trigger_message_id`` 都必须可空：她主动开口既没有会话，
    也没有触发它的那条来源消息。**硬填一个假值的后果是静默的** —— 投递侧拿它去查
    台账、查不到、退避重投三次、写一行影响 0 行的失败、进死信，一个渠道接口都不会
    调，消息安安静静留在群里，全程不抛一个异常。所以"恰好一个"这条在构造时就判，
    不留到队列上。
    """
    session_id: Annotated[str | None, Key] = None
    outbound_id: str | None = None
    chat_id: str
    trigger_message_id: str | None = None
    reason: str
    channel: str
    detail: str | None = None
    lane: str | None = None

    @model_validator(mode="after")
    def _points_at_exactly_one_thing(self) -> Recall:
        locators = [self.session_id, self.outbound_id]
        given = [x for x in locators if x]
        if len(given) != 1:
            raise ValueError(
                "撤回请求要么给 session_id、要么给 outbound_id，恰好一个："
                f"session_id={self.session_id!r} outbound_id={self.outbound_id!r}"
            )
        return self

    class Meta:
        transient = True
