"""Data types for memory pipeline debounce triggers (afterthought).

``Meta.transient = True`` — fire signals are not persisted to pg.
The runtime keeps state in mq (delay messages) + redis (latest trigger_id),
not in a table.

DriftTrigger（voice 再生成的触发信号）随 voice 子系统拆除删除。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


class AfterthoughtTrigger(Data):
    """Emitted by chat post_actions when a qualifying message lands.

    afterthought_check (debounced 300s / max_buffer 15) summarises the
    recent chat history into a v4 conversation Fragment.
    """

    chat_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True
