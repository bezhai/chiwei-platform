"""MessageRequest: MQ 入口的请求体 Data。

``Source.mq("vectorize")`` 消费老队列 body ``{"message_id": X}``,engine
层 decode 成 ``MessageRequest(message_id=X)`` 交给 ``hydrate_message`` @node。
Transient — 不落 pg，仅作为 MQ 帧 -> Message 之间的一跳。
"""
from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


class MessageRequest(Data):
    message_id: Annotated[str, Key]

    class Meta:
        transient = True
