"""world 主 agent 才有的动作。别的几类 agent 只拿知识来源的只读工具（:mod:`app.world.sources`），
这些只给主 agent：

* 写记录（``write_record``）由插件 ``record_keeping`` 登记（:mod:`app.world.records.keeping`）。
* :func:`wake_me_at` —— 定下次醒来的时刻。每一轮必须调，没调的一轮算失败
  （:mod:`app.world.main_agent`）。
* 报告世界里发生的一个变化（``report_change``，:meth:`app.world.perception.Perceiving.report_change`）
  和让一个 NPC 出场（``let_npc_appear``，:meth:`app.world.npc.Npcs.let_npc_appear`）要调别的
  agent，是拿着那两个 agent 的对象的方法，不在这里；告知先记后发的规矩见 :mod:`app.world.perception`。

**它们由 world 插件逐个登记**（:mod:`app.plugins.world`，``ctx.tool``），清单 ``world`` 把它们
排在知识来源的查询工具后面。它们的返回是它自己做过的事，跟着它自己的话一起留（``trim="kept"``；
只有来源的查询结果当材料裁）。

一轮里动作之间共享的东西是这一轮的状态 :class:`RoundScope`：这一轮定下的下次醒来。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.agent.round import current_round
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities._errors import CapabilityInvalidArg
from app.infra.cst_time import CST, now_cst
from app.world.agents import when


@dataclass(frozen=True)
class WakeChoice:
    at: datetime
    reason: str


@dataclass
class RoundScope:
    """一轮里工具之间共享的东西：这一轮的状态（``current_round().state(RoundScope)``）。"""

    # 这一轮定下的下次醒来；调过几次以最后一次为准。
    next_wake: WakeChoice | None = None


def _scope() -> RoundScope:
    return current_round().state(RoundScope)


# ---------------------------------------------------------------------------
# 下次醒来
# ---------------------------------------------------------------------------


@tool
@tool_error("没有定下")
async def wake_me_at(
    at: Annotated[
        str,
        Field(description="下次醒来的时刻，ISO 8601，形如 YYYY-MM-DDTHH:MM；不带时区的按东八区算"),
    ],
    reason: Annotated[
        str,
        Field(
            description=(
                "为什么定这个时刻。到时候这句话会原样摆在你眼前；时刻跟你原来定的下次醒来相同时，"
                "不另排，到时候摆出来的是原来那次写的理由"
            )
        ),
    ],
) -> str:
    """定下次醒来的时刻。每一轮结束前必须定一次；调了几次以最后一次为准。

    有人找你、或者有消息来，你会被提前叫醒，不用为等消息定时刻。
    """
    try:
        moment = datetime.fromisoformat(at.strip())
    except ValueError as exc:
        raise CapabilityInvalidArg(
            "时刻写成 ISO 8601，形如 YYYY-MM-DDTHH:MM；不带时区的按东八区算"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CST)
    now = now_cst()
    if moment <= now:
        raise CapabilityInvalidArg(
            f"这个时刻已经过去了（现在是 {when(now)}）：下次醒来要定在现在之后"
        )
    if not reason.strip():
        raise CapabilityInvalidArg("写一句为什么定这个时刻：到时候它会原样摆在你眼前")
    _scope().next_wake = WakeChoice(at=moment, reason=reason.strip())
    minutes = int((moment - now).total_seconds() // 60)
    return (
        f"定好了：{when(moment)}（离现在约 {minutes} 分钟）。这一轮结束时生效；"
        f"再调一次就改成新的时刻。"
    )
