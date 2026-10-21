"""传到她这里的消息和她往外发的消息，摆进她一轮的样子，和别的插件经服务读它们的那一面（household 插件的
插件对象）。

  * **开收件箱**（:meth:`Mailbox.open_inboxes`）：通信机制开始接收之前，按 ``residents`` 服务读好、
    检查过的名字给三姐妹各开一个；收件处理只存储（:func:`app.living.household.received.receive`）。
  * **输入段**「这段时间传到你这里的」（:meth:`Mailbox.received_section`）：她还没看过的那几条，
    这一轮读一次；**落地那个事务里的** ``household.read`` 一步（:meth:`Mailbox.mark_read`）把摆出来的
    那几条记成看过，没摆的一条都不记。
  * **往外发**（:meth:`Mailbox.send_what_she_did`）：一轮开始前（``outgoing.before``）和落地之后
    （``outgoing.after``）各一次，只在她的轮次队列上跑。
  * ``received`` 服务（:class:`app.living.services.Received`）：提前叫醒她的那条钟问还有没有要叫醒她
    的，日记读一天里传到她这里的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial

from app.agent.round import Round, wake_source
from app.host import DeclareInbox
from app.living.household.outgoing import send_what_she_did
from app.living.household.received import (
    ReceivedMessage,
    mark_read,
    read_received_between,
    receive,
    received_line,
    render_received,
    unread_received,
)
from app.living.services import HerDue, HerRecords, Letter, WhoLivesHere


@dataclass
class _Shown:
    """这一轮摆到她眼前的收件（这一轮的状态）：那一段读一次，落地那一步记成看过的就是这几条。"""

    items: list[ReceivedMessage] | None = None


class Mailbox:
    """household 插件对象：三姐妹的收件箱、她一轮里收件那一段和记成看过的那一步、往外发，和
    ``received`` 服务。名字对照从 ``residents`` 服务取（开收件箱、往外发时认出姐妹），她做过什么从
    ``her_records`` 服务读（往外讲的就是她存下的经历）。对象本身不带状态，这一轮摆出来的那几条在这一轮的
    状态上（:class:`_Shown`）。"""

    def __init__(self, *, residents: WhoLivesHere, her_records: HerRecords) -> None:
        self.residents = residents
        self.her_records = her_records

    # -- 开收件箱 ------------------------------------------------------------------

    async def open_inboxes(self, declare: DeclareInbox) -> None:
        """读三姐妹的名字（``residents`` 服务，读的时候检查），按名字各开一个收件箱：对每个名字调
        ``declare``（宿主交来的，参数和 :meth:`app.host.Context.inbox` 一样）。通信机制开始接收
        之前调一次。"""
        known = await self.residents.load()
        for name in known.by_persona.values():
            declare(name, on_message=partial(receive, residents=known))

    # -- received 服务 -------------------------------------------------------------

    async def earliest_waking_unread(self, *, lane: str, persona_id: str) -> str | None:
        """她还没看过、要叫醒她的最早那条的 id（:func:`unread_received` 的 ``waking_only``：过滤在
        查询里做，一条更早的不叫醒的挡不住它）。"""
        earliest = await unread_received(
            lane=lane, persona_id=persona_id, limit=1, waking_only=True
        )
        return earliest[0].message_id if earliest else None

    async def between(
        self, *, lane: str, persona_id: str, since: datetime, until: datetime
    ) -> list[ReceivedMessage]:
        """她收到的、说的是 ``[since, until)`` 之间的事的那些（:func:`read_received_between`）。"""
        return await read_received_between(
            lane=lane, persona_id=persona_id, since=since, until=until
        )

    def line(self, letter: Letter) -> str:
        """收到的一条在她眼里的样子（:func:`received_line`）。"""
        return received_line(letter)

    # -- 摆到她眼前的 --------------------------------------------------------------

    async def received_section(self, round: Round) -> str:
        """输入：这段时间传到她这里的。"""
        return render_received(await self._shown(round), now=round.now)

    async def _shown(self, round: Round) -> list[ReceivedMessage]:
        """传到她这里、她还没看过的消息。读在模型调用之前：只有这几条会跟这一轮一起记成看过
        （:meth:`mark_read`），这一轮跑着的时候新到的留给下一轮。

        ``due.must_show`` 是收件箱里叫醒她这一轮的那条消息的 id（:class:`app.living.services.HerDue`）：
        它还没看过，就一定摆进这一轮（``including``），这一轮落地它就算看过。这一轮的身份就是它；它不在
        这一轮里的话，这一轮落地了它还没看过，之后每一拍拿它叫醒她都撞上"这一轮跑过了"。
        """
        shown = round.state(_Shown)
        if shown.items is None:
            due: HerDue = round.due
            shown.items = await unread_received(
                lane=round.lane,
                persona_id=round.participant,
                including=due.must_show,
            )
        return shown.items

    # -- 落地 --------------------------------------------------------------------

    async def mark_read(self, round: Round) -> None:
        """这一轮摆到她眼前的那几条收件记成看过；没摆的一条都不记。清单里落地那个事务的
        ``household.read`` 一步，跟这一轮的 ``LifeMoment`` 同一个事务。"""
        await mark_read(round.state(_Shown).items or [], moment_id=round.round_id)

    # -- 往外发 ------------------------------------------------------------------

    async def send_what_she_did(self, round: Round) -> None:
        """她做了的事一条发给 world，只对姐妹说的话直接发给那位姐妹（:mod:`app.living.household.outgoing`）。

        登记两次。**一轮开始前**（``outgoing.before``，失败这一轮就失败）：上一轮失败、超时、赶上
        部署时，那一轮走不到收尾那一次。**这一轮落地之后**（``outgoing.after``，失败只记日志，这一轮
        照样算数）：她做了的事不随这一轮落不落地而改变，读的是她存下的经历；这一步没做完的，下一轮
        开始前那一次接着做。

        **只在她的轮次队列上跑，不在就当场报错。** 讲到哪了靠的是"她的经历这一刻全都落了库"，而那是
        队列一次只跑一轮给的：同一个人的两处同时讲，会把同一段讲两遍。队列跑一轮时绑着唤醒它的那个来源
        （:func:`app.agent.round.wake_source`），没有就是不在任何一个轮次里。
        """
        if wake_source() is None:
            raise RuntimeError(
                f"{round.participant} 往外发不在她的轮次队列上：讲到哪了要她的经历这一刻全都落了库，"
                "只有队列一次只跑一轮才保证得了"
            )
        await send_what_she_did(
            lane=round.lane,
            persona_id=round.participant,
            residents=self.residents.residents(),
            her_records=self.her_records,
        )
