"""world 主 agent 才有的动作。别的几类 agent 只拿知识来源的只读工具（:mod:`app.world.sources`），
这些只给主 agent：

* :func:`write_record` —— 整份写下一份记录（:mod:`app.world.records`）。没有删除：一样东西
  没了，改写那份记录说它没了。人工接口可以删。
* :func:`wake_me_at` —— 定下次醒来的时刻。每一轮必须调，没调的一轮算失败
  （:mod:`app.world.main_agent`）。
* :func:`report_change` —— 报告世界里发生的一个变化。由感知判断 agent 决定谁会察觉、各自
  察觉到什么，代码把判断发给他们（:mod:`app.world.perception`），主 agent 看到告知了谁、送没
  送达。这是让居民知道世界变化的唯一办法：主 agent 没有直接给谁发消息的工具。
* :func:`let_npc_appear` —— 让一个 NPC 在一次互动里出场。主 agent 只给人和情境，扮演他的临时
  agent 给出他的言行（:mod:`app.world.npc`），那段言行原样交给感知判断；主 agent 看到他的言行
  和告知了谁，事后把互动留下的东西记进记录。

报告变化、让 NPC 出场：判断完、发出任何一条告知之前，先把要发的告知连同预先定好的消息 id 记进
:mod:`app.world.unfinished`，再按这些 id 发。告知收不回来，这一轮要是没跑完，下一轮开始时按原 id
补发，并且看得见它们。

一轮里动作之间共享的东西放在 :class:`RoundScope` 里，由这一轮的 ``AgentContext`` 带着：叫醒这一轮
的那条消息（报告变化、让 NPC 出场都把它交给感知判断）、这一轮写下了哪几份记录、定下的下次醒来。

**改写一份已有的记录，必须在这一轮里读过它现在的样子。** 模型不用自己搬指纹：读记录的工具
（:func:`app.world.sources.records.read_record`）把读到的指纹记进这一轮的
:data:`app.world.sources.records.RECORDS_READ`，写的时候拿它去做 :func:`app.world.records.write`
的指纹检查。它读过之后别人（人工接口）又改过，这次写就不落盘，告诉它重新读。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities._errors import CapabilityInvalidArg
from app.infra.cst_time import CST, now_cst
from app.messaging.message import Message, message_body
from app.world import records, unfinished
from app.world.agents import when
from app.world.npc import play_npc
from app.world.perception import Notice, judge_who_notices, render_told, tell
from app.world.sources.records import RECORDS_READ, RecordPath

logger = logging.getLogger(__name__)

ROUND_SCOPE = "world_round"


@dataclass(frozen=True)
class WakeChoice:
    at: datetime
    reason: str


@dataclass
class RoundScope:
    """一轮里工具之间共享的东西。每一轮新建一个，放进 ``AgentContext.features``。"""

    # 叫醒这一轮的那条消息：感知判断要知道是谁的什么消息叫醒了这一轮
    # （:func:`app.world.perception.judge_who_notices`）。
    woken_by: Message
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


# ---------------------------------------------------------------------------
# 报告一个变化
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 报告一个变化、让一个 NPC 出场
#
# 这两个动作都分两段。判断那一段（感知判断、NPC 扮演）没成，什么都还没记、没发，失败交回给主
# agent，它可以再试一次。要记进 unfinished 的每一段话（变化原文、NPC 的名字和情境、NPC 的言行）
# 和每一条告知的正文，都先按通信机制的正文规则（:func:`app.messaging.message.message_body`）
# 检查过，记下来的东西一定写得进卷、发得出去。判断完先把告知记进 unfinished，再按记下的 id 发：从这里往后的失败（写不
# 进卷、发送出错、被取消）不收住，原样往外抛，这一轮按失败重来，下一轮开始时按原 id 补发。所以
# 这两个动作不包 @tool_error——它会把发送出错也变成一条交给模型的失败，这一轮照常跑完、清空
# unfinished，那条告知就再也发不出去了。
# ---------------------------------------------------------------------------


def _not_done(what: str, exc: Exception) -> str:
    logger.warning("world: %s: %s", what, exc, exc_info=True)
    return f"{what}：{type(exc).__name__}: {exc}。什么都还没告知，可以再试一次。"


async def _tell_and_keep(what: str, notices: list[Notice]) -> str:
    happening = unfinished.note(what, notices)
    return render_told(await tell(happening.notices))


@tool
async def report_change(
    change: Annotated[
        str,
        Field(
            description=(
                "世界里发生的一个变化：什么时候、在哪、什么变成了什么样。主语是世界：天气、"
                "地方、物件、机构、其他人的处境"
            )
        ),
    ],
) -> str:
    """报告世界里发生的一个变化，让会察觉到它的人知道。

    由感知判断决定谁会察觉、各自察觉到的是什么，并告知他们；返回告知了谁、送没送达。
    这是让居民知道世界变化的唯一办法。一个变化报告一次。
    """
    try:
        change = message_body(change.strip())
    except ValueError as exc:
        return f"没有报告：这段话里有记不下来的东西，改一下再报告（{exc}）。"
    try:
        notices = await judge_who_notices(change, woken_by=_scope().woken_by)
    except Exception as exc:
        return _not_done("没有报告出去，感知判断没有做成", exc)
    return await _tell_and_keep(f"你报告了一个变化：{change}", notices)


@tool
async def let_npc_appear(
    npc: Annotated[str, Field(description="出场的是谁：他在记录里的名字和身份")],
    situation: Annotated[
        str,
        Field(
            description=(
                "这次出场的情境：什么时候、在哪、刚刚发生了什么、谁在跟他打交道。只写情境，"
                "不写他要说什么、做什么"
            )
        ),
    ],
) -> str:
    """让一个 NPC 在一次互动里出场。

    一个临时 agent 依据各来源扮演他，给出他这一次说的话、做的事；这段言行原样交给感知判断，
    告知会察觉到的人。返回他的言行和告知了谁。这次互动留下了什么，之后由你记进记录。
    """
    try:
        name, situation = message_body(npc.strip()), message_body(situation.strip())
    except ValueError as exc:
        return f"没有出场：写下出场的是谁、这次出场的情境，里面不能有记不下来的东西（{exc}）。"
    try:
        acted = await play_npc(name, situation)
        if not acted:
            return f"{name} 这一次没有说话，也没有做什么。没有告知任何人。"
        notices = await judge_who_notices(message_body(acted), woken_by=_scope().woken_by)
    except Exception as exc:
        return _not_done(f"{name} 没有出场，扮演或者感知判断没有做成", exc)
    words = f"【{name} 这一次的言行】\n{acted}"
    told = await _tell_and_keep(f"你让 {name} 出场，情境：{situation}\n{words}", notices)
    return f"{words}\n\n{told}"


# 只给主 agent 的动作，排在知识来源的查询工具后面。它们的返回是它自己做过的事，跟着它自己
# 的话一起留（:mod:`app.agent.continuity` 只把来源的查询结果当材料裁）。
ACTIONS = [write_record, wake_me_at, report_change, let_npc_appear]
