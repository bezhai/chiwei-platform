"""Data types for memory pipeline debounce triggers (drift / afterthought).

Both are ``Meta.transient = True`` — fire signals are not persisted to pg.
The runtime keeps state in mq (delay messages) + redis (latest trigger_id),
not in a table.
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key


class DriftTrigger(Data):
    """Emitted by chat post_actions when an assistant reply lands.

    drift_check (debounced) reads the recent persona reply window from db
    and decides whether to regenerate base reply_style.
    """

    chat_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True


class AfterthoughtTrigger(Data):
    """Emitted by chat post_actions when a qualifying message lands.

    afterthought_check (debounced 300s / max_buffer 15) summarises the
    recent chat history into a v4 conversation Fragment.
    """

    chat_id: Annotated[str, Key]
    persona_id: str

    class Meta:
        transient = True
