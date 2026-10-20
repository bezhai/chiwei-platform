"""speaking 插件：她在手机上开口和撤回 —— 嘴只有出口，没有入口。

  * 两只手：``send_message``（:class:`app.living.speaking.mouth.Mouth`）给手机上某条会话发一条，
    ``take_back_message``（:class:`app.living.speaking.takeback.TakeBack`）撤回自己发出去的一条。
  * 嘴交出去之前那道检查（:mod:`app.living.speaking.output_safety`，禁用词在
    :mod:`app.living.speaking.banned_words`）：判词模型的段落由这里登记，它是她那一轮的子 agent。
  * 核对落没落地的那条钟（``landing``，:mod:`app.living.speaking.landing`）：渠道那边关于她这次开口
    的事实，事后按 id 取回来。
  * 两条出站：她说的话（``ChatResponseSegment``）和她撤回的（``Recall``）。
  * 要 ``phone`` 服务（发到哪、撤回找哪条会话、说的话算哪一档）、``her_records`` 服务（她说的话、
    撤回记进她的经历）和 ``pictures`` 服务（她给的图的句柄认回她手上那一张）。
"""
from __future__ import annotations

from datetime import timedelta

from app.agent.tooling import Tool
from app.domain.chat_dataflow import ChatResponseSegment
from app.domain.safety import Recall
from app.host import Context, Plugin
from app.living.common import living_lane
from app.living.speaking.landing import reconcile_what_she_said
from app.living.speaking.mouth import Mouth
from app.living.speaking.output_safety import GUARD_OUTPUT_SEGMENT
from app.living.speaking.takeback import TakeBack

# Five minutes between two matchings of what she said with what landed. Nobody waits for it: her
# rounds do not read that record, taking a message back looks the message up by its outbound id,
# and whether she still sees a message she took back is read from the channel's own row. The two
# writes it follows land seconds to tens of seconds after she acts, so any interval well above that
# matches them as well. Not denser, because every tick rescans the ones that will never match (a
# send whose outcome is unknown, a row the channel never wrote, a take-back that never succeeded),
# and that part does not shrink with time; not sparser, so that what landed is matched before her
# next regular round (ten minutes).
#
# **The landing clock is not a wake.** Matching what she said with what landed puts nothing on her
# queues: it takes no turn, and its writes are compare-and-set on the version chain, so it may run
# while one of her turns does (:mod:`app.living.speaking.landing`). Its tick gets only the time and
# runs the reconcile of this process's lane.
LANDING_EVERY = timedelta(minutes=5)


def setup(ctx: Context) -> None:
    lane = living_lane()
    phone, her_records = ctx.service("phone"), ctx.service("her_records")
    mouth = Mouth(phone=phone, her_records=her_records, pictures=ctx.service("pictures"))
    taking_back = TakeBack(phone=phone, her_records=her_records)
    # 发消息这只手的回执是"这件事到底做成了没有"的唯一记录（发出去了、已经说过了、交出去但没等到
    # 确认），裁掉她就会照着一个不知道有没有成功的动作再来一遍，所以留着。
    ctx.tool(Tool(mouth.send_message), trim="kept")
    # 撤回这只手的回执是她自己动作的结果，跟她自己的话一样留着。
    ctx.tool(Tool(taking_back.take_back_message), trim="kept")
    ctx.segment(GUARD_OUTPUT_SEGMENT)
    ctx.clock(
        "landing",
        LANDING_EVERY.total_seconds(),
        lambda ts: reconcile_what_she_said(lane=lane),
    )
    ctx.outbound(ChatResponseSegment, "chat_response")
    ctx.outbound(Recall, "recall")


PLUGIN = Plugin(
    name="speaking",
    setup=setup,
    requires=("phone", "her_records", "pictures"),
)
