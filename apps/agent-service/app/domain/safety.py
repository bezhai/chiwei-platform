"""Safety Data — Phase 2 dataflow types.

PreSafetyRequest / PreSafetyVerdict 是请求路径内的瞬时控制面数据（transient）；
PostSafetyRequest 用 adoption mode adopt 已有的 ``agent_responses`` 表（lark-server
那边 INSERT 的）；Recall 是出 graph 给 lark-server recall-worker 的事件。
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
    """Post-safety check 请求；adoption mode adopt agent_responses 表。

    Row 由 lark-server 在 chat 完成时已 INSERT；agent-service 仅作为
    durable wire 的入口 trigger。session_id 是 agent_responses 的
    unique business key（无 dedup_hash 列），所以业务幂等通过节点入口
    查 safety_status 短路实现。
    """
    session_id: Annotated[str, Key]
    trigger_message_id: str
    chat_id: str
    response_text: str

    class Meta:
        existing_table = "agent_responses"
        dedup_column = "session_id"


class Recall(Data):
    """撤回事件，通过 Sink.mq("recall") 出 graph 给 lark-server recall-worker。

    payload schema 与旧 ``mq.publish(RECALL, ...)`` 一致；lane 字段
    被 recall-worker.ts 从 payload 直接读取，必须显式带。
    """
    session_id: Annotated[str, Key]
    chat_id: str
    trigger_message_id: str
    reason: str
    detail: str | None = None
    lane: str | None = None

    class Meta:
        transient = True
