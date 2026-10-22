"""两个 App 的参与者的一轮共用的历史文字（:class:`~app.agent.contracts.HistoryTexts`）：两种标记
消息的表头和说明、裁掉的材料和图片换成的那句话、没来得及执行的工具调用得到的回答。她的一轮和
world 主 agent 的一轮用的是同一套，所以定义一次，两份清单都引用这里。通用层
（:mod:`app.agent.continuity`、:mod:`app.agent.core`）只用这些话，不定任何一句。

标记消息是：表头（``{at}`` 换成那个时刻）、换行、说明、此刻的状态。
"""

from __future__ import annotations

from app.agent.contracts import HistoryTexts

HISTORY_TEXTS = HistoryTexts(
    # 固定时刻的清理：往前那一段真的不在了。
    checkpoint_head="【上下文清理 {at}】",
    checkpoint_note="再往前的那一段不在你眼前了，只剩你自己记下来的。你现在：\n\n",
    # 上一轮的上下文没存下来：往前那一段还在、中间少了一轮。文案和清理那种分开，缺口那次眼前的
    # 历史一条没少，套用"再往前的那一段不在你眼前了"就是往它眼前塞一句假话。
    gap_head="【上一轮没存下来 {at}】",
    gap_note=(
        "上一轮你做过说过的没能存下来，往上那一段停在它**之前** —— 中间那一轮的"
        "经过接不回来了，它留下的东西在下面这份状态里。你现在：\n\n"
    ),
    # 过期载荷换成的那句话，和图片块换成的那句话。写死，不是概括。
    material_trimmed="（这一段你当时读过，现在不在眼前了。还要就再去看一次。）",
    picture_trimmed="（这张图不在你眼前了。还要看就再拿出来一次。）",
    # 这一轮在它之前就结束了（结束这一轮的工具之后的调用）。
    never_ran="（这一轮在这只手之前就结束了，它没有执行。）",
)
