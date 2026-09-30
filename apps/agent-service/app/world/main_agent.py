"""world 主 agent 的一轮：被一条消息叫醒，看记录、看现实、让世界变化，最后定下次醒来的时刻。

**一条消息一轮。** 收件箱每送来一条消息（别人发来的、自己排的醒来、机制发回的"没有送达"
告知），:func:`on_world_message` 就跑一轮；被后来定的时刻取代了的自定消息直接跳过
（:func:`app.world.wake.is_stale_wake`）。收件箱开设时声明了一次只处理一条、一轮最多
:data:`ROUND_TIMEOUT`（:mod:`app.world.wiring`），所以同一时刻只有一轮在跑，一轮跑得再久
也不会被当成"前一个进程死了"被别人接管。

**一轮怎样才算跑完。** 模型那一段结束之后，依次：

1. 这一轮定了下次醒来的时刻吗（:func:`app.world.tools.wake_me_at`）？没定就抛
   :class:`NoNextWake`：这一轮算处理失败，按通信机制的重试再跑，重试用完进死信；
2. 把这一轮存进它的连续上下文（:mod:`app.agent.continuity`，按版本做 CAS）；
3. 定下次醒来（:func:`app.world.wake.set_next_wake`：先排出自定消息，broker 确认之后才记成
   私有状态里的最新唤醒）。

任何一步失败都往外抛，这一轮按失败重跑。重跑是安全的：它改过的记录留在盘上，下一次读得到；
上下文和下次醒来都只在最后才写下。叫醒这一轮的自定消息在第 3 步记下新唤醒之前一直是状态里的
最新唤醒，所以重投时不会被当成旧消息，失败了也不限次数重试、不进死信；哪一处失败、进程死在
哪里，各自怎么接上见 :mod:`app.world.wake`。先存上下文、后定时刻，是因为定时刻做完之后这一轮
就不该再重跑——否则会多出一个被取代的自定消息，而上下文里又少了这一轮。

**它眼前摆着什么。** 一条 USER 消息：现在几点、这一次是什么叫醒了它；被别人叫醒时再加上
它原来定的下次醒来，提醒它这一轮结束前要重新定。它的记录目录只在上下文清理时写进那条带
时刻的标记消息（:mod:`app.agent.continuity`；每轮都一样的东西不每轮重发）。prompt 在 Langfuse（:data:`ROUND_PROMPT_ID`），正文不
引用任何变量。

**模型和工具预算走 Dynamic Config**（:data:`WORLD_MODEL_KEY`、:data:`WORLD_RECURSION_LIMIT_KEY`），
默认值在这里。裁剪阈值（:data:`TRIM_POLICY`）是这个 agent 的一部分，写死在这里。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from inner_shared.dynamic_config import dynamic_config

from app.agent.context import AgentContext
from app.agent.continuity import (
    TrimPolicy,
    commit_transcript,
    next_transcript,
    trim_for_round,
)
from app.agent.core import AgentConfig
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.session import load_session
from app.agent.trace import collect_usage
from app.capabilities.agent import AgentRunner
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import now_cst, to_cst_full
from app.messaging.message import Kind, Message
from app.runtime.lane_policy import current_deployment_lane
from app.world import records
from app.world.tools import MATERIAL_TOOLS, ROUND_SCOPE, WORLD_TOOLS, RoundScope
from app.world.wake import WORLD, NextWake, is_stale_wake, read_next_wake, set_next_wake

logger = logging.getLogger(__name__)

# Langfuse prompt。泳道 label 取不到时退回 production（:func:`app.agent.prompts.get_prompt`）。
ROUND_PROMPT_ID = "world_round"
_TRACE_NAME = "world-round"

# Dynamic Config：用哪个模型、一轮最多几次带工具的模型调用。改它们不用重新部署。
WORLD_MODEL_KEY = "world_model"
DEFAULT_WORLD_MODEL = "offline-model"
WORLD_RECURSION_LIMIT_KEY = "world_recursion_limit"
# 一轮典型要做的事：列目录、读几份、查一次天气或搜一下、写一两份、定时刻。一次模型调用
# 可以同时调好几个工具，12 次留了余量。撞上它时框架再给一次不带工具的调用收尾——那一次
# 定不了时刻，这一轮按没定处理。
DEFAULT_WORLD_RECURSION_LIMIT = 12

# 一轮最多跑多久。收件箱按它放长占位租约（通信机制默认 15 分钟），超过就取消、按失败重试。
# 正常一轮几分钟；留到半小时是为了扛住一两次卡住的模型调用。
ROUND_TIMEOUT = timedelta(minutes=30)

# 它醒得稀：两次之间常常隔一两个小时。读到的材料（记录、搜索、天气）过一个清理周期就换成
# 一句"不在眼前了"，要用再读；它自己想过说过的留半天。
TRIM_POLICY = TrimPolicy(
    material_minutes=60,
    own_minutes=720,
    cleanup_minutes=60,
    hard_cap_tokens=150_000,
    trim_target_tokens=80_000,
)


class NoNextWake(RuntimeError):
    """这一轮跑完了模型那一段，却没有定下次醒来的时刻：这一轮不算完成。"""


def _lane() -> str:
    return current_deployment_lane() or "prod"


def _when(moment: datetime) -> str:
    return to_cst_full(moment.isoformat())


async def round_config() -> AgentConfig:
    model = await asyncio.to_thread(
        dynamic_config.get, WORLD_MODEL_KEY, default=DEFAULT_WORLD_MODEL
    )
    limit = await asyncio.to_thread(
        dynamic_config.get_int,
        WORLD_RECURSION_LIMIT_KEY,
        default=DEFAULT_WORLD_RECURSION_LIMIT,
    )
    if limit <= 0:
        logger.warning(
            "dynamic config %s = %r is not a positive integer; using %d",
            WORLD_RECURSION_LIMIT_KEY,
            limit,
            DEFAULT_WORLD_RECURSION_LIMIT,
        )
        limit = DEFAULT_WORLD_RECURSION_LIMIT
    return AgentConfig(
        ROUND_PROMPT_ID,
        model.strip() or DEFAULT_WORLD_MODEL,
        _TRACE_NAME,
        recursion_limit=limit,
    )


def build_round_runner(config: AgentConfig) -> AgentRunner:
    """这一轮的 agent。模块级函数，测试替身从这里换掉，不碰真模型。"""
    return AgentRunner(config, tools=WORLD_TOOLS)


def _render_state() -> str:
    """写进上下文清理标记消息的那段"你现在"：它有哪些记录。"""
    entries = records.listing()
    if not entries:
        return "你还没有任何记录。"
    return "\n".join(
        [f"你的记录（{len(entries)} 份）："]
        + [f"- {e.path}（{e.chars} 字）" for e in entries]
    )


def _render_round_input(trigger: Message, *, now: datetime, planned: NextWake | None) -> str:
    lines = [f"【现在】{_when(now)}"]
    if trigger.kind is Kind.MESSAGE and trigger.sender == WORLD:
        lines.append(f"【叫醒你的】你给自己排的一次醒来（排在 {_when(trigger.time)}）：")
        lines.append(trigger.body)
        return "\n".join(lines)
    if trigger.kind is Kind.NOT_DELIVERED:
        lines.append("【叫醒你的】通信机制告诉你，你的一条消息没有送达：")
        lines.append(trigger.body)
    else:
        lines.append(f"【叫醒你的】{trigger.sender} 发来一条消息（{_when(trigger.time)}）：")
        lines.append(trigger.body)
    if planned is not None:
        lines.append(
            f"【你原来定的下次醒来】{_when(planned.at)}。当时的说明：{planned.reason}\n"
            f"这一轮结束前要重新定一个时刻；还想按原来的来，就再定一次同一个时刻。"
        )
    return "\n".join(lines)


async def on_world_message(message: Message) -> None:
    """world 收件箱的处理函数：作废的自定消息跳过，其余每一条跑一轮。"""
    if is_stale_wake(message):
        logger.info(
            "world: wake %s was replaced by a later one; skipped", message.message_id
        )
        return
    await run_round(message)


async def run_round(trigger: Message) -> None:
    """跑一轮。没定下次醒来的时刻抛 :class:`NoNextWake`；别的失败原样往外抛。"""
    lane = _lane()
    now = now_cst()
    round_id = uuid.uuid4().hex
    key = f"{WORLD}:{lane}"

    history, ver = await load_session(key)
    history = trim_for_round(
        history,
        now=now,
        state=_render_state(),
        policy=TRIM_POLICY,
        material_tools=MATERIAL_TOOLS,
    )
    round_input = Turn(
        role=Role.USER,
        content=_render_round_input(trigger, now=now, planned=read_next_wake()),
    )
    scope = RoundScope()
    context = AgentContext(session_id=key, features={ROUND_SCOPE: scope})
    produced: list[Turn] = []
    runner = build_round_runner(await round_config())
    with collect_usage() as usage:
        reply = await runner.run(
            [*history, round_input],
            context=context,
            max_retries=1,
            transcript_sink=produced,
        )
    await record_round_cost(
        lane=lane, actor=WORLD, round_id=round_id, usage=usage, observed_at=now.isoformat()
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
    chosen = await set_next_wake(
        choice.at, f"你在 {_when(now)} 定下这个时刻醒来，当时写下的理由：{choice.reason}"
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
