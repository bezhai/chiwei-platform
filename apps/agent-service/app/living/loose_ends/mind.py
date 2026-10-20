"""她心里挂着的那份清单，摆进她一轮的样子，和她重写它的那只手（loose_ends 插件的插件对象）。

两段都由这里摆：

  * **状态段**「心里挂着没了结的」（:meth:`OnHerMind.open_section`）：整份清单，每条带着到没到点、
    从哪一轮带过来的。是她此刻的样子，清理那一下才重铺。
  * **输入段**「刚到点的」（:meth:`OnHerMind.just_due_section`）：上一轮到这一轮之间到点的那几件。

"刚到点的"是这条线上唯一一处从状态里**分出来**的东西：那份清单跟着状态走，但"这件事
到点了"是这一轮真的发生的变化。等下一次重铺的话，一件 15:30 该做的事要到 16:00 她才
看得见。

唯一一处在这里**算**出来的东西是「到点了 / 还没到」：她给线头挂的时刻当场跟 ``now`` 比，库里没有
这个状态（:func:`_open_end_line`）。这不是压缩，是把同一个事实换算成她读得懂的说法——比她自己拿
第一行的钟点去减更不容易错，而"这件事算不算了结"仍然只有她能答。

两段读的是同一份清单，一轮只读一次（:class:`_Open`，这一轮的状态），由第一个要它的段读：段在模型
之前算，那时她还没调过 ``keep_in_mind``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.agent.round import Round, current_round
from app.agent.tools._common import tool_error
from app.living.loose_ends.threads import (
    LooseEnd,
    format_entry,
    list_open_loose_ends,
    rewrite_loose_ends,
)


@dataclass
class _Open:
    """这一轮读到的、她还挂着的那份清单（这一轮的状态）。"""

    ends: list[LooseEnd] | None = None


class OnHerMind:
    """loose_ends 插件对象：``keep_in_mind`` 这只手、两段摆到她眼前的文字，和 ``loose_ends`` 服务
    （:class:`app.living.services.LooseEnds`）。不带状态。"""

    async def open_count(self, *, lane: str, persona_id: str) -> int:
        """她此刻还挂着几件（落地那一行的 ``open_ends``）。"""
        return len(await list_open_loose_ends(lane=lane, persona_id=persona_id))

    async def _open(self, round: Round) -> list[LooseEnd]:
        front = round.state(_Open)
        if front.ends is None:
            front.ends = await list_open_loose_ends(
                lane=round.lane, persona_id=round.participant
            )
        return front.ends

    async def open_section(self, round: Round) -> str:
        """状态：心里挂着没了结的那几件，带着到没到点。

        空的时候如实说空，不留白洞。**不带时刻**：界桩自己头上就印着这次清理的时刻，这里再报一次
        就是同一份输入里两个"现在"。
        """
        ends = await self._open(round)
        if not ends:
            return "心里挂着没了结的：（没有）"
        lines = [f"- {_open_end_line(e, now=round.now)}" for e in ends]
        return "心里挂着没了结的：\n" + "\n".join(lines)

    async def just_due_section(self, round: Round) -> str | None:
        """输入：这期间到点的那几件；一件都没有返回 ``None``（不写一行空标题）。

        **"刚到点的"是事件，不是清单。** 她挂着的那份清单跟着状态走、清理时才重铺一
        次（默认一小时），但"这件事到点了"是这一轮真的发生的变化 —— 等重铺的话一件
        15:30 该做的事要到 16:00 她才看得见，而 ``keep_in_mind`` 的文案对她的承诺是
        "到点之后你眼前那条会写着「到点了」"。

        上一轮的时刻是 ``None``（一个 moment 都没跑过）时也返回 ``None``：没有"这期间"可言，
        把一堆早就过期的事当成刚刚发生是往她眼前塞假话。
        """
        previous_at, now = round.previous_at, round.now
        if previous_at is None:
            return None
        came_due = [
            e
            for e in await self._open(round)
            if e.due_at is not None and previous_at < e.due_at <= now
        ]
        if not came_due:
            return None
        lines = [f"- {format_entry(e.what, e.due_at)}" for e in came_due]
        return "刚到点的：\n" + "\n".join(lines)

    @tool_error("记下心里挂着的事失败")
    async def keep_in_mind(
        self,
        still_on_my_mind: Annotated[
            list[str],
            Field(
                description=(
                    "你现在还挂着没了结的事，**全部**列出来（一件都没有就传空数组）。"
                    "有该在的时刻就写成「[2026-07-25 15:00] 家属谈话会」，"
                    "没有时刻就直接写那件事"
                )
            ),
        ],
    ) -> str:
        """记下我心里挂着、还没了结的事。

        **随时都能调，跟你换不换手上的事没有关系。** 有人跟你说了句要紧的话、你临时
        想起一件事、你答应了谁什么——手上的书不用放下，记一笔就行。

        这是**整份清单，不是新增**：列进去的会一直跟着你，这次没列的就此了结、下一次你
        就想不起来了。上一份清单每次都原样摆在你眼前，照着抄，再加上新的那件。

        你没主动记下来的东西，过几次就真的没了——你眼前那份"刚发生过什么"只有最近一小
        段，滚出去就找不回来了。

        **有的事该在几点**（约好的谈话、说了要打的电话、几点出门）。那就把时刻写在这件
        事前面：「[2026-07-25 15:00] 家属谈话会」。年月日 + 时分，今天几号看你眼前第一
        行。没有时刻的事直接写，别写方括号。

        到点之后你眼前那条会写着「到点了」，但它**不会自己消失**——去没去、算不算完是你
        说了算，你不再列它才是了结。

        想改时间就这次写一个不一样的时刻，不想要那个时刻了就这次别写方括号；都不用再调
        别的东西。

        Args:
            still_on_my_mind: 你现在还挂着没了结的事，全部。

        Returns:
            一句确认文本。
        """
        this_round = current_round()
        lane, now, persona_id = this_round.lane, this_round.now, this_round.participant
        moment_id = this_round.round_id
        open_now = await rewrite_loose_ends(
            lane=lane,
            persona_id=persona_id,
            moment_id=moment_id,
            now=now,
            still_on_my_mind=still_on_my_mind,
        )
        # 连时刻一起回给她：确认里只有事情没有时刻的话，她当场无从知道自己写的那个时刻
        # 收下了没有（下一个 moment 才在快照里看得见，那时已经隔了十分钟）。
        carried = "、".join(format_entry(e.what, e.due_at) for e in open_now) or "没有"
        return f"心里挂着：{carried}"


def _open_end_line(end: LooseEnd, *, now: datetime) -> str:
    """她心上一条线头的样子：这件事（可能带该在几点）· 到了没有 · 从哪一轮带过来的。

    **前半段走** :func:`~app.living.loose_ends.threads.format_entry`，所以她读到的形状就是她
    下一轮该照抄回 ``keep_in_mind`` 的形状（整份重写意味着她每一轮都要抄一遍）。抄回
    来解析不出同一件事的话，那条会在她眼皮底下被关掉、再以另一个身份重开。

    **"到点了"在这里当场算，库里没有这个状态。** 有个东西替她把"挂着"改成"到点了"
    就是替她做决定；而且到点之后这条**继续显示**，直到她自己不再列它——时间过了，那个
    会她还是没去开。理由见 :mod:`app.living.loose_ends`。

    ``opened_moment_id`` 而不是只给钟点：跨天之后"12:00 那一轮"分不清是哪一天，而这条
    正是"指得出它是从哪一轮带过来的"这个验收的落点。
    """
    parts = [format_entry(end.what, end.due_at)]
    if end.due_at is not None:
        parts.append("到点了" if end.due_at <= now else "还没到")
    parts.append(f"从 {end.opened_moment_id} 那一刻起挂着")
    return " · ".join(parts)
