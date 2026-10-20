"""household 插件：传到她这里的消息和她往外发的消息 —— 收件箱是她唯一的入口，收件只存储，她醒来时读；
她在世界里做了的事一条汇总给 world，只对姐妹说的话直接给那位姐妹。

  * 开始接收时开三姐妹的收件箱（``inboxes_at_start``），名字取自 ``residents`` 服务。
  * 输入段 ``household.received``：这段时间传到她这里、她还没看过的；落地那个事务里的
    ``household.read``：把摆出来的那几条记成看过。
  * 一轮开始前的 ``outgoing.before``、落地之后的 ``outgoing.after``：把她做了还没发出去的发出去
    （后一次失败只记日志，这一轮照样算数）；只在她的轮次队列上跑。
  * 交出 ``received`` 服务（:class:`app.living.services.Received`）。
  * 要 ``residents`` 服务（名字）和 ``her_records`` 服务（往外讲的是她存下的经历）。

收件怎么存、怎么读见 :mod:`app.living.household.received`，往外发什么、怎么发见
:mod:`app.living.household.outgoing`，摆进她一轮的样子见 :mod:`app.living.household.mailbox`。
"""
from __future__ import annotations

from app.agent.contracts import Continue
from app.host import Context, Plugin
from app.living.household.mailbox import Mailbox


def setup(ctx: Context) -> None:
    mailbox = Mailbox(residents=ctx.service("residents"), her_records=ctx.service("her_records"))
    ctx.provide("received", mailbox)
    ctx.inboxes_at_start(mailbox.open_inboxes)
    ctx.section("household.received", mailbox.received_section)
    ctx.round_start("outgoing.before", mailbox.send_what_she_did)
    ctx.commit("household.read", mailbox.mark_read)
    ctx.after_commit("outgoing.after", mailbox.send_what_she_did, on_failure=Continue())


PLUGIN = Plugin(
    name="household",
    setup=setup,
    requires=("residents", "her_records"),
    provides=("received",),
)
