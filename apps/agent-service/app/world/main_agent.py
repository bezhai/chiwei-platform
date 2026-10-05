"""world 主 agent 的一轮：被一条消息叫醒，看记录、看现实、让世界变化，最后定下次醒来的时刻。

**一条消息一轮。** 收件箱每送来一条消息（别人发来的、自己排的醒来、机制发回的"没有送达"
告知），:func:`on_world_message` 先把它交给各知识来源的收件处理
（:func:`app.world.sources.take_in`，各来源按消息 id 去重，重投、重跑都不会多存一份），再跑
一轮；被后来定的时刻取代了的自定消息不跑（:func:`app.world.wake.is_stale_wake`）。收件箱开设时
声明了一次只处理一条、一轮最多 :data:`ROUND_TIMEOUT`（:mod:`app.world.wiring`），所以同一时刻
只有一轮在跑，一轮跑得再久也不会被当成"前一个进程死了"被别人接管。

**一轮怎样才算跑完。** 模型那一段结束之后，依次：

1. 这一轮定了下次醒来的时刻吗（:func:`app.world.actions.wake_me_at`）？没定就抛
   :class:`NoNextWake`：这一轮算处理失败，按通信机制的重试再跑，重试用完进死信。工具预算
   用完时框架给的那一次不带工具的收尾定不了时刻，同样按没定处理；
2. 把这一轮存进它的连续上下文（:mod:`app.agent.continuity`，按版本做 CAS），然后清空
   :mod:`app.world.unfinished`——这一轮发生过的事已经在上下文里了；
3. 定下次醒来（:func:`app.world.wake.set_next_wake`：先排出自定消息，broker 确认之后才记成
   私有状态里的最新唤醒）。

任何一步失败都往外抛，这一轮按失败重跑。重跑是安全的：它改过的记录留在盘上，下一次读得到；
已经发出去的告知收不回来：报告过的变化、出过场的 NPC 和要发的告知（带预先定好的消息 id）在
发之前就记在 :mod:`app.world.unfinished`，下一轮模型跑之前按原 id 补发一遍（接收方按 id 去重），
再摆到它眼前，它不会把同一件事再报告一遍；上下文和下次醒来都只在最后才写下。叫醒这一轮的自定消息在第 3 步记下新唤醒之前一直是状态里的
最新唤醒，所以重投时不会被当成旧消息，失败了也不限次数重试、不进死信；哪一处失败、进程死在
哪里，各自怎么接上见 :mod:`app.world.wake`。先存上下文、后定时刻，是因为定时刻做完之后这一轮
就不该再重跑——否则会多出一个被取代的自定消息，而上下文里又少了这一轮。

**它眼前摆着什么。** 一条 USER 消息：现在几点、这一次是什么叫醒了它；被别人叫醒时再加上
它原来定的下次醒来，提醒它这一轮结束前要重新定；之前有一轮没跑完时，再加上那一轮里已经
发生的事和补发告知的结果。它的记录目录只在上下文清理时写进那条带
时刻的标记消息（:mod:`app.agent.continuity`；每轮都一样的东西不每轮重发）。prompt 在 Langfuse
（:data:`ROUND`），正文不引用任何变量。

**它手里有什么。** 全部已启用知识来源的查询工具（:func:`app.world.sources.query_tools`，跟
另外三类 agent 拿的是同一份），加上只有它才有的动作（:data:`app.world.actions.ACTIONS`）。
来源的查询结果是读到的材料，按 :data:`TRIM_POLICY` 的短档保留。

**模型和工具预算走 Dynamic Config**，跟另外三类一起在 :mod:`app.world.agents`。裁剪阈值
（:data:`TRIM_POLICY`）是这个 agent 的一部分，写死在这里。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from app.agent.context import AgentContext
from app.agent.continuity import (
    TrimPolicy,
    commit_transcript,
    next_transcript,
    trim_for_round,
)
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.session import load_session
from app.infra.cst_time import now_cst
from app.messaging.message import Kind, Message
from app.world import records, unfinished
from app.world.actions import ACTIONS, ROUND_SCOPE, RoundScope
from app.world.agents import WORLD_MODEL_KEY, AgentKind, run_agent, session_key, when
from app.world.perception import Told, render_told, tell
from app.world.sources import material_tools, query_tools, take_in
from app.world.sources.records import RECORDS_READ
from app.world.wake import WORLD, NextWake, is_stale_wake, read_next_wake, set_next_wake

logger = logging.getLogger(__name__)

# 主 agent 这一类：Langfuse prompt（泳道 label 取不到时退回 production，见
# :func:`app.agent.prompts.get_prompt`）、trace 名、模型键。
ROUND = AgentKind(prompt_id="world_round", trace_name="world-round", model_key=WORLD_MODEL_KEY)

# 一轮最多跑多久。收件箱按它放长占位租约（通信机制默认 15 分钟），超过就取消、按失败重试。
# 正常一轮几分钟；留到半小时是为了扛住一两次卡住的模型调用。
ROUND_TIMEOUT = timedelta(minutes=30)

# 读到的材料（记录、搜索、天气）过一个清理周期就换成一句"不在眼前了"，要用再读；它自己想过
# 说过的最多留半天。
#
# 带进一轮的历史压在两三万 token：估算超过 3 万就从最老的整组丢到 2 万以下。一轮里每调一次
# 模型都把这份历史整个再送一遍，费用跟它成正比。2026-10-05 在 coe-world 上量到：历史估
# 8.9 万时一次调用的输入是 9.35 万 token（另外是 SYSTEM 约 3.2k、工具定义约 1.5k），一轮调
# 3 到 5 次，列表价 1.4 到 2.3 美元；一轮在历史里平均留下约 1.7k token（中位 1.6k，最多 5k），
# 每跨一个清理点再加一条约 0.6k 的标记消息。所以 2 万到 3 万之间是十几轮。
#
# 被姐妹们的汇总叫醒时它一分钟就能跑一轮，先撞上的是这个顶；自己醒的时候两次常隔一两个小时，
# 半天里五六轮连同标记消息不到 3 万，那时起作用的是半天那一档。顶和目标之间留 1 万，是因为
# 每裁一次历史的开头就变了、那一轮的前缀缓存全部落空，1 万大约是六轮裁一次。
TRIM_POLICY = TrimPolicy(
    material_minutes=60,
    own_minutes=720,
    cleanup_minutes=60,
    hard_cap_tokens=30_000,
    trim_target_tokens=20_000,
)


class NoNextWake(RuntimeError):
    """这一轮跑完了模型那一段，却没有定下次醒来的时刻：这一轮不算完成。"""


def _render_state() -> str:
    """写进上下文清理标记消息的那段"你现在"：它有哪些记录。"""
    entries = records.listing()
    if not entries:
        return "你还没有任何记录。"
    return "\n".join(
        [f"你的记录（{len(entries)} 份）："]
        + [f"- {e.path}（{e.chars} 字）" for e in entries]
    )


def _render_round_input(
    trigger: Message,
    *,
    now: datetime,
    planned: NextWake | None,
    left_over: list[tuple[unfinished.Happening, list[Told]]],
) -> str:
    lines = [f"【现在】{when(now)}"]
    if trigger.kind is Kind.MESSAGE and trigger.sender == WORLD:
        lines.append(f"【叫醒你的】你给自己排的一次醒来（排在 {when(trigger.time)}）：")
        lines.append(trigger.body)
    else:
        if trigger.kind is Kind.NOT_DELIVERED:
            lines.append("【叫醒你的】通信机制告诉你，你的一条消息没有送达：")
        else:
            lines.append(f"【叫醒你的】{trigger.sender} 发来一条消息（{when(trigger.time)}）：")
        lines.append(trigger.body)
        if planned is not None:
            lines.append(
                f"【你原来定的下次醒来】{when(planned.at)}。当时的说明：{planned.reason}\n"
                f"这一轮结束前要重新定一个时刻；还想按原来的来，就再定一次同一个时刻。"
            )
    if left_over:
        lines.append(
            "【之前没跑完的一轮里已经发生的事】之前有一轮没有正常收尾。那一轮的经过你可能记得，"
            "也可能不记得；不管记不记得，下面这些在那一轮里已经发生了，记录里可能还没写。那一轮"
            "要发的告知，这一轮开始前已经按原样补发了一遍（收到过的人不会再收到一遍），下面是"
            "补发的结果："
        )
        lines += [
            f"- {when(h.at)}\n{h.what}\n{render_told(told)}" for h, told in left_over
        ]
    return "\n".join(lines)


async def on_world_message(message: Message) -> None:
    """world 收件箱的处理函数：先交给各知识来源收下，再看要不要跑一轮——作废的自定消息跳过，
    其余每一条跑一轮。"""
    await take_in(message)
    if is_stale_wake(message):
        logger.info(
            "world: wake %s was replaced by a later one; skipped", message.message_id
        )
        return
    await run_round(message)


async def _resend_left_over() -> list[tuple[unfinished.Happening, list[Told]]]:
    """模型跑之前：按原 id 把还没进上下文的那几件事要发的告知全部再发一遍。

    发送出错原样往外抛：这一轮在模型开始之前就算失败，记着的东西一样不动，下一轮再补。
    """
    left_over = [(h, await tell(h.notices)) for h in unfinished.read()]
    if left_over:
        logger.info(
            "world: resent %d notice(s) of %d happening(s) left by an unfinished round",
            sum(len(told) for _, told in left_over),
            len(left_over),
        )
    return left_over


async def run_round(trigger: Message) -> None:
    """跑一轮。没定下次醒来的时刻抛 :class:`NoNextWake`；别的失败原样往外抛。"""
    now = now_cst()
    round_id = uuid.uuid4().hex
    key = session_key()
    left_over = await _resend_left_over()

    history, ver = await load_session(key)
    history = trim_for_round(
        history,
        now=now,
        state=_render_state(),
        policy=TRIM_POLICY,
        material_tools=material_tools(),
    )
    round_input = Turn(
        role=Role.USER,
        content=_render_round_input(
            trigger, now=now, planned=read_next_wake(), left_over=left_over
        ),
    )
    scope = RoundScope(woken_by=trigger)
    context = AgentContext(
        session_id=key, features={ROUND_SCOPE: scope, RECORDS_READ: {}}
    )
    produced: list[Turn] = []
    reply = await run_agent(
        ROUND,
        [*history, round_input],
        tools=[*await query_tools(), *ACTIONS],
        context=context,
        call_id=round_id,
        transcript_sink=produced,
    )

    choice = scope.next_wake
    if choice is None:
        raise NoNextWake(
            f"world round {round_id} (woken by {trigger.message_id}) ended without "
            f"setting its next wake"
        )
    await commit_transcript(
        key,
        next_transcript(history, [round_input, *produced], policy=TRIM_POLICY),
        expected_ver=ver,
        session=None,
    )
    unfinished.clear()
    chosen = await set_next_wake(
        choice.at, f"你在 {when(now)} 定下这个时刻醒来，当时写下的理由：{choice.reason}"
    )
    logger.info(
        "world: round %s woken by %s (%s from %s) done; wrote %d record(s); next wake %s at %s; said: %s",
        round_id,
        trigger.message_id,
        trigger.kind,
        trigger.sender,
        len(scope.written),
        chosen.message_id,
        chosen.at.isoformat(),
        reply.text().strip()[:200],
    )
