"""两个 App 的参与者的一轮共用的历史文字（:class:`~app.agent.contracts.HistoryTexts`）：两种标记
消息的表头和说明、裁掉的材料和图片换成的那句话、没来得及执行的工具调用得到的回答。她的一轮和
world 主 agent 的一轮用的是同一套，所以定义一次，两份清单都引用这里；每一句话本身在它唯一的
定义处（:mod:`app.agent.continuity`、:mod:`app.agent.core`）。
"""

from __future__ import annotations

from app.agent.continuity import (
    CHECKPOINT_HEAD,
    CHECKPOINT_NOTE,
    GAP_HEAD,
    GAP_NOTE,
    MATERIAL_TRIMMED,
    PICTURE_TRIMMED,
)
from app.agent.contracts import HistoryTexts
from app.agent.core import NEVER_RAN

HISTORY_TEXTS = HistoryTexts(
    checkpoint_head=CHECKPOINT_HEAD,
    checkpoint_note=CHECKPOINT_NOTE,
    gap_head=GAP_HEAD,
    gap_note=GAP_NOTE,
    material_trimmed=MATERIAL_TRIMMED,
    picture_trimmed=PICTURE_TRIMMED,
    never_ran=NEVER_RAN,
)
