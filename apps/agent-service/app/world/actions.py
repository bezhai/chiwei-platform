"""world 主 agent 才有的动作。别的几类 agent 只拿知识来源的只读工具（:mod:`app.world.sources`），
这些只给主 agent：

* :func:`write_record` —— 整份写下一份记录（:mod:`app.world.records`）。没有删除：一样东西
  没了，改写那份记录说它没了。人工接口可以删。
* :func:`wake_me_at` —— 定下次醒来的时刻。每一轮必须调，没调的一轮算失败
  （:mod:`app.world.main_agent`）。

一轮里动作之间共享的东西放在 :class:`RoundScope` 里，由这一轮的 ``AgentContext`` 带着：这一轮
写下了哪几份记录、定下的下次醒来。

**改写一份已有的记录，必须在这一轮里读过它现在的样子。** 模型不用自己搬指纹：读记录的工具
（:func:`app.world.sources.records.read_record`）把读到的指纹记进这一轮的
:data:`app.world.sources.records.RECORDS_READ`，写的时候拿它去做 :func:`app.world.records.write`
的指纹检查。它读过之后别人（人工接口）又改过，这次写就不落盘，告诉它重新读。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities._errors import CapabilityInvalidArg
from app.infra.cst_time import CST, now_cst
from app.world import records
from app.world.agents import when
from app.world.sources.records import RECORDS_READ, RecordPath

ROUND_SCOPE = "world_round"


@dataclass(frozen=True)
class WakeChoice:
    at: datetime
    reason: str


@dataclass
class RoundScope:
    """一轮里工具之间共享的东西。每一轮新建一个，放进 ``AgentContext.features``。"""

    # 这一轮写下的记录，按写的先后（只用来记日志）。
    written: list[str] = field(default_factory=list)
    # 这一轮定下的下次醒来；调过几次以最后一次为准。
    next_wake: WakeChoice | None = None


def _scope() -> RoundScope:
    return get_context().features[ROUND_SCOPE]


# ---------------------------------------------------------------------------
# 写记录
# ---------------------------------------------------------------------------


@tool
@tool_error("写记录失败")
async def write_record(
    path: RecordPath,
    text: Annotated[str, Field(description="这一份记录的全文，整份替换原来的")],
) -> str:
    """整份写下一份记录：没有就新建，有就整份替换。

    改写一份已经有的记录之前，要在这一轮里读过它现在的样子。
    """
    scope = _scope()
    reads: dict[str, str] = get_context().features[RECORDS_READ]
    try:
        written = records.write(path, text, expected=reads.get(path))
    except (records.InvalidRecordPath, records.InvalidRecordText) as exc:
        raise CapabilityInvalidArg(str(exc)) from exc
    except records.RecordConflict:
        if path in reads:
            return (
                f"没有写：「{path}」在你读过之后被改过了。重新读一遍（read_record）"
                f"再决定怎么写。"
            )
        return (
            f"没有写：「{path}」已经有了，你这一轮还没读过它现在的样子。先读一遍"
            f"（read_record）再写。"
        )
    reads[written.path] = written.fingerprint
    scope.written.append(written.path)
    return f"写好了：「{written.path}」（{len(written.text)} 字）。"


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
        str, Field(description="为什么定这个时刻。到时候这句话会原样摆在你眼前")
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


# 只给主 agent 的动作，排在知识来源的查询工具后面。它们的返回是它自己做过的事，跟着它自己
# 的话一起留（:mod:`app.agent.continuity` 只把来源的查询结果当材料裁）。
ACTIONS = [write_record, wake_me_at]
